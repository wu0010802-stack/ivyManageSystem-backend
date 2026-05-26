"""驗 remind_upcoming_comp_grants 推 LINE + stamp reminder_sent_at + 防重複。

fixture pattern 對齊 tests/test_offboarding_step_snapshot_leave.py：
  - db_session: SQLite in-memory + Base.metadata.create_all + swap 全域
  - employee_factory: Employee + db_session flush
  - user_factory: User + db_session flush（含 line_user_id 設定）
"""

import os
import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import models.overtime_comp_leave_grant  # noqa: F401 — 確保 Base.metadata 含此表
import models.unused_leave_payout_log  # noqa: F401 — 確保 Base.metadata 含此表
from models.database import Base, Employee, User

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory test session（對齊 offboarding step test pattern）。"""
    db_path = tmp_path / "remind_upcoming_comp_grants.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def employee_factory(db_session):
    """建立測試員工並 flush 取得 id。"""

    def _factory(
        *,
        is_active: bool = True,
        hire_date=date(2020, 1, 1),
    ) -> Employee:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"RMD{_counter:04d}",
            name=f"提醒測試員工{_counter}",
            hire_date=hire_date,
            is_active=is_active,
            base_salary=30000,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

    return _factory


@pytest.fixture
def user_factory(db_session):
    """建立測試 User（可指定 line_user_id）並 flush。"""

    def _factory(*, employee_id: int, line_user_id: str | None = None) -> User:
        global _counter
        _counter += 1
        user = User(
            username=f"rmd_user_{_counter}",
            password_hash="x",
            role="teacher",
            employee_id=employee_id,
            line_user_id=line_user_id,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _factory


@pytest.fixture
def grant_factory(db_session):
    """建立 OvertimeCompLeaveGrant 並 flush。

    使用 raw SQL 插入 overtime_records（迴避複雜 FK 關聯；
    對齊 test_leave_quota_expiry_helpers.py 的做法）。
    """
    from sqlalchemy import text as _text
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    def _factory(
        *,
        employee_id: int,
        granted_hours: float = 4.0,
        consumed_hours: float = 0.0,
        expires_at: date,
        status: str = "active",
        reminder_sent_at=None,
    ) -> OvertimeCompLeaveGrant:
        global _counter
        _counter += 1
        day = (_counter % 28) + 1
        # 用 raw SQL 插入 overtime_records 跳過 ORM FK 驗證
        # use_comp_leave NOT NULL DEFAULT 0（Boolean → SQLite 0/1）
        db_session.execute(
            _text(
                "INSERT INTO overtime_records"
                " (employee_id, overtime_date, overtime_type, hours, status,"
                "  use_comp_leave, comp_leave_granted)"
                " VALUES (:emp, :dt, 'weekday', :h, 'approved', 1, 1)"
            ),
            {"emp": employee_id, "dt": f"2025-01-{day:02d}", "h": granted_hours},
        )
        db_session.flush()
        # 取剛插入的 overtime_record id
        ot_id = db_session.execute(_text("SELECT last_insert_rowid()")).scalar()

        grant = OvertimeCompLeaveGrant(
            overtime_record_id=ot_id,
            employee_id=employee_id,
            granted_hours=granted_hours,
            consumed_hours=consumed_hours,
            granted_at=date(2025, 1, 1),
            expires_at=expires_at,
            status=status,
            reminder_sent_at=reminder_sent_at,
        )
        db_session.add(grant)
        db_session.flush()
        return grant

    return _factory


# ── 測試案例 ───────────────────────────────────────────────────────────────────


def _make_mock_line_service(push_return: bool = True) -> MagicMock:
    svc = MagicMock()
    svc.push_flex_to_user.return_value = push_return
    return svc


def test_no_upcoming_grants_no_op(db_session):
    """無任何 grant → 回傳 0 不推 LINE。"""
    import services.leave_quota_expiry.comp_grant_reminder as mod

    mock_svc = _make_mock_line_service()
    mod._line_service = mock_svc

    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    summary = remind_upcoming_comp_grants(date(2026, 4, 1), db_session)
    assert summary == {"reminded_employees": 0, "skipped_no_line": 0}
    mock_svc.push_flex_to_user.assert_not_called()


def test_reminds_employee_with_line_user_id(
    db_session, employee_factory, user_factory, grant_factory
):
    """active grant 7 天內到期 + emp 有 line_user_id → push LINE + stamp reminder_sent_at。"""
    today = date(2026, 4, 1)
    emp = employee_factory(is_active=True)
    user = user_factory(employee_id=emp.id, line_user_id="Uxxx_abc123")
    expires_at = today + timedelta(days=5)
    grant = grant_factory(
        employee_id=emp.id,
        granted_hours=4.0,
        consumed_hours=1.0,
        expires_at=expires_at,
    )

    import services.leave_quota_expiry.comp_grant_reminder as mod

    mock_svc = _make_mock_line_service(push_return=True)
    mod._line_service = mock_svc

    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    summary = remind_upcoming_comp_grants(today, db_session)

    assert summary["reminded_employees"] == 1
    assert summary["skipped_no_line"] == 0

    # grant.reminder_sent_at 已 stamp（同 session identity map 直接檢查；
    # 不呼叫 refresh()，refresh 會強制 DB re-read 丟失未 commit 的 in-memory 變更）
    assert grant.reminder_sent_at is not None

    # push_flex_to_user 被呼叫：驗 line_user_id、alt_text 含時數與到期日、flex bubble 結構
    mock_svc.push_flex_to_user.assert_called_once()
    call_args = mock_svc.push_flex_to_user.call_args
    assert call_args[0][0] == "Uxxx_abc123"  # line_user_id
    flex_content = call_args[0][1]
    alt_text = call_args[0][2]
    assert "3.0" in alt_text or "3" in alt_text  # 4.0 - 1.0 = 3.0 小時
    assert expires_at.isoformat() in alt_text
    # flex bubble 基本結構驗證
    assert flex_content["type"] == "bubble"
    assert "header" in flex_content
    assert "body" in flex_content


def test_skipped_when_no_line_user_id(
    db_session, employee_factory, user_factory, grant_factory
):
    """emp 有 user 但無 line_user_id → skipped_no_line+1，reminder_sent_at 不 set。"""
    today = date(2026, 4, 1)
    emp = employee_factory(is_active=True)
    user_factory(employee_id=emp.id, line_user_id=None)  # 無 LINE
    grant = grant_factory(
        employee_id=emp.id,
        granted_hours=4.0,
        expires_at=today + timedelta(days=3),
    )

    import services.leave_quota_expiry.comp_grant_reminder as mod

    mock_svc = _make_mock_line_service()
    mod._line_service = mock_svc

    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    summary = remind_upcoming_comp_grants(today, db_session)

    assert summary["reminded_employees"] == 0
    assert summary["skipped_no_line"] == 1

    # reminder_sent_at 不 set（同 session identity map 直接檢查，不 refresh）
    assert grant.reminder_sent_at is None
    mock_svc.push_flex_to_user.assert_not_called()


def test_skipped_when_no_user_at_all(db_session, employee_factory, grant_factory):
    """emp 無對應 User 記錄 → skipped_no_line+1。"""
    today = date(2026, 4, 1)
    emp = employee_factory(is_active=True)
    # 不建 User
    grant = grant_factory(
        employee_id=emp.id,
        granted_hours=4.0,
        expires_at=today + timedelta(days=3),
    )

    import services.leave_quota_expiry.comp_grant_reminder as mod

    mock_svc = _make_mock_line_service()
    mod._line_service = mock_svc

    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    summary = remind_upcoming_comp_grants(today, db_session)

    assert summary["skipped_no_line"] == 1
    assert grant.reminder_sent_at is None


def test_does_not_reremind_when_already_sent(
    db_session, employee_factory, user_factory, grant_factory
):
    """grant.reminder_sent_at 已 set → 不重推，不計入 reminded_employees。"""
    today = date(2026, 4, 1)
    emp = employee_factory(is_active=True)
    user_factory(employee_id=emp.id, line_user_id="Uxxx_already")
    already_sent = datetime(2026, 3, 30, 10, 0, 0)
    grant_factory(
        employee_id=emp.id,
        granted_hours=4.0,
        expires_at=today + timedelta(days=5),
        reminder_sent_at=already_sent,
    )

    import services.leave_quota_expiry.comp_grant_reminder as mod

    mock_svc = _make_mock_line_service()
    mod._line_service = mock_svc

    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    summary = remind_upcoming_comp_grants(today, db_session)

    assert summary["reminded_employees"] == 0
    assert summary["skipped_no_line"] == 0
    mock_svc.push_flex_to_user.assert_not_called()


def test_expired_grant_not_reminded(
    db_session, employee_factory, user_factory, grant_factory
):
    """status='expired' grant 不提醒（只撈 active）。"""
    today = date(2026, 4, 1)
    emp = employee_factory(is_active=True)
    user_factory(employee_id=emp.id, line_user_id="Uxxx_expired")
    grant_factory(
        employee_id=emp.id,
        granted_hours=4.0,
        expires_at=today + timedelta(days=3),
        status="expired",
    )

    import services.leave_quota_expiry.comp_grant_reminder as mod

    mock_svc = _make_mock_line_service()
    mod._line_service = mock_svc

    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    summary = remind_upcoming_comp_grants(today, db_session)

    assert summary["reminded_employees"] == 0
    mock_svc.push_flex_to_user.assert_not_called()


def test_grant_past_deadline_not_reminded(
    db_session, employee_factory, user_factory, grant_factory
):
    """expires_at > today + 7d 的 grant 不提醒（超出 window）。"""
    today = date(2026, 4, 1)
    emp = employee_factory(is_active=True)
    user_factory(employee_id=emp.id, line_user_id="Uxxx_far")
    grant_factory(
        employee_id=emp.id,
        granted_hours=4.0,
        expires_at=today + timedelta(days=10),  # 10 天後，超出 7 天 window
    )

    import services.leave_quota_expiry.comp_grant_reminder as mod

    mock_svc = _make_mock_line_service()
    mod._line_service = mock_svc

    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    summary = remind_upcoming_comp_grants(today, db_session)

    assert summary["reminded_employees"] == 0


def test_multiple_grants_same_employee_single_push(
    db_session, employee_factory, user_factory, grant_factory
):
    """同一員工有多筆 active grant 7 天內到期 → 只推一次 LINE，全部 stamp。"""
    today = date(2026, 4, 1)
    emp = employee_factory(is_active=True)
    user_factory(employee_id=emp.id, line_user_id="Uxxx_multi")
    grant1 = grant_factory(
        employee_id=emp.id, granted_hours=4.0, expires_at=today + timedelta(days=2)
    )
    grant2 = grant_factory(
        employee_id=emp.id, granted_hours=2.0, expires_at=today + timedelta(days=5)
    )

    import services.leave_quota_expiry.comp_grant_reminder as mod

    mock_svc = _make_mock_line_service()
    mod._line_service = mock_svc

    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    summary = remind_upcoming_comp_grants(today, db_session)

    assert summary["reminded_employees"] == 1
    mock_svc.push_flex_to_user.assert_called_once()

    # 兩筆 grant 皆 stamp（同 session identity map 直接檢查，不 refresh）
    assert grant1.reminder_sent_at is not None
    assert grant2.reminder_sent_at is not None
