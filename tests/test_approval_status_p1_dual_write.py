"""P1 dual-write listener tests — covers ApprovalStatus enum + attribute listener.

P1 listener direction: is_approved (set) → status (mirror).
P2 PR will reverse the direction; this file gets new tests at that point.
"""

import pytest

from models.approval import ApprovalStatus, register_p1_listeners


class TestApprovalStatusEnum:
    def test_three_values(self):
        assert ApprovalStatus.PENDING.value == "pending"
        assert ApprovalStatus.APPROVED.value == "approved"
        assert ApprovalStatus.REJECTED.value == "rejected"

    def test_is_str_subclass(self):
        """str-mixin so SQLAlchemy String column round-trips cleanly."""
        assert isinstance(ApprovalStatus.PENDING, str)
        assert ApprovalStatus.PENDING == "pending"

    def test_no_extra_values(self):
        assert {m.value for m in ApprovalStatus} == {"pending", "approved", "rejected"}


from datetime import date

# Import the models package — this triggers register_p1_listeners().
import models  # noqa: F401
from models.employee import Employee
from models.leave import LeaveRecord
from models.overtime import OvertimeRecord, PunchCorrectionRequest


def _make_leave(session, **overrides):
    """Build a LeaveRecord with required fields filled."""
    emp_id = session.query(Employee).first().id
    defaults = dict(
        employee_id=emp_id,
        leave_type="sick",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 1),
        leave_hours=8.0,
    )
    defaults.update(overrides)
    return LeaveRecord(**defaults)


def _make_overtime(session, **overrides):
    emp_id = session.query(Employee).first().id
    defaults = dict(
        employee_id=emp_id,
        overtime_date=date(2026, 1, 1),
        overtime_type="weekday",
        hours=2.0,
    )
    defaults.update(overrides)
    return OvertimeRecord(**defaults)


def _make_punch(session, **overrides):
    emp_id = session.query(Employee).first().id
    defaults = dict(
        employee_id=emp_id,
        attendance_date=date(2026, 1, 1),
        correction_type="punch_in",
    )
    defaults.update(overrides)
    return PunchCorrectionRequest(**defaults)


@pytest.fixture
def session(test_db_session):
    """Reuse the conftest SQLite in-memory session; seed one employee."""
    emp = Employee(name="Test", employee_id="E_P1_TEST", hire_date=date(2024, 1, 1))
    test_db_session.add(emp)
    test_db_session.commit()
    yield test_db_session


@pytest.mark.parametrize("factory", [_make_leave, _make_overtime, _make_punch])
class TestP1ListenerSyncsIsApprovedToStatus:
    def test_set_true_mirrors_to_approved(self, session, factory):
        rec = factory(session)
        rec.is_approved = True
        assert rec.status == "approved"

    def test_set_false_mirrors_to_rejected(self, session, factory):
        rec = factory(session)
        rec.is_approved = False
        assert rec.status == "rejected"

    def test_set_none_mirrors_to_pending(self, session, factory):
        rec = factory(session)
        rec.is_approved = None
        assert rec.status == "pending"

    def test_transition_chain(self, session, factory):
        rec = factory(session)
        rec.is_approved = None
        assert rec.status == "pending"
        rec.is_approved = True
        assert rec.status == "approved"
        rec.is_approved = False
        assert rec.status == "rejected"
        rec.is_approved = None
        assert rec.status == "pending"

    def test_approval_status_property_returns_status(self, session, factory):
        rec = factory(session)
        rec.is_approved = True
        assert rec.approval_status == "approved"
        assert rec.approval_status == rec.status

    def test_default_is_pending_on_construction(self, session, factory):
        rec = factory(session)
        session.add(rec)
        session.flush()
        assert rec.status == "pending"
        assert rec.approval_status == "pending"

    def test_idempotency_no_write_when_already_aligned(self, session, factory):
        """Listener guard: setting is_approved to current value should not
        rewrite status."""
        from sqlalchemy import inspect as sa_inspect

        rec = factory(session)
        rec.is_approved = True
        session.add(rec)
        session.commit()
        # status is already 'approved'; setting is_approved=True again should be a no-op write.
        session.expire(rec)
        _ = rec.is_approved  # reload
        assert rec.status == "approved"
        rec.is_approved = True  # same value
        # Listener guard means status should NOT be in dirty history (added=()).
        status_history = sa_inspect(rec).attrs.status.history
        assert not status_history.added, (
            f"status should not be re-written by idempotency guard, "
            f"but got added={status_history.added}"
        )
