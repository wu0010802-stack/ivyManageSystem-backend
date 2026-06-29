"""tests/test_activity_waitlist_notify_release_2026_06_29.py

才藝候補升位通知 / 過期同步釋出 稽核修補（2026-06-29）：

- F3 過期同步釋出：confirm 端偵測逾期時，特權 session 應同步「刪除逾期
  promoted_pending → 通知家長逾期 → 遞補下一位」，使家長端「名額已釋出給下一位
  候補」訊息名實相符，不再依賴預設停用的 sweeper。
- F2 手動升位通知家長：管理員手動升位（直升 enrolled）除通知 staff 外，亦應通知
  家長（複用 activity.waitlist_promoted，deadline=None → 「已升為正式報名」）。

使用 SQLite in-memory（與 test_activity_waitlist_promotion.py 對齊）。
"""

import os
import sys
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.taipei_time import now_taipei_naive as _now  # noqa: E402

import services.activity_service as svc_mod  # noqa: E402
from models.base import Base  # noqa: E402
from models.activity import (  # noqa: E402
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
from models.auth import User  # noqa: E402
from services.activity_service import ActivityService  # noqa: E402


def _add_user(session, *, active=True, line=True) -> User:
    """建立一個家長 User；line=True 代表 LINE 真正可達（active + 綁定 + 已加好友）。"""
    u = User(
        username=f"parent_{ActivityRegistration.__name__}_{id(object())}",
        password_hash="x",
        role="parent",
        is_active=active,
        line_user_id=("U" + str(id(object()))) if line else None,
        line_follow_confirmed_at=_now() if line else None,
    )
    session.add(u)
    session.flush()
    return u


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


def _add_course(session, name="美術", capacity=1) -> ActivityCourse:
    c = ActivityCourse(name=name, price=1000, capacity=capacity, allow_waitlist=True)
    session.add(c)
    session.flush()
    return c


def _add_reg(session, student_name="王小明") -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="大班",
        parent_phone="0912345678",
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


def _rc(session, reg_id, course_id):
    return (
        session.query(RegistrationCourse)
        .filter(
            RegistrationCourse.registration_id == reg_id,
            RegistrationCourse.course_id == course_id,
        )
        .first()
    )


# ------------------------------------------------------------------ #
# F3 過期同步釋出
# ------------------------------------------------------------------ #


class TestReleaseExpiredPendingPromotion:
    def test_release_deletes_expired_and_promotes_next(self, session, svc):
        """逾期 promoted_pending 被刪除，下一位候補遞補為 promoted_pending。"""
        course = _add_course(session, capacity=1)
        reg_p = _add_reg(session, "逾期待確認")
        rc_p = _enroll(session, reg_p.id, course.id, status="promoted_pending")
        rc_p.confirm_deadline = _now() - timedelta(hours=1)
        reg_w = _add_reg(session, "候補二")
        _enroll(session, reg_w.id, course.id, status="waitlist")
        session.flush()

        name, course_name = svc.release_expired_pending_promotion(
            session, reg_p.id, course.id
        )
        session.flush()

        assert course_name == "美術"
        # 逾期者已被刪除（名額釋出）
        assert _rc(session, reg_p.id, course.id) is None
        # 下一位遞補
        assert _rc(session, reg_w.id, course.id).status == "promoted_pending"

    def test_release_notifies_parent_of_expiry(self, session, svc, monkeypatch):
        """釋出時通知逾期家長（activity.waitlist_expired）。"""
        from unittest.mock import MagicMock
        import services.notification.dispatch as dispatch_mod

        course = _add_course(session, capacity=1)
        reg_p = _add_reg(session, "逾期待確認")
        rc_p = _enroll(session, reg_p.id, course.id, status="promoted_pending")
        rc_p.confirm_deadline = _now() - timedelta(hours=1)
        session.flush()

        monkeypatch.setattr(
            svc_mod, "_resolve_parent_user_ids_for_registration", lambda s, rid: [777]
        )
        monkeypatch.setattr(
            svc_mod, "_list_active_users_with_permission", lambda s, p: []
        )
        spy = MagicMock()
        monkeypatch.setattr(dispatch_mod, "enqueue", spy)

        svc.release_expired_pending_promotion(session, reg_p.id, course.id)

        expired_calls = [
            c
            for c in spy.call_args_list
            if c.kwargs.get("event_type") == "activity.waitlist_expired"
            and c.kwargs.get("recipient_user_id") == 777
        ]
        assert len(expired_calls) == 1

    def test_release_rejects_non_expired_pending(self, session, svc):
        """未逾期的 promoted_pending 不可被釋出（守衛：誤呼叫不誤刪有效名額）。"""
        course = _add_course(session, capacity=1)
        reg_p = _add_reg(session, "未逾期")
        rc_p = _enroll(session, reg_p.id, course.id, status="promoted_pending")
        rc_p.confirm_deadline = _now() + timedelta(hours=10)
        session.flush()

        with pytest.raises(ValueError, match="NOT_EXPIRED"):
            svc.release_expired_pending_promotion(session, reg_p.id, course.id)
        # 仍存在、狀態不變
        assert _rc(session, reg_p.id, course.id).status == "promoted_pending"

    def test_release_rejects_enrolled(self, session, svc):
        """已是 enrolled（非 pending）不可走釋出路徑。"""
        course = _add_course(session, capacity=1)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="enrolled")
        session.flush()

        with pytest.raises(ValueError, match="NOT_PENDING"):
            svc.release_expired_pending_promotion(session, reg.id, course.id)


# ------------------------------------------------------------------ #
# F2 手動升位通知家長
# ------------------------------------------------------------------ #


class TestManualPromoteNotifiesParent:
    def test_manual_promote_notifies_parent(self, session, svc, monkeypatch):
        """管理員手動升位（直升 enrolled）也應通知家長（activity.waitlist_promoted，
        deadline 不帶 → 渲染「已升為正式報名」）。"""
        from unittest.mock import MagicMock
        import services.notification.dispatch as dispatch_mod

        course = _add_course(session, capacity=2)
        reg = _add_reg(session, "候補生")
        _enroll(session, reg.id, course.id, status="waitlist")
        session.flush()

        monkeypatch.setattr(
            svc_mod, "_resolve_parent_user_ids_for_registration", lambda s, rid: [555]
        )
        spy = MagicMock()
        monkeypatch.setattr(dispatch_mod, "enqueue", spy)

        svc.promote_waitlist(session, reg.id, course.id)

        parent_calls = [
            c
            for c in spy.call_args_list
            if c.kwargs.get("event_type") == "activity.waitlist_promoted"
            and c.kwargs.get("recipient_user_id") == 555
        ]
        assert len(parent_calls) == 1
        # 直升無 confirm 窗 → context 不帶 deadline（渲染「已升為正式報名」）
        assert parent_calls[0].kwargs["context"].get("deadline") is None
        assert parent_calls[0].kwargs["context"]["course_name"] == "美術"


# ------------------------------------------------------------------ #
# F1 自動升位：無家長通知管道時於 staff 通知標註
# ------------------------------------------------------------------ #


class TestAutoPromoteFlagsNoParentChannel:
    """F1（2026-06-29 audit）：自動升位仍對所有候補生效（含 student_id=None 公開
    報名——本就無 Guardian 管道，是一級支援型態，不可跳過否則公開報名永不升位），
    但當被升候補無家長 App/LINE 通知管道時，於 staff 通知 context 標註
    no_parent_channel + parent_phone，讓 staff 主動以電話通知確認，閉合
    「啟動 48h 確認時鐘但家長收不到任何通知 → 靜默失位」的缺口。"""

    def _spy_dispatch(self, monkeypatch):
        from unittest.mock import MagicMock
        import services.notification.dispatch as dispatch_mod

        spy = MagicMock()
        monkeypatch.setattr(dispatch_mod, "enqueue", spy)
        return spy

    def test_no_channel_candidate_still_promoted_and_flags_staff(
        self, session, svc, monkeypatch
    ):
        course = _add_course(session, capacity=1)
        reg_w = _add_reg(session, "無管道候補")  # student_id=None → 無 Guardian 管道
        rc_w = _enroll(session, reg_w.id, course.id, status="waitlist")
        session.flush()

        monkeypatch.setattr(
            svc_mod, "_resolve_parent_user_ids_for_registration", lambda s, rid: []
        )
        monkeypatch.setattr(
            svc_mod, "_list_active_users_with_permission", lambda s, p: [42]
        )
        spy = self._spy_dispatch(monkeypatch)

        svc._auto_promote_first_waitlist(session, course.id)
        session.flush()

        # 仍被升位（公開報名不可被跳過）
        assert rc_w.status == "promoted_pending"
        # staff 通知 context 帶 no_parent_channel + parent_phone
        staff_calls = [
            c
            for c in spy.call_args_list
            if c.kwargs.get("event_type") == "activity.waitlist_promoted"
            and c.kwargs.get("recipient_user_id") == 42
        ]
        assert len(staff_calls) == 1
        ctx = staff_calls[0].kwargs["context"]
        assert ctx.get("no_parent_channel") is True
        assert ctx.get("parent_phone") == "0912345678"

    def test_channel_candidate_does_not_flag_staff(self, session, svc, monkeypatch):
        """有 Guardian.user_id 且該 User 真正 LINE 可達 → 不 flag。"""
        course = _add_course(session, capacity=1)
        reg_w = _add_reg(session, "有管道候補")
        _enroll(session, reg_w.id, course.id, status="waitlist")
        parent = _add_user(session, active=True, line=True)
        session.flush()

        monkeypatch.setattr(
            svc_mod,
            "_resolve_parent_user_ids_for_registration",
            lambda s, rid: [parent.id],
        )
        monkeypatch.setattr(
            svc_mod, "_list_active_users_with_permission", lambda s, p: [42]
        )
        spy = self._spy_dispatch(monkeypatch)

        svc._auto_promote_first_waitlist(session, course.id)
        session.flush()

        staff_calls = [
            c
            for c in spy.call_args_list
            if c.kwargs.get("event_type") == "activity.waitlist_promoted"
            and c.kwargs.get("recipient_user_id") == 42
        ]
        assert len(staff_calls) == 1
        ctx = staff_calls[0].kwargs["context"]
        assert not ctx.get("no_parent_channel")

    def test_user_without_line_binding_flags_no_parent_channel(
        self, session, svc, monkeypatch
    ):
        """Finding #2（2026-06-29 audit）：有 Guardian.user_id 但該 User 未綁定 LINE
        （或未加好友/已停用）→ 實際不可達。舊版只看 parent_uids 是否為空 → 誤判為
        有管道、不提醒 staff；真正發送時 dispatch 才拒絕，家長收不到、48h 靜默失位。
        修後：no_parent_channel 須反映真實 LINE 可達性 → 此情境應 flag staff 電話外撥。"""
        course = _add_course(session, capacity=1)
        reg_w = _add_reg(session, "帳號未綁LINE")
        _enroll(session, reg_w.id, course.id, status="waitlist")
        parent = _add_user(session, active=True, line=False)  # 有帳號但無 LINE 綁定
        session.flush()

        monkeypatch.setattr(
            svc_mod,
            "_resolve_parent_user_ids_for_registration",
            lambda s, rid: [parent.id],
        )
        monkeypatch.setattr(
            svc_mod, "_list_active_users_with_permission", lambda s, p: [42]
        )
        spy = self._spy_dispatch(monkeypatch)

        svc._auto_promote_first_waitlist(session, course.id)
        session.flush()

        staff_calls = [
            c
            for c in spy.call_args_list
            if c.kwargs.get("event_type") == "activity.waitlist_promoted"
            and c.kwargs.get("recipient_user_id") == 42
        ]
        assert len(staff_calls) == 1
        ctx = staff_calls[0].kwargs["context"]
        assert ctx.get("no_parent_channel") is True
        assert ctx.get("parent_phone") == "0912345678"

    def test_inactive_user_flags_no_parent_channel(self, session, svc, monkeypatch):
        """有綁定 LINE 但帳號已停用 → dispatch 不會送（is_active 守衛）→ 應 flag。"""
        course = _add_course(session, capacity=1)
        reg_w = _add_reg(session, "停用帳號")
        _enroll(session, reg_w.id, course.id, status="waitlist")
        parent = _add_user(session, active=False, line=True)  # 綁了 LINE 但停用
        session.flush()

        monkeypatch.setattr(
            svc_mod,
            "_resolve_parent_user_ids_for_registration",
            lambda s, rid: [parent.id],
        )
        monkeypatch.setattr(
            svc_mod, "_list_active_users_with_permission", lambda s, p: [42]
        )
        spy = self._spy_dispatch(monkeypatch)

        svc._auto_promote_first_waitlist(session, course.id)
        session.flush()

        staff_calls = [
            c
            for c in spy.call_args_list
            if c.kwargs.get("event_type") == "activity.waitlist_promoted"
            and c.kwargs.get("recipient_user_id") == 42
        ]
        ctx = staff_calls[0].kwargs["context"]
        assert ctx.get("no_parent_channel") is True

    def test_staff_renderer_surfaces_no_channel_warning(self):
        """in_app renderer 對 no_parent_channel context 應在 body 附上電話外撥提示。"""
        from services.notification.renderers import render

        rendered = render(
            "activity.waitlist_promoted",
            {
                "student_name": "小明",
                "course_name": "美術",
                "course_id": 7,
                "no_parent_channel": True,
                "parent_phone": "0912345678",
            },
        )
        assert "0912345678" in rendered.body
        assert "管道" in rendered.body or "電話" in rendered.body
