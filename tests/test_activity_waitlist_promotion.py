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

    def test_deadline_matches_configured_window(self, session, svc, monkeypatch):
        """env 覆寫確認窗口長度"""
        monkeypatch.setenv("ACTIVITY_WAITLIST_CONFIRM_WINDOW_HOURS", "72")
        course = _add_course(session, capacity=1)
        reg_e = _add_reg(session, "在籍")
        _enroll(session, reg_e.id, course.id)
        reg_w = _add_reg(session, "候補")
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")

        before = datetime.now()
        svc.delete_registration(session, reg_e.id, "admin")
        session.flush()
        after = datetime.now()

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
        rc.promoted_at = datetime.now() - timedelta(hours=50)
        rc.confirm_deadline = datetime.now() - timedelta(hours=1)
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
        rc_w1.confirm_deadline = datetime.now() - timedelta(minutes=1)
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
        rc.promoted_at = datetime.now() - timedelta(hours=36)
        rc.confirm_deadline = datetime.now() + timedelta(hours=12)
        session.flush()

        result = svc.sweep_expired_pending_promotions(session)
        session.flush()

        assert result["reminded"] == 1
        assert rc.reminder_sent_at is not None

    def test_sweep_reminder_fires_once(self, session, svc):
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        rc = _enroll(session, reg.id, course.id, status="promoted_pending")
        rc.promoted_at = datetime.now() - timedelta(hours=36)
        rc.confirm_deadline = datetime.now() + timedelta(hours=12)
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
        assert result == {"expired": 0, "reminded": 0}

    def test_sweep_multiple_same_course_expired_all_replaced(self, session, svc):
        """同課 2 筆 pending 同時過期 → 應遞補 2 位（而非只補 1）"""
        course = _add_course(session, capacity=2)
        # 2 筆 promoted_pending 同時過期
        reg_p1 = _add_reg(session, "P1")
        rc_p1 = _enroll(session, reg_p1.id, course.id, status="promoted_pending")
        rc_p1.promoted_at = datetime.now() - timedelta(hours=50)
        rc_p1.confirm_deadline = datetime.now() - timedelta(hours=2)
        reg_p2 = _add_reg(session, "P2")
        rc_p2 = _enroll(session, reg_p2.id, course.id, status="promoted_pending")
        rc_p2.promoted_at = datetime.now() - timedelta(hours=50)
        rc_p2.confirm_deadline = datetime.now() - timedelta(hours=1)
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
        rc_p.promoted_at = datetime.now()
        rc_p.confirm_deadline = datetime.now() + timedelta(hours=24)
        reg_w = _add_reg(session, "丙")
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")
        session.flush()

        svc.delete_registration(session, reg_p.id, "admin")
        session.flush()

        assert rc_w.status == "promoted_pending"
