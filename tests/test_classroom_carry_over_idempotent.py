"""classroom_carry_over 重跑不 double-create 目標學期班級。"""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Classroom, Student
from models.academic_term import AcademicTerm
from services.term_subscribers.classroom_carry_over import handle


def _mk_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_carry_over_twice_no_double_create():
    s = _mk_session()
    old = AcademicTerm(
        school_year=114,
        semester=1,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
    )
    new = AcademicTerm(
        school_year=114,
        semester=2,
        start_date=date(2026, 2, 1),
        end_date=date(2026, 7, 31),
    )
    s.add_all([old, new])
    s.flush()
    cls = Classroom(name="星星班", school_year=114, semester=1, capacity=30)
    s.add(cls)
    s.flush()

    handle(old=old, new=new, session=s)
    s.flush()
    first = (
        s.query(Classroom)
        .filter(Classroom.school_year == 114, Classroom.semester == 2)
        .count()
    )

    handle(old=old, new=new, session=s)
    s.flush()
    second = (
        s.query(Classroom)
        .filter(Classroom.school_year == 114, Classroom.semester == 2)
        .count()
    )

    assert first == 1
    assert second == 1  # 第二次跑：目標學期已有班級 → 跳過
