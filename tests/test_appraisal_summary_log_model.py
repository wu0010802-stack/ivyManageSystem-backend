"""AppraisalSummaryLog model 基本約束 + cascade。"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, text

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
    AppraisalSummaryLog,
    CycleStatus,
    Grade,
    RoleGroup,
    Semester,
    SummaryLogAction,
    SummaryStatus,
)
from models.auth import User
from models.employee import Employee, EmployeeType


@pytest.fixture
def fk_session(test_db_session):
    """test_db_session + 啟用 SQLite FK 強制（讓 ondelete CASCADE 生效）。"""
    engine = test_db_session.get_bind()

    def _pragma_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "connect", _pragma_fk)
    test_db_session.execute(text("PRAGMA foreign_keys=ON"))
    yield test_db_session
    event.remove(engine, "connect", _pragma_fk)


def _make_actor(s):
    user = User(username="actor1", password_hash="x", role="admin")
    s.add(user)
    s.flush()
    return user


def _make_summary(s):
    emp = Employee(
        employee_id="E001",
        name="王小華",
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    s.add(emp)
    s.flush()
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    s.add(cycle)
    s.flush()
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    s.add(p)
    s.flush()
    summary = AppraisalSummary(
        participant_id=p.id,
        cycle_id=cycle.id,
        base_score=Decimal("75.6"),
        event_score_sum=Decimal("0"),
        total_score=Decimal("75.6"),
        grade=Grade.PASS,
        bonus_amount=Decimal("0"),
        status=SummaryStatus.DRAFT,
    )
    s.add(summary)
    s.flush()
    return cycle, p, summary


def test_summary_log_basic_insert(test_db_session):
    s = test_db_session
    actor = _make_actor(s)
    cycle, p, summary = _make_summary(s)
    log = AppraisalSummaryLog(
        summary_id=summary.id,
        action=SummaryLogAction.SIGN_SUPERVISOR,
        from_status=SummaryStatus.DRAFT,
        to_status=SummaryStatus.SUPERVISOR_SIGNED,
        actor_id=actor.id,
        actor_role_snapshot="supervisor",
    )
    s.add(log)
    s.flush()
    assert log.id is not None
    assert log.created_at is not None


def test_summary_log_cascade_on_summary_delete(fk_session):
    s = fk_session
    actor = _make_actor(s)
    cycle, p, summary = _make_summary(s)
    s.add(
        AppraisalSummaryLog(
            summary_id=summary.id,
            action=SummaryLogAction.COMMENT,
            actor_id=actor.id,
            actor_role_snapshot="admin",
            comment="test",
        )
    )
    s.flush()
    assert s.query(AppraisalSummaryLog).count() == 1
    s.delete(summary)
    s.flush()
    assert s.query(AppraisalSummaryLog).count() == 0


def test_summary_log_action_enum_complete(test_db_session):
    expected = {
        "SIGN_SUPERVISOR",
        "SIGN_ACCOUNTING",
        "FINALIZE",
        "REJECT",
        "COMMENT",
        "RECOMPUTE",
    }
    actual = {a.value for a in SummaryLogAction}
    assert actual == expected
