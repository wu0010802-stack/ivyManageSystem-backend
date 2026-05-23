"""classroom_carry_over subscriber 單元測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Classroom, Student
from models.academic_term import AcademicTerm
from services.term_subscribers.classroom_carry_over import handle
from utils.term_events import reset_handlers_for_tests


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory test session（swap base_module 全域 engine pattern）。"""
    db_path = tmp_path / "term.sqlite"
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


@pytest.fixture(autouse=True)
def _reset():
    reset_handlers_for_tests()
    yield
    reset_handlers_for_tests()


def _make_term(db_session, sy, sem, sd, ed, is_current=False):
    t = AcademicTerm(
        school_year=sy,
        semester=sem,
        start_date=sd,
        end_date=ed,
        is_current=is_current,
    )
    db_session.add(t)
    db_session.flush()
    return t


def _make_classroom(db_session, sy, sem, name="ABC"):
    cls = Classroom(
        name=name,
        school_year=sy,
        semester=sem,
        capacity=30,
    )
    db_session.add(cls)
    db_session.flush()
    return cls


def _make_student(db_session, classroom_id, student_id, is_active=True):
    s = Student(
        student_id=student_id,
        name=f"學生{student_id}",
        gender="M",
        birthday=date(2020, 1, 1),
        classroom_id=classroom_id,
        is_active=is_active,
    )
    db_session.add(s)
    db_session.flush()
    return s


class TestClassroomCarryOver:
    def test_initial_set_current_no_op(self, db_session):
        """old=None：跳過 carry-over，no exception。"""
        new = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31), True)
        handle(old=None, new=new, session=db_session)
        # 無 classroom 被建
        assert db_session.query(Classroom).count() == 0

    def test_same_year_1_to_2_copies_classroom_and_moves_students(self, db_session):
        """同學年 1→2：classroom 複製 + active student 遷移。"""
        old = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31))
        new = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        cls = _make_classroom(db_session, 114, 1, name="星星班")
        s1 = _make_student(db_session, cls.id, "114-A-01", is_active=True)
        s2 = _make_student(db_session, cls.id, "114-A-02", is_active=True)
        inactive = _make_student(db_session, cls.id, "114-A-03", is_active=False)

        handle(old=old, new=new, session=db_session)

        # 新 classroom 建出
        new_classrooms = (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 114, Classroom.semester == 2)
            .all()
        )
        assert len(new_classrooms) == 1
        new_cls = new_classrooms[0]
        assert new_cls.name == "星星班"
        assert new_cls.id != cls.id

        # active student 遷移
        db_session.refresh(s1)
        db_session.refresh(s2)
        db_session.refresh(inactive)
        assert s1.classroom_id == new_cls.id
        assert s2.classroom_id == new_cls.id
        # inactive 不遷移
        assert inactive.classroom_id == cls.id

    def test_cross_year_2_to_1_no_op(self, db_session, caplog):
        """跨學年 114-2 → 115-1：classroom 不動 + log info。"""
        import logging

        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        cls = _make_classroom(db_session, 114, 2, name="月亮班")

        with caplog.at_level(logging.INFO):
            handle(old=old, new=new, session=db_session)

        # 新學期沒有 classroom 被建
        assert (
            db_session.query(Classroom).filter(Classroom.school_year == 115).count()
            == 0
        )
        assert any("跨學年" in r.message for r in caplog.records)

    def test_empty_old_term_classrooms_noop(self, db_session):
        """上學期 0 classroom 時 early return。"""
        old = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31))
        new = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        # 沒有 classroom
        handle(old=old, new=new, session=db_session)
        assert db_session.query(Classroom).count() == 0

    def test_atypical_jump_logs_warning(self, db_session, caplog):
        """跳級切換 113-2 → 115-1：no-op + warning log。"""
        import logging

        old = _make_term(db_session, 113, 2, date(2025, 2, 1), date(2025, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))

        with caplog.at_level(logging.WARNING):
            handle(old=old, new=new, session=db_session)

        assert any("非典型切換" in r.message for r in caplog.records)

    def test_copies_classroom_full_fields(self, db_session):
        """複製 classroom 時 head/assistant/art teacher、grade_id、capacity、class_code 都帶過去。"""
        old = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31))
        new = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))

        cls = Classroom(
            name="大象班",
            school_year=114,
            semester=1,
            capacity=25,
            head_teacher_id=None,
            assistant_teacher_id=None,
            art_teacher_id=None,
            grade_id=None,
            class_code="ELE",
        )
        db_session.add(cls)
        db_session.flush()

        handle(old=old, new=new, session=db_session)

        new_cls = (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 114, Classroom.semester == 2)
            .first()
        )
        assert new_cls.name == "大象班"
        assert new_cls.capacity == 25
        assert new_cls.class_code == "ELE"
