"""tests/test_recruitment_intake_plan.py — 新生名額規劃模型 + 彙總純函式。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import (
    ClassGrade,
    Student,
    LIFECYCLE_ENROLLED,
    LIFECYCLE_WITHDRAWN,
)
from models.recruitment import RecruitmentVisit, GradeIntakeTarget


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def test_model_columns_and_target_table(session):
    grade = ClassGrade(name="中班", sort_order=2)
    session.add(grade)
    session.flush()

    v = RecruitmentVisit(
        month="115.03",
        child_name="王小寶",
        has_deposit=True,
        provisional_grade_id=grade.id,
        target_school_year=115,
        target_semester=1,
    )
    session.add(v)

    t = GradeIntakeTarget(
        grade_id=grade.id, school_year=115, semester=1, target_seats=30
    )
    session.add(t)
    session.flush()

    got = session.query(RecruitmentVisit).first()
    assert got.provisional_grade_id == grade.id
    assert got.target_school_year == 115
    assert got.target_semester == 1
    assert session.query(GradeIntakeTarget).first().target_seats == 30


from services.recruitment_intake_plan import compute_intake_plan


def _grade(session, name, order):
    g = ClassGrade(name=name, sort_order=order)
    session.add(g)
    session.flush()
    return g


def test_compute_intake_plan_counts(session):
    mid = _grade(session, "中班", 2)
    big = _grade(session, "大班", 3)
    session.add(
        GradeIntakeTarget(grade_id=mid.id, school_year=115, semester=1, target_seats=30)
    )
    session.flush()

    # 2 筆已保留（未轉換）中班
    for nm in ("甲", "乙"):
        session.add(
            RecruitmentVisit(
                month="115.03",
                child_name=nm,
                has_deposit=True,
                provisional_grade_id=mid.id,
                target_school_year=115,
                target_semester=1,
                enrolled=False,
            )
        )
    # 1 筆已轉換中班 visit + 對應 Student（不應再算進「保留」，要算「註冊」）
    conv = RecruitmentVisit(
        month="115.03",
        child_name="丙",
        has_deposit=True,
        provisional_grade_id=mid.id,
        target_school_year=115,
        target_semester=1,
        enrolled=True,
    )
    session.add(conv)
    session.flush()
    # 顯式給 student_id（nullable=False；正常由 before_flush listener 自動填，
    # 此處顯式設定以免測試依賴 listener 內部行為）
    session.add(
        Student(
            student_id="115T001",
            name="丙",
            enrollment_school_year=115,
            enrollment_seq=1,
            lifecycle_status=LIFECYCLE_ENROLLED,
            recruitment_visit_id=conv.id,
        )
    )
    session.flush()

    rows = compute_intake_plan(session, school_year=115, semester=1)
    by_grade = {r["grade_id"]: r for r in rows}

    assert by_grade[mid.id]["reserved_count"] == 2
    assert by_grade[mid.id]["enrolled_count"] == 1
    assert by_grade[mid.id]["target_seats"] == 30
    assert by_grade[mid.id]["remaining"] == 27  # 30 - 2 - 1
    assert by_grade[mid.id]["over_capacity"] is False
    # 大班無 target、無保留/註冊 → target 0、remaining 0
    assert by_grade[big.id]["target_seats"] == 0
    assert by_grade[big.id]["remaining"] == 0


def test_over_capacity_flag(session):
    mid = _grade(session, "中班", 2)
    session.add(
        GradeIntakeTarget(grade_id=mid.id, school_year=115, semester=1, target_seats=1)
    )
    for nm in ("甲", "乙"):
        session.add(
            RecruitmentVisit(
                month="115.03",
                child_name=nm,
                has_deposit=True,
                provisional_grade_id=mid.id,
                target_school_year=115,
                target_semester=1,
                enrolled=False,
            )
        )
    session.flush()
    rows = {
        r["grade_id"]: r
        for r in compute_intake_plan(session, school_year=115, semester=1)
    }
    assert rows[mid.id]["over_capacity"] is True
    assert rows[mid.id]["remaining"] == -1


def test_terminal_lifecycle_excluded_from_enrolled(session):
    """終態（退學/轉出/畢業）的 Student 不算進 enrolled。"""
    mid = _grade(session, "中班", 2)
    session.add(
        GradeIntakeTarget(grade_id=mid.id, school_year=115, semester=1, target_seats=30)
    )
    conv = RecruitmentVisit(
        month="115.03",
        child_name="退",
        has_deposit=True,
        provisional_grade_id=mid.id,
        target_school_year=115,
        target_semester=1,
        enrolled=True,
    )
    session.add(conv)
    session.flush()
    session.add(
        Student(
            student_id="115T009",
            name="退",
            enrollment_school_year=115,
            enrollment_seq=9,
            lifecycle_status=LIFECYCLE_WITHDRAWN,
            recruitment_visit_id=conv.id,
        )
    )
    session.flush()
    rows = {
        r["grade_id"]: r
        for r in compute_intake_plan(session, school_year=115, semester=1)
    }
    assert rows[mid.id]["enrolled_count"] == 0
    assert rows[mid.id]["reserved_count"] == 0  # enrolled=True 不算保留
    assert rows[mid.id]["remaining"] == 30


from models.recruitment import RecruitmentEventLog
from services.recruitment_intake_plan import (
    IntakePlanError,
    set_provisional_seat,
    upsert_intake_targets,
)


def test_set_provisional_seat_requires_deposit(session):
    mid = _grade(session, "中班", 2)
    v = RecruitmentVisit(month="115.03", child_name="甲", has_deposit=False)
    session.add(v)
    session.flush()
    with pytest.raises(IntakePlanError):
        set_provisional_seat(
            session,
            visit_id=v.id,
            provisional_grade_id=mid.id,
            target_school_year=115,
            target_semester=1,
            actor_user_id=None,
        )


def test_set_then_release_provisional_seat(session):
    mid = _grade(session, "中班", 2)
    v = RecruitmentVisit(month="115.03", child_name="甲", has_deposit=True)
    session.add(v)
    session.flush()

    set_provisional_seat(
        session,
        visit_id=v.id,
        provisional_grade_id=mid.id,
        target_school_year=115,
        target_semester=1,
        actor_user_id=7,
    )
    session.flush()
    got = session.query(RecruitmentVisit).get(v.id)
    assert got.provisional_grade_id == mid.id
    assert got.target_school_year == 115
    assert (
        session.query(RecruitmentEventLog).filter_by(event_type="seat_reserved").count()
        == 1
    )

    # 釋放
    set_provisional_seat(
        session,
        visit_id=v.id,
        provisional_grade_id=None,
        target_school_year=None,
        target_semester=None,
        actor_user_id=7,
    )
    session.flush()
    got = session.query(RecruitmentVisit).get(v.id)
    assert got.provisional_grade_id is None
    assert (
        session.query(RecruitmentEventLog).filter_by(event_type="seat_released").count()
        == 1
    )


def test_upsert_intake_targets(session):
    mid = _grade(session, "中班", 2)
    upsert_intake_targets(
        session,
        school_year=115,
        semester=1,
        targets=[{"grade_id": mid.id, "target_seats": 25}],
    )
    session.flush()
    assert (
        session.query(GradeIntakeTarget).filter_by(grade_id=mid.id).one().target_seats
        == 25
    )
    # 再 upsert 同鍵 → 更新而非新增
    upsert_intake_targets(
        session,
        school_year=115,
        semester=1,
        targets=[{"grade_id": mid.id, "target_seats": 40}],
    )
    session.flush()
    assert session.query(GradeIntakeTarget).filter_by(grade_id=mid.id).count() == 1
    assert (
        session.query(GradeIntakeTarget).filter_by(grade_id=mid.id).one().target_seats
        == 40
    )
