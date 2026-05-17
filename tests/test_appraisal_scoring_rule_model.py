"""AppraisalScoringRule + AppraisalManualEventCount model 基本約束測試。"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from models.appraisal import (
    AppraisalCycle,
    AppraisalManualEventCount,
    AppraisalParticipant,
    AppraisalScoringRule,
    CycleStatus,
    RoleGroup,
    ScoreItemCode,
    Semester,
)
from models.employee import Employee, EmployeeType


@pytest.fixture
def fk_session(test_db_session):
    """test_db_session + 啟用 SQLite FK 強制（讓 ondelete CASCADE 生效）。"""
    engine = test_db_session.get_bind()

    def _pragma_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "connect", _pragma_fk)
    # 對當前 connection 也立即生效
    test_db_session.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))
    yield test_db_session
    event.remove(engine, "connect", _pragma_fk)


def _make_cycle(session, year=114):
    cycle = AppraisalCycle(
        academic_year=year,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    session.add(cycle)
    session.flush()
    return cycle


def test_scoring_rule_unique_item_code_effective_from(test_db_session):
    s = test_db_session
    s.add(
        AppraisalScoringRule(
            item_code=ScoreItemCode.LATE_EARLY.value,
            effective_from=date(2026, 1, 1),
            rule_type="PER_UNIT",
            rule_config={"per_unit_delta": -0.25},
        )
    )
    s.flush()
    s.add(
        AppraisalScoringRule(
            item_code=ScoreItemCode.LATE_EARLY.value,
            effective_from=date(2026, 1, 1),
            rule_type="PER_UNIT",
            rule_config={"per_unit_delta": -0.5},
        )
    )
    with pytest.raises(IntegrityError):
        s.flush()


def test_scoring_rule_two_versions_different_date_ok(test_db_session):
    s = test_db_session
    s.add(
        AppraisalScoringRule(
            item_code=ScoreItemCode.LATE_EARLY.value,
            effective_from=date(2026, 1, 1),
            rule_type="PER_UNIT",
            rule_config={"per_unit_delta": -0.25},
        )
    )
    s.add(
        AppraisalScoringRule(
            item_code=ScoreItemCode.LATE_EARLY.value,
            effective_from=date(2026, 7, 1),
            rule_type="PER_UNIT",
            rule_config={"per_unit_delta": -0.5},
        )
    )
    s.flush()


def test_manual_event_count_unique_triple(test_db_session):
    s = test_db_session
    cycle = _make_cycle(s)
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=1,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    s.add(p)
    s.flush()
    s.add(
        AppraisalManualEventCount(
            cycle_id=cycle.id,
            participant_id=p.id,
            item_code=ScoreItemCode.SCHOOL_MEETING_ABSENCE.value,
            count=Decimal("2"),
        )
    )
    s.flush()
    s.add(
        AppraisalManualEventCount(
            cycle_id=cycle.id,
            participant_id=p.id,
            item_code=ScoreItemCode.SCHOOL_MEETING_ABSENCE.value,
            count=Decimal("3"),
        )
    )
    with pytest.raises(IntegrityError):
        s.flush()


def test_manual_event_count_cascade_on_cycle_delete(fk_session):
    s = fk_session
    emp = Employee(
        employee_id="E001",
        name="王雅玲",
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    s.add(emp)
    s.flush()
    cycle = _make_cycle(s)
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    s.add(p)
    s.flush()
    s.add(
        AppraisalManualEventCount(
            cycle_id=cycle.id,
            participant_id=p.id,
            item_code=ScoreItemCode.OTHER.value,
            count=Decimal("1"),
        )
    )
    s.flush()
    s.delete(cycle)
    s.flush()
    assert s.query(AppraisalManualEventCount).count() == 0
