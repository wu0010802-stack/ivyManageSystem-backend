"""Tests for services.announcement_publish_scheduler.tick."""

from datetime import datetime
from unittest.mock import patch

import pytest

from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    Employee,
)
from services.announcement_publish_scheduler import tick


@pytest.fixture
def admin_emp(test_db_session):
    emp = Employee(employee_id="E_ADMIN", name="admin", is_active=True, base_salary=0)
    test_db_session.add(emp)
    test_db_session.commit()
    return emp


def _mk_ann(session, emp_id, publish_at, has_parent=True):
    a = Announcement(title="T", content="C", created_by=emp_id, publish_at=publish_at)
    session.add(a)
    session.flush()
    if has_parent:
        session.add(AnnouncementParentRecipient(announcement_id=a.id, scope="all"))
        session.flush()
    return a


def test_tick_dispatches_window_only(test_db_session, admin_emp):
    """Only announcements with publish_at in (last_dispatched_at, now] should fire."""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    a_in = _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    _mk_ann(
        test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 58, 0)
    )  # before window
    _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 8, 1, 0))  # future
    _mk_ann(
        test_db_session,
        admin_emp.id,
        datetime(2026, 5, 29, 7, 59, 45),
        has_parent=False,
    )  # no parent
    test_db_session.commit()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push"
    ) as mock_fire:
        count = tick(test_db_session, now=now, last_dispatched_at=last)

    assert count == 1
    assert mock_fire.call_count == 1
    fired_ann = mock_fire.call_args.args[1]
    assert fired_ann.id == a_in.id


def test_tick_idempotent_within_same_window(test_db_session, admin_emp):
    """重跑同 (now, last_dispatched_at) 不重複推播；推進 last 後不再 fire。"""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    _mk_ann(test_db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    test_db_session.commit()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push"
    ) as mock_fire:
        c1 = tick(test_db_session, now=now, last_dispatched_at=last)
        c2 = tick(test_db_session, now=now, last_dispatched_at=now)

    assert c1 == 1
    assert c2 == 0
    assert mock_fire.call_count == 1
