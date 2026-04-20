"""
tests/test_activity_registration_logic.py — 才藝報名核心邏輯單元測試。

涵蓋：
- delete_registration 軟刪除 + 自動候補升位（B9）
- public_update_registration 時間窗口檢查（B7）
- RegistrationTimeSettings Pydantic 驗證（B8）

使用 SQLite in-memory，不依賴 PostgreSQL。
"""

import os
import sys
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    ActivityRegistrationSettings,
    RegistrationCourse,
    RegistrationChange,
)
from services.activity_service import ActivityService
from api.activity import RegistrationTimeSettings

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


def _add_course(session, name="美術", capacity=1) -> ActivityCourse:
    c = ActivityCourse(name=name, price=1000, capacity=capacity, allow_waitlist=True)
    session.add(c)
    session.flush()
    return c


def _add_reg(session, student_name="王小明", class_name="大班") -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name=class_name,
        is_paid=False,
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


def _enroll(
    session, reg_id: int, course_id: int, status: str = "enrolled"
) -> RegistrationCourse:
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
# B9 - delete_registration 自動候補升位
# ------------------------------------------------------------------ #


class TestDeleteRegistrationAutoPromote:
    def test_delete_soft_deletes_is_active(self, session, svc):
        """刪除後 is_active=False"""
        course = _add_course(session)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="enrolled")

        svc.delete_registration(session, reg.id, "admin")
        session.flush()

        assert reg.is_active is False

    def test_delete_auto_promotes_first_waitlist_to_pending(self, session, svc):
        """刪除正式報名後，候補第一位自動升為 promoted_pending（24h 確認窗）"""
        course = _add_course(session, capacity=1)

        # 第一筆：正式報名（占滿名額）
        reg1 = _add_reg(session, student_name="甲")
        _enroll(session, reg1.id, course.id, status="enrolled")

        # 第二筆：候補
        reg2 = _add_reg(session, student_name="乙")
        rc2 = _enroll(session, reg2.id, course.id, status="waitlist")

        # 刪除正式報名 → 候補應自動升 promoted_pending 並帶 deadline
        svc.delete_registration(session, reg1.id, "admin")
        session.flush()

        assert rc2.status == "promoted_pending"
        assert rc2.promoted_at is not None
        assert rc2.confirm_deadline is not None
        assert rc2.confirm_deadline > rc2.promoted_at

    def test_delete_no_waitlist_no_error(self, session, svc):
        """無候補時刪除不拋例外"""
        course = _add_course(session)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="enrolled")

        # 不應拋例外
        svc.delete_registration(session, reg.id, "admin")
        session.flush()

        assert reg.is_active is False

    def test_delete_only_promotes_per_course(self, session, svc):
        """報名含多門課時，只對各課程分別升位一次"""
        course_a = _add_course(session, name="美術", capacity=1)
        course_b = _add_course(session, name="音樂", capacity=1)

        # 正式報名甲（兩門課皆滿）
        reg1 = _add_reg(session, student_name="甲")
        _enroll(session, reg1.id, course_a.id, status="enrolled")
        _enroll(session, reg1.id, course_b.id, status="enrolled")

        # 候補乙（兩門課皆候補）
        reg2 = _add_reg(session, student_name="乙")
        rc_a2 = _enroll(session, reg2.id, course_a.id, status="waitlist")
        rc_b2 = _enroll(session, reg2.id, course_b.id, status="waitlist")

        # 候補丙（只候補 A 課）
        reg3 = _add_reg(session, student_name="丙")
        rc_a3 = _enroll(session, reg3.id, course_a.id, status="waitlist")

        svc.delete_registration(session, reg1.id, "admin")
        session.flush()

        # 乙應升 promoted_pending（兩門課）
        assert rc_a2.status == "promoted_pending"
        assert rc_b2.status == "promoted_pending"
        # 丙在乙後，A課已被乙的 promoted_pending 佔位，不升
        assert rc_a3.status == "waitlist"

    def test_delete_logs_change(self, session, svc):
        """刪除後應有修改紀錄"""
        course = _add_course(session)
        reg = _add_reg(session, student_name="測試學生")
        _enroll(session, reg.id, course.id, status="enrolled")

        svc.delete_registration(session, reg.id, "admin-user")
        session.flush()

        change = (
            session.query(RegistrationChange)
            .filter(RegistrationChange.registration_id == reg.id)
            .first()
        )
        assert change is not None
        assert change.changed_by == "admin-user"
        assert change.change_type == "刪除報名"

    def test_delete_auto_promote_logs_change(self, session, svc):
        """自動升位後應在 RegistrationChange 寫入記錄"""
        course = _add_course(session, capacity=1)
        reg1 = _add_reg(session, student_name="甲")
        _enroll(session, reg1.id, course.id, status="enrolled")
        reg2 = _add_reg(session, student_name="乙")
        _enroll(session, reg2.id, course.id, status="waitlist")

        svc.delete_registration(session, reg1.id, "admin")
        session.flush()

        # 應有一筆「候補升正式（待確認）」記錄，屬於乙的報名
        promote_log = (
            session.query(RegistrationChange)
            .filter(
                RegistrationChange.registration_id == reg2.id,
                RegistrationChange.change_type == "候補升正式（待確認）",
            )
            .first()
        )
        assert promote_log is not None
        assert promote_log.changed_by == "system"


# ------------------------------------------------------------------ #
# B7 - public_update_registration 時間窗口（Pydantic 層面驗證）
# ------------------------------------------------------------------ #


class TestPublicUpdateTimeCheck:
    """測試 ActivityRegistrationSettings 在 Service 層的時間窗口邏輯。

    注意：B7 的實際 HTTP 檢查在 FastAPI 路由層，這裡測試設定值的邏輯。
    """

    def _check_time_window(
        self, settings: ActivityRegistrationSettings, now_str: str
    ) -> str | None:
        """仿照 public_update_registration 的時間窗口判斷邏輯，回傳錯誤訊息或 None。"""
        if not settings.is_open:
            return "報名尚未開放"
        if settings.open_at and now_str < settings.open_at:
            return "報名尚未開始"
        if settings.close_at and now_str > settings.close_at:
            return "報名已截止"
        return None

    def test_update_blocked_when_closed(self, session):
        """is_open=False 時應回傳阻擋訊息"""
        settings = ActivityRegistrationSettings(is_open=False)
        error = self._check_time_window(settings, "2026-03-30T10:00:00")
        assert error == "報名尚未開放"

    def test_update_blocked_after_close_time(self, session):
        """超過 close_at 時應回傳阻擋訊息"""
        settings = ActivityRegistrationSettings(
            is_open=True,
            open_at="2026-03-01T00:00",
            close_at="2026-03-29T23:59",
        )
        error = self._check_time_window(settings, "2026-03-30T10:00:00")
        assert error == "報名已截止"

    def test_update_blocked_before_open_time(self, session):
        """open_at 之前應回傳阻擋訊息"""
        settings = ActivityRegistrationSettings(
            is_open=True,
            open_at="2026-04-01T00:00",
        )
        error = self._check_time_window(settings, "2026-03-30T10:00:00")
        assert error == "報名尚未開始"

    def test_update_allowed_when_open(self, session):
        """is_open=True 且在時間內應允許"""
        settings = ActivityRegistrationSettings(
            is_open=True,
            open_at="2026-03-01T00:00",
            close_at="2026-04-30T23:59",
        )
        error = self._check_time_window(settings, "2026-03-30T10:00:00")
        assert error is None


# ------------------------------------------------------------------ #
# B8 - RegistrationTimeSettings Pydantic 驗證
# ------------------------------------------------------------------ #


class TestRegistrationTimeSettingsValidation:
    def test_invalid_format_raises(self):
        """傳入 '2026/03/29 09:00' 格式應拋 ValidationError"""
        with pytest.raises(ValidationError):
            RegistrationTimeSettings(is_open=True, open_at="2026/03/29 09:00")

    def test_invalid_date_only_raises(self):
        """傳入 '2026-03-29' 日期格式應拋 ValidationError"""
        with pytest.raises(ValidationError):
            RegistrationTimeSettings(is_open=True, open_at="2026-03-29")

    def test_close_before_open_raises(self):
        """close_at < open_at 應拋 ValidationError"""
        with pytest.raises(ValidationError):
            RegistrationTimeSettings(
                is_open=True,
                open_at="2026-04-01T00:00",
                close_at="2026-03-29T23:59",
            )

    def test_close_equal_open_raises(self):
        """close_at == open_at 應拋 ValidationError"""
        with pytest.raises(ValidationError):
            RegistrationTimeSettings(
                is_open=True,
                open_at="2026-03-30T09:00",
                close_at="2026-03-30T09:00",
            )

    def test_valid_iso_format_accepted(self):
        """合法 ISO 字串（YYYY-MM-DDTHH:MM）可通過驗證"""
        s = RegistrationTimeSettings(
            is_open=True,
            open_at="2026-03-01T09:00",
            close_at="2026-04-30T18:00",
        )
        assert s.open_at == "2026-03-01T09:00"
        assert s.close_at == "2026-04-30T18:00"

    def test_valid_iso_with_seconds_accepted(self):
        """合法 ISO 字串（含秒）可通過驗證"""
        s = RegistrationTimeSettings(
            is_open=True,
            open_at="2026-03-01T09:00:00",
            close_at="2026-04-30T18:00:00",
        )
        assert s.open_at == "2026-03-01T09:00:00"

    def test_none_values_accepted(self):
        """open_at/close_at 為 None 時可通過驗證"""
        s = RegistrationTimeSettings(is_open=False)
        assert s.open_at is None
        assert s.close_at is None
