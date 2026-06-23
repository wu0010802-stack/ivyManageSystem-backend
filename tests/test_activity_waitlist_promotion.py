"""
tests/test_activity_waitlist_promotion.py — 候補轉正智能化（24h 確認窗）測試

涵蓋：
- 退課觸發 promoted_pending（deadline 設定正確）
- confirm / decline / sweep 狀態機
- 容量閘（promoted_pending 也佔位）
- 管理員手動升位（跳過 pending 直接 enrolled）
- 冪等 confirm（重複不壞資料）
- 刪除 promoted_pending 觸發遞補
- 邊界錯誤碼（409/410 語意）

使用 SQLite in-memory。
"""

import os
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 對齊 production `services/activity_service._now()`：全部走 naive Taipei time
# （UTC+8），讓 confirm_deadline / promoted_at 與 sweep 的 "now" 參考一致。
# 原本 test 用 datetime.now()，本機 +08 上看不出問題，但 CI 跑 UTC 時 production
# now 比 test 預期超前 8h → deadline 80h（=72+8）、sweep filter 漏命中。
from utils.taipei_time import now_taipei_naive as _now  # noqa: E402

from models.base import Base
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
    RegistrationChange,
)
from services.activity_service import ActivityService

# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def svc():
    return ActivityService()


def _add_course(
    session, name="美術", capacity=1, allow_waitlist=True
) -> ActivityCourse:
    c = ActivityCourse(
        name=name, price=1000, capacity=capacity, allow_waitlist=allow_waitlist
    )
    session.add(c)
    session.flush()
    return c


def _add_reg(
    session, student_name="王小明", parent_phone="0912345678"
) -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="大班",
        parent_phone=parent_phone,
        is_paid=False,
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


def _enroll(session, reg_id, course_id, status="enrolled") -> RegistrationCourse:
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course_id,
        status=status,
        price_snapshot=1000,
    )
    session.add(rc)
    session.flush()
    return rc


# ------------------------------------------------------------------ #
# 1. 退課觸發 promoted_pending
# ------------------------------------------------------------------ #


class TestAutoPromoteToPending:
    def test_delete_enrolled_promotes_waitlist_to_pending(self, session, svc):
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "候補")
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")

        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()

        assert rc_w.status == "promoted_pending"
        assert rc_w.promoted_at is not None
        assert rc_w.confirm_deadline is not None

    def test_promote_notifies_parent(self, session, svc, monkeypatch):
        """候補升正式時也要推家長（複用 activity.waitlist_reminder；修補只推 staff 缺口）。"""
        from unittest.mock import MagicMock
        import services.activity_service as svc_mod
        import services.notification.dispatch as dispatch_mod

        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "候補")
        _enroll(session, reg_w.id, course.id, status="waitlist")

        # fixture 無 guardian/user 鏈：mock 家長 resolver 回 fake uid；staff 回空聚焦家長路徑
        monkeypatch.setattr(
            svc_mod, "_resolve_parent_user_ids_for_registration", lambda s, rid: [999]
        )
        monkeypatch.setattr(
            svc_mod, "_list_active_users_with_permission", lambda s, p: []
        )
        spy = MagicMock()
        monkeypatch.setattr(dispatch_mod, "enqueue", spy)

        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()

        parent_calls = [
            c
            for c in spy.call_args_list
            if c.kwargs.get("event_type") == "activity.waitlist_reminder"
            and c.kwargs.get("recipient_user_id") == 999
        ]
        assert len(parent_calls) == 1
        assert parent_calls[0].kwargs["context"]["course_name"] == "美術"

    def test_deadline_matches_configured_window(self, session, svc, monkeypatch):
        """env 覆寫確認窗口長度"""
        monkeypatch.setenv("ACTIVITY_WAITLIST_CONFIRM_WINDOW_HOURS", "72")
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "候補")
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")

        before = _now()
        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()
        after = _now()

        # deadline 介於 before+72h 與 after+72h 之間
        assert rc_w.confirm_deadline >= before + timedelta(hours=71, minutes=59)
        assert rc_w.confirm_deadline <= after + timedelta(hours=72, minutes=1)

    def test_no_waitlist_no_op(self, session, svc):
        """無候補時，刪除後沒有 promoted_pending"""
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id)

        svc.delete_registration(session, reg.id, "admin")
        session.flush()

        pending_count = (
            session.query(RegistrationCourse)
            .filter(RegistrationCourse.status == "promoted_pending")
            .count()
        )
        assert pending_count == 0


# ------------------------------------------------------------------ #
# 2. 家長確認 / 放棄
# ------------------------------------------------------------------ #


class TestConfirmPromotion:
    def test_confirm_pending_becomes_enrolled(self, session, svc):
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "候補")
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")
        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()
        assert rc_w.status == "promoted_pending"

        svc.confirm_waitlist_promotion(session, reg_w.id, course.id)
        session.flush()

        assert rc_w.status == "enrolled"
        assert rc_w.confirm_deadline is None

    def test_confirm_expired_raises(self, session, svc):
        """confirm_deadline 已過 → EXPIRED"""
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.promoted_at = _now() - timedelta(hours=50)
        rc.confirm_deadline = _now() - timedelta(hours=1)
        session.flush()

        with pytest.raises(ValueError, match="EXPIRED"):
            svc.confirm_waitlist_promotion(session, reg.id, course.id)

    def test_confirm_already_enrolled_raises(self, session, svc):
        """已是 enrolled 再 confirm → ALREADY_CONFIRMED"""
        course = _add_course(session)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="enrolled")

        with pytest.raises(ValueError, match="ALREADY_CONFIRMED"):
            svc.confirm_waitlist_promotion(session, reg.id, course.id)

    def test_confirm_waitlist_raises(self, session, svc):
        """純 waitlist（非 pending）confirm → NOT_PENDING"""
        course = _add_course(session)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="waitlist")

        with pytest.raises(ValueError, match="NOT_PENDING"):
            svc.confirm_waitlist_promotion(session, reg.id, course.id)

    def test_confirm_idempotent_not_supported(self, session, svc):
        """第二次 confirm 應返回 ALREADY_CONFIRMED（表示 confirm 非冪等，但不破壞資料）"""
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "候補")
        _enroll(session, reg_w.id, course.id, status="waitlist")
        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()

        svc.confirm_waitlist_promotion(session, reg_w.id, course.id)
        session.flush()

        with pytest.raises(ValueError, match="ALREADY_CONFIRMED"):
            svc.confirm_waitlist_promotion(session, reg_w.id, course.id)


class TestConfirmPromotionTerminalStudentGuard:
    """Finding (P1)：已離校/畢業/轉出子女不可被升為正式。

    直接報名走 _assert_student_owned(for_write=True) 已擋終態；但候補確認只看
    ActivityRegistration.is_active，不 join Student。終態學生被升 enrolled 後會
    長出「幽靈報名」——佔課程容量，卻永不出現在點名名冊（session-detail 聚合
    明確排除 Student.is_active=False）。守衛放 service，家長端與公開端 confirm
    共用同一道。decline 不擋（讓終態學生可放棄佔位、釋出名額）。
    """

    def _setup_pending_for_student(self, session, svc, student):
        # 模擬「學生在籍時被遞補為待確認（promoted_pending），之後才轉終態」——
        # confirm 守衛即攔此情境。自 2026-06-23 P2-2 起 _auto_promote_first_waitlist
        # 會跳過終態學生（不再升位），故不能再靠 delete→auto_promote 把終態學生升上
        # promoted_pending；改為直接構造該狀態。
        from datetime import timedelta
        from services.activity_service import _now_taipei_naive

        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "候補")
        reg_w.student_id = student.id
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")
        svc.delete_registration(session, reg_e.id, "admin")  # 釋出名額
        # 直接升為待確認（在籍時遞補的等價狀態）
        rc_w.status = "promoted_pending"
        rc_w.confirm_deadline = _now_taipei_naive() + timedelta(hours=24)
        session.flush()
        assert rc_w.status == "promoted_pending"
        return reg_w, course, rc_w

    def test_confirm_blocked_for_terminal_student(self, session, svc):
        from models.classroom import LIFECYCLE_WITHDRAWN, Student

        student = Student(
            student_id="S-TERM-001",
            name="已退學童",
            lifecycle_status=LIFECYCLE_WITHDRAWN,
            is_active=False,
        )
        session.add(student)
        session.flush()
        reg_w, course, rc_w = self._setup_pending_for_student(session, svc, student)

        with pytest.raises(ValueError, match="STUDENT_TERMINAL"):
            svc.confirm_waitlist_promotion(session, reg_w.id, course.id)
        # 守衛須在改 status 前生效：rc 仍 promoted_pending，未長出幽靈 enrolled
        session.refresh(rc_w)
        assert rc_w.status == "promoted_pending"

    def test_confirm_allowed_for_active_student(self, session, svc):
        """在籍（is_active=True、非終態）學生正常升正式，守衛不誤殺。"""
        from models.classroom import LIFECYCLE_ACTIVE, Student

        student = Student(
            student_id="S-ACTIVE-001",
            name="在籍童",
            lifecycle_status=LIFECYCLE_ACTIVE,
            is_active=True,
        )
        session.add(student)
        session.flush()
        reg_w, course, rc_w = self._setup_pending_for_student(session, svc, student)

        svc.confirm_waitlist_promotion(session, reg_w.id, course.id)
        session.flush()
        assert rc_w.status == "enrolled"

    def test_confirm_allowed_when_no_student_linked(self, session, svc):
        """student_id 為 NULL（未配對學生）的報名不受終態守衛影響（無學生可判定）。"""
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "候補")  # 不設 student_id
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")
        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()
        assert rc_w.status == "promoted_pending"

        svc.confirm_waitlist_promotion(session, reg_w.id, course.id)
        session.flush()
        assert rc_w.status == "enrolled"


class TestDeclinePromotion:
    def test_decline_deletes_row_and_promotes_next(self, session, svc):
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w1 = _add_reg(session, "候補一")
        rc_w1 = _enroll(session, reg_w1.id, course.id, status="waitlist")
        reg_w2 = _add_reg(session, "候補二")
        rc_w2 = _enroll(session, reg_w2.id, course.id, status="waitlist")
        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()
        assert rc_w1.status == "promoted_pending"
        assert rc_w2.status == "waitlist"

        svc.decline_waitlist_promotion(session, reg_w1.id, course.id)
        session.flush()

        # w1 row 已被刪除；w2 遞補為 promoted_pending
        rc_w1_exists = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg_w1.id,
                RegistrationCourse.course_id == course.id,
            )
            .first()
        )
        assert rc_w1_exists is None
        assert rc_w2.status == "promoted_pending"

    def test_decline_non_pending_raises(self, session, svc):
        course = _add_course(session)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="waitlist")

        with pytest.raises(ValueError, match="NOT_PENDING"):
            svc.decline_waitlist_promotion(session, reg.id, course.id)


# ------------------------------------------------------------------ #
# 3. sweep 過期
# ------------------------------------------------------------------ #


class TestSweepExpired:
    def test_sweep_deletes_expired_and_promotes_next(self, session, svc):
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w1 = _add_reg(session, "候補一")
        rc_w1 = _enroll(session, reg_w1.id, course.id, status="waitlist")
        reg_w2 = _add_reg(session, "候補二")
        rc_w2 = _enroll(session, reg_w2.id, course.id, status="waitlist")
        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()

        # 手動把 w1 的 deadline 推回過去
        rc_w1.confirm_deadline = _now() - timedelta(minutes=1)
        session.flush()

        result = svc.sweep_expired_pending_promotions(session)
        session.flush()

        assert result["expired"] == 1
        # w1 已刪除；w2 遞補為 pending
        rc_w1_still = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg_w1.id,
                RegistrationCourse.course_id == course.id,
            )
            .first()
        )
        assert rc_w1_still is None
        assert rc_w2.status == "promoted_pending"

    def test_sweep_sends_reminder_when_near_deadline(self, session, svc):
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        # 離 deadline 還有 12h（落在預設 24h 提醒閾值內）
        rc.promoted_at = _now() - timedelta(hours=36)
        rc.confirm_deadline = _now() + timedelta(hours=12)
        session.flush()

        result = svc.sweep_expired_pending_promotions(session)
        session.flush()

        assert result["reminded"] == 1
        assert rc.reminder_sent_at is not None

    def test_sweep_reminder_fires_once(self, session, svc):
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.promoted_at = _now() - timedelta(hours=36)
        rc.confirm_deadline = _now() + timedelta(hours=12)
        session.flush()

        r1 = svc.sweep_expired_pending_promotions(session)
        session.flush()
        r2 = svc.sweep_expired_pending_promotions(session)
        session.flush()

        assert r1["reminded"] == 1
        assert r2["reminded"] == 0  # 已發過，不重發

    def test_sweep_noop_when_nothing_pending(self, session, svc):
        _add_course(session)
        result = svc.sweep_expired_pending_promotions(session)
        assert result == {"expired": 0, "reminded": 0, "final_reminded": 0}

    def test_sweep_multiple_same_course_expired_all_replaced(self, session, svc):
        """同課 2 筆 pending 同時過期 → 應遞補 2 位（而非只補 1）"""
        course = _add_course(session, capacity=2)
        # 2 筆 promoted_pending 同時過期
        reg_p1 = _add_reg(session, "P1")
        rc_p1 = _enroll(session, reg_p1.id, course.id, status="promoted_pending")
        rc_p1.promoted_at = _now() - timedelta(hours=50)
        rc_p1.confirm_deadline = _now() - timedelta(hours=2)
        reg_p2 = _add_reg(session, "P2")
        rc_p2 = _enroll(session, reg_p2.id, course.id, status="promoted_pending")
        rc_p2.promoted_at = _now() - timedelta(hours=50)
        rc_p2.confirm_deadline = _now() - timedelta(hours=1)
        # 2 筆候補排在後面
        reg_w1 = _add_reg(session, "W1")
        rc_w1 = _enroll(session, reg_w1.id, course.id, status="waitlist")
        reg_w2 = _add_reg(session, "W2")
        rc_w2 = _enroll(session, reg_w2.id, course.id, status="waitlist")
        session.flush()

        result = svc.sweep_expired_pending_promotions(session)
        session.flush()

        assert result["expired"] == 2
        # P1/P2 被刪；W1/W2 各升 promoted_pending
        assert rc_w1.status == "promoted_pending"
        assert rc_w2.status == "promoted_pending"

    def test_final_reminder_sent_when_within_6h(self, session, svc):
        """剩餘 ≤ 6h 且 final_reminder_sent_at NULL 時應發送並寫戳記。"""
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.promoted_at = _now() - timedelta(hours=42)
        rc.confirm_deadline = _now() + timedelta(hours=5)  # 剩 5h
        rc.reminder_sent_at = _now() - timedelta(hours=18)  # T-24h 已發
        rc.final_reminder_sent_at = None
        session.flush()

        result = svc.sweep_expired_pending_promotions(session)
        session.commit()

        assert result["final_reminded"] == 1
        session.refresh(rc)
        assert rc.final_reminder_sent_at is not None

    def test_final_reminder_not_resent(self, session, svc):
        """final_reminder_sent_at 非 NULL 時不重發。"""
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.confirm_deadline = _now() + timedelta(hours=3)
        rc.final_reminder_sent_at = _now() - timedelta(hours=1)
        session.flush()

        result = svc.sweep_expired_pending_promotions(session)
        assert result["final_reminded"] == 0

    def test_final_reminder_enqueues_dispatch_event(self, session, svc, monkeypatch):
        """T-6h: 進入 final_reminder 區間 → dispatch.enqueue 註冊事件 + 戳記寫入。

        PR-C-2 behavior change: dispatch 模型下 LINE 推送是 fire-and-forget，
        caller 無法探測送達結果。戳記改為「成功 enqueue 即寫」（dispatch._fan_out
        失敗只 log，不重推）。原「LINE ack-based 戳記」invariant 已 deprecated。
        """
        from unittest.mock import patch

        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.confirm_deadline = _now() + timedelta(hours=4)
        rc.reminder_sent_at = _now()
        rc_id = rc.id
        session.flush()

        with patch("services.notification.dispatch.enqueue") as mock_enqueue:
            result = svc.sweep_expired_pending_promotions(session)

        session.flush()
        rc_after = (
            session.query(RegistrationCourse)
            .filter(RegistrationCourse.id == rc_id)
            .first()
        )
        # PR-C-2 behavior: 成功 enqueue 即寫戳記
        assert (
            rc_after.final_reminder_sent_at is not None
        ), f"final_reminder_sent_at 未寫；result={result}"
        assert result["final_reminded"] == 1
        # reg 未設 student_id → 無 guardian → enqueue 不被呼叫但 success 仍標
        assert mock_enqueue.call_count == 0

    def test_t24_reminder_enqueues_dispatch_event(self, session, svc, monkeypatch):
        """T-24h: 進入 reminder 區間 → dispatch.enqueue 註冊事件 + 戳記寫入。

        deadline 剩 20h：進入 T-24h 區間（≤24h），但不在 T-6h 區間（>6h），
        T-24h 與 T-6h 完全互斥（I-1 修正），此筆只走 T-24h 分支。
        """
        from unittest.mock import patch

        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.confirm_deadline = _now() + timedelta(hours=20)
        rc_id = rc.id
        session.flush()

        with patch("services.notification.dispatch.enqueue") as mock_enqueue:
            result = svc.sweep_expired_pending_promotions(session)

        session.flush()
        rc_after = (
            session.query(RegistrationCourse)
            .filter(RegistrationCourse.id == rc_id)
            .first()
        )
        assert (
            rc_after.reminder_sent_at is not None
        ), f"reminder_sent_at 未寫；result={result}"
        assert result["reminded"] == 1
        # T-6h 分支不應觸發（deadline 距現在 20h > 6h）
        assert rc_after.final_reminder_sent_at is None
        assert result["final_reminded"] == 0
        assert mock_enqueue.call_count == 0


# ------------------------------------------------------------------ #
# 4. 容量閘：promoted_pending 佔位
# ------------------------------------------------------------------ #


class TestCapacityGate:
    def test_check_capacity_counts_pending_as_occupied(self, session, svc):
        """check_course_capacity: promoted_pending 被視為佔位"""
        course = _add_course(session, capacity=2)
        reg1 = _add_reg(session, "甲")
        _enroll(session, reg1.id, course.id, status="enrolled")
        reg2 = _add_reg(session, "乙")
        _enroll(session, reg2.id, course.id, status="promoted_pending")

        capacity, count, vacancy = svc.check_course_capacity(session, course.id)
        assert capacity == 2
        assert count == 2
        assert vacancy is False

    def test_auto_promote_blocked_when_pending_occupies(self, session, svc):
        """A 課容量 1：已有 promoted_pending 時，新退課不會重複升位"""
        course = _add_course(session, capacity=1)
        # 先讓 W1 升成 pending
        reg_e1 = _add_reg(session, "甲")
        _enroll(session, reg_e1.id, course.id)
        reg_w1 = _add_reg(session, "乙")
        rc_w1 = _enroll(session, reg_w1.id, course.id, status="waitlist")
        svc.delete_registration(session, reg_e1.id, "admin")
        session.flush()
        assert rc_w1.status == "promoted_pending"

        # 第二位候補應維持 waitlist（因為 W1 仍佔位）
        reg_w2 = _add_reg(session, "丙")
        rc_w2 = _enroll(session, reg_w2.id, course.id, status="waitlist")

        # 再發一次 auto_promote（模擬另一起退課誤觸發）
        svc._auto_promote_first_waitlist(session, course.id)
        session.flush()
        assert rc_w2.status == "waitlist"


# ------------------------------------------------------------------ #
# 5. 管理員手動升位
# ------------------------------------------------------------------ #


class TestAdminManualPromote:
    def test_admin_promote_skips_pending_directly_to_enrolled(self, session, svc):
        """管理員對 waitlist 直接升正式，跳過 24h 窗"""
        course = _add_course(session, capacity=2)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="waitlist")

        student_name, course_name = svc.promote_waitlist(session, reg.id, course.id)
        session.flush()

        assert rc.status == "enrolled"
        assert rc.confirm_deadline is None

    def test_admin_promote_pending_row_to_enrolled(self, session, svc):
        """管理員可將 promoted_pending 強制升 enrolled（代替家長確認）"""
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "甲")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "乙")
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")
        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()
        assert rc_w.status == "promoted_pending"

        svc.promote_waitlist(session, reg_w.id, course.id)
        session.flush()

        assert rc_w.status == "enrolled"
        assert rc_w.confirm_deadline is None

    def test_admin_promote_fails_when_capacity_full(self, session, svc):
        """已滿時管理員升位拋 ValueError（容量看 enrolled + pending - 自己）"""
        course = _add_course(session, capacity=1)
        reg1 = _add_reg(session, "甲")
        _enroll(session, reg1.id, course.id, status="enrolled")
        reg2 = _add_reg(session, "乙")
        _enroll(session, reg2.id, course.id, status="waitlist")

        with pytest.raises(ValueError, match="容量已滿"):
            svc.promote_waitlist(session, reg2.id, course.id)


# ------------------------------------------------------------------ #
# 6. 刪除 promoted_pending 也觸發遞補
# ------------------------------------------------------------------ #


class TestDeletePromotedPendingCascade:
    def test_delete_registration_with_pending_promotes_next(self, session, svc):
        """delete_registration 處理 promoted_pending 時也應遞補下一位"""
        course = _add_course(session, capacity=1)
        reg_p = _add_reg(session, "乙")
        rc_p = _enroll(session, reg_p.id, course.id, status="promoted_pending")
        rc_p.promoted_at = _now()
        rc_p.confirm_deadline = _now() + timedelta(hours=24)
        reg_w = _add_reg(session, "丙")
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")
        session.flush()

        svc.delete_registration(session, reg_p.id, "admin")
        session.flush()

        assert rc_w.status == "promoted_pending"


def test_final_reminder_sent_at_field_exists(session):
    """final_reminder_sent_at 欄位應存在於 RegistrationCourse model。"""
    rc = RegistrationCourse(
        registration_id=1,
        course_id=1,
        status="promoted_pending",
        price_snapshot=1000,
        final_reminder_sent_at=None,
    )
    assert hasattr(rc, "final_reminder_sent_at")
    assert rc.final_reminder_sent_at is None


@pytest.mark.skip(
    reason="2026-05-26 notification phase 4 section 4 (f745380): "
    "LineService._notify_* 全系列下架，邏輯遷至 services.notification.dispatch.enqueue。"
    "本 test 驗證舊 private method 存在，已不符 dispatcher-based 架構。"
)
def test_line_service_has_final_reminder_method():
    """LineService 應有 _notify_activity_waitlist_final_reminder 方法（PR-D
    rename 後私有 method；caller 走 dispatch.enqueue 不再直接呼叫）。"""
    from services.line_service import LineService

    assert hasattr(LineService, "_notify_activity_waitlist_final_reminder")


# ------------------------------------------------------------------ #
# N. 候補/待確認轉正後重算 is_paid（修付款狀態錯位）
# ------------------------------------------------------------------ #


class TestPromotionRecomputesIsPaid:
    """候補/待確認轉 enrolled 會讓 total 增加（_calc_total_amount 只計 enrolled），
    必須同步重算 is_paid。否則原本付清的報名轉正後仍停在 is_paid=True，被
    payment_status=paid 篩選誤收為已繳、催繳清單漏列。"""

    def _setup(self, session, pending_status):
        # 課 A：已 enrolled 且付清；課 B：待轉正（waitlist 或 promoted_pending）
        course_a = _add_course(session, name="A", capacity=10)
        course_b = _add_course(session, name="B", capacity=10)
        reg = _add_reg(session, "付清家長")
        _enroll(session, reg.id, course_a.id, status="enrolled")  # price_snapshot 1000
        rc_b = _enroll(session, reg.id, course_b.id, status=pending_status)
        # 模擬「只就 enrolled 的課 A 付清」：total=1000、paid=1000 → is_paid=True
        reg.paid_amount = 1000
        reg.is_paid = True
        session.flush()
        return reg, course_b, rc_b

    def test_confirm_promotion_recomputes_is_paid_to_false(self, session, svc):
        reg, course_b, rc_b = self._setup(session, "promoted_pending")
        assert reg.is_paid is True  # 前提：轉正前已繳清

        svc.confirm_waitlist_promotion(session, reg.id, course_b.id)
        session.flush()

        assert rc_b.status == "enrolled"
        # total 變 2000、paid 仍 1000 → 應重算為未繳清
        assert reg.is_paid is False

    def test_admin_promote_recomputes_is_paid_to_false(self, session, svc):
        reg, course_b, rc_b = self._setup(session, "waitlist")
        assert reg.is_paid is True

        svc.promote_waitlist(session, reg.id, course_b.id)
        session.flush()

        assert rc_b.status == "enrolled"
        assert reg.is_paid is False

    def test_promotion_keeps_is_paid_true_when_paid_covers_new_total(
        self, session, svc
    ):
        """轉正後 paid 仍 >= 新 total，is_paid 維持 True（守衛：不過度翻 False）。"""
        course_a = _add_course(session, name="A", capacity=10)
        course_b = _add_course(session, name="B", capacity=10)
        reg = _add_reg(session, "預繳家長")
        _enroll(session, reg.id, course_a.id, status="enrolled")
        rc_b = _enroll(session, reg.id, course_b.id, status="waitlist")
        reg.paid_amount = 2000  # 已預繳兩堂課
        reg.is_paid = True
        session.flush()

        svc.promote_waitlist(session, reg.id, course_b.id)
        session.flush()

        assert rc_b.status == "enrolled"
        assert reg.is_paid is True
