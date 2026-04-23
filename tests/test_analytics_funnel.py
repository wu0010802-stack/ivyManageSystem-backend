"""funnel_service tests"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.classroom import Student, Classroom
from models.recruitment import RecruitmentVisit
from models.activity import ParentInquiry
from services.analytics.funnel_service import (
    count_visit_side_stages,
    summarize_no_deposit_reasons,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _add_visit(
    session,
    *,
    month,
    has_deposit=False,
    enrolled=False,
    source="walk_in",
    grade="小班",
    no_deposit_reason=None,
    child_name="幼生A",
):
    v = RecruitmentVisit(
        month=month,
        child_name=child_name,
        source=source,
        grade=grade,
        has_deposit=has_deposit,
        enrolled=enrolled,
        no_deposit_reason=no_deposit_reason,
    )
    session.add(v)
    session.commit()
    return v


def _add_inquiry(session, *, created_at):
    # ParentInquiry 欄位：name, phone, question, is_read
    inq = ParentInquiry(
        name="家長A",
        phone="0900",
        question="詢問",
        created_at=created_at,
        is_read=False,
    )
    session.add(inq)
    session.commit()
    return inq


def test_visit_count_basic(session):
    # 3 visits in 2026-03
    _add_visit(session, month="115.03", has_deposit=False, enrolled=False)
    _add_visit(session, month="115.03", has_deposit=True, enrolled=False)
    _add_visit(session, month="115.03", has_deposit=True, enrolled=True)
    # 1 inquiry in 2026-03
    _add_inquiry(session, created_at=datetime(2026, 3, 5, 10, 0))

    result = count_visit_side_stages(
        session, start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)
    )
    # lead = 3 visits + 1 inquiry = 4
    assert result["lead"] == 4
    assert result["deposit"] == 2
    assert result["enrolled"] == 1


def test_visit_count_filters_by_grade_and_source(session):
    _add_visit(
        session,
        month="115.03",
        grade="小班",
        source="walk_in",
        has_deposit=True,
        enrolled=True,
    )
    _add_visit(
        session,
        month="115.03",
        grade="中班",
        source="walk_in",
        has_deposit=True,
        enrolled=True,
    )
    _add_visit(
        session,
        month="115.03",
        grade="小班",
        source="referral",
        has_deposit=True,
        enrolled=True,
    )

    r = count_visit_side_stages(
        session,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        grade_filter="小班",
    )
    assert r["enrolled"] == 2

    r2 = count_visit_side_stages(
        session,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        grade_filter="小班",
        source_filter="walk_in",
    )
    assert r2["enrolled"] == 1


def test_visit_invalid_month_skipped(session):
    _add_visit(session, month="bad-month", has_deposit=True, enrolled=True)
    _add_visit(session, month="115.03", has_deposit=True, enrolled=True)

    r = count_visit_side_stages(
        session, start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)
    )
    assert r["enrolled"] == 1  # 'bad-month' 略過


def test_no_deposit_reasons(session):
    _add_visit(session, month="115.03", has_deposit=False, no_deposit_reason="考慮中")
    _add_visit(session, month="115.03", has_deposit=False, no_deposit_reason="考慮中")
    _add_visit(session, month="115.03", has_deposit=False, no_deposit_reason="選擇他校")
    _add_visit(session, month="115.03", has_deposit=True, no_deposit_reason=None)
    _add_visit(
        session, month="115.03", has_deposit=False, no_deposit_reason=None
    )  # 沒填原因，不計入

    reasons = summarize_no_deposit_reasons(
        session, start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)
    )
    by_reason = {r["reason"]: r["count"] for r in reasons}
    assert by_reason == {"考慮中": 2, "選擇他校": 1}


def _add_student(
    session,
    *,
    name,
    lifecycle_status,
    enrollment_date=None,
    withdrawal_date=None,
    classroom=None,
):
    # Student.classroom_id 是 FK；classroom 參數接受 Classroom 物件或 None
    s = Student(
        student_id=f"S-{name}",  # student_id 是 non-nullable unique 欄位
        name=name,
        lifecycle_status=lifecycle_status,
        enrollment_date=enrollment_date,
        withdrawal_date=withdrawal_date,
        classroom_id=classroom.id if classroom is not None else None,
        is_active=lifecycle_status in ("active", "on_leave", "enrolled"),
    )
    session.add(s)
    session.commit()
    return s


def _add_classroom(session, *, name, grade_name=None):
    """Create a Classroom and (optionally) wire to a ClassGrade.

    grade_name: if provided, ensures a ClassGrade with this exact name exists
    and links the new Classroom to it.
    """
    import uuid
    from models.classroom import ClassGrade

    grade_id = None
    if grade_name:
        cg = session.query(ClassGrade).filter(ClassGrade.name == grade_name).first()
        if cg is None:
            cg = ClassGrade(name=grade_name, is_active=True)
            session.add(cg)
            session.commit()
        grade_id = cg.id

    # Classroom 有 UniqueConstraint(school_year, semester, name)；用 uuid 確保唯一
    suffix = uuid.uuid4().hex[:6]
    c = Classroom(name=f"{name}-{suffix}", is_active=True, grade_id=grade_id)
    session.add(c)
    session.commit()
    return c


def test_student_active_counts_in_range(session):
    from services.analytics.funnel_service import count_student_side_stages

    cls = _add_classroom(session, name="小班A")

    # 2026-03 入學、active
    _add_student(
        session,
        name="A",
        lifecycle_status="active",
        enrollment_date=date(2026, 3, 5),
        classroom=cls,
    )
    # 2026-03 入學但已退學
    _add_student(
        session,
        name="B",
        lifecycle_status="withdrawn",
        enrollment_date=date(2026, 3, 6),
        withdrawal_date=date(2026, 3, 20),
        classroom=cls,
    )
    # 2026-02 入學（範圍外）
    _add_student(
        session,
        name="C",
        lifecycle_status="active",
        enrollment_date=date(2026, 2, 28),
        classroom=cls,
    )

    r = count_student_side_stages(
        session,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        today=date(2026, 4, 23),
    )
    # active = 入學日落在區間 = A + B = 2
    assert r["active"] == 2


def test_retention_windows_boundaries(session):
    from services.analytics.funnel_service import count_student_side_stages

    cls = _add_classroom(session, name="小班A")

    # 入學日 2026-03-01；today 2026-04-01 → 剛滿 31 天 ≥ 30，1m 留存 ✓
    _add_student(
        session,
        name="滿月",
        lifecycle_status="active",
        enrollment_date=date(2026, 3, 1),
        classroom=cls,
    )
    # 入學日 2026-03-15；today 2026-04-01 → 17 天，1m 留存 ✗
    _add_student(
        session,
        name="未滿月",
        lifecycle_status="active",
        enrollment_date=date(2026, 3, 15),
        classroom=cls,
    )
    # 入學日 2026-03-01 + 退學日 2026-03-20（< 30 天就退）→ 1m 留存 ✗
    _add_student(
        session,
        name="未滿月退",
        lifecycle_status="withdrawn",
        enrollment_date=date(2026, 3, 1),
        withdrawal_date=date(2026, 3, 20),
        classroom=cls,
    )

    r = count_student_side_stages(
        session,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        today=date(2026, 4, 1),
    )
    assert r["active"] == 3
    assert r["retained_1m"] == 1
    assert r["retained_6m"] == 0


def test_retained_6m(session):
    from services.analytics.funnel_service import count_student_side_stages

    cls = _add_classroom(session, name="小班A")
    # 2025-09-01 入學；today 2026-04-23 → 超過 180 天 ✓
    _add_student(
        session,
        name="老學生",
        lifecycle_status="active",
        enrollment_date=date(2025, 9, 1),
        classroom=cls,
    )

    r = count_student_side_stages(
        session,
        start_date=date(2025, 9, 1),
        end_date=date(2025, 9, 30),
        today=date(2026, 4, 23),
    )
    assert r["retained_6m"] == 1


def test_by_source_visit_side_only(session):
    from services.analytics.funnel_service import slice_by_source

    _add_visit(
        session, month="115.03", source="walk_in", has_deposit=True, enrolled=True
    )
    _add_visit(
        session, month="115.03", source="walk_in", has_deposit=False, enrolled=False
    )
    _add_visit(
        session, month="115.03", source="referral", has_deposit=True, enrolled=True
    )

    rows = slice_by_source(
        session, start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)
    )
    by_source = {r["source"]: r for r in rows}
    assert by_source["walk_in"]["lead"] == 2
    assert by_source["walk_in"]["enrolled"] == 1
    assert by_source["referral"]["lead"] == 1
    assert by_source["referral"]["enrolled"] == 1
    # 轉換率
    assert by_source["walk_in"]["conversion"] == pytest.approx(0.5)
    assert by_source["referral"]["conversion"] == pytest.approx(1.0)


def test_by_grade_includes_student_side(session):
    from services.analytics.funnel_service import slice_by_grade

    cls = _add_classroom(session, name="小班A", grade_name="小班")

    _add_visit(session, month="115.03", grade="小班", has_deposit=True, enrolled=True)
    _add_student(
        session,
        name="X",
        lifecycle_status="active",
        enrollment_date=date(2026, 3, 10),
        classroom=cls,
    )

    rows = slice_by_grade(
        session,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        today=date(2026, 4, 23),
    )
    by_grade = {r["grade"]: r for r in rows}
    # 小班：visit 端 lead=1 enrolled=1；student 端 active=1
    assert by_grade["小班"]["lead"] == 1
    assert by_grade["小班"]["enrolled"] == 1
    assert by_grade["小班"]["active"] == 1
