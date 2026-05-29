"""services/scoping/student_scope.filter_clause 單元測試。

涵蓋：
  - scope='all'  → 回傳 None
  - scope='own_class' → 回傳 SQLAlchemy 子句，篩出教師所屬班級的學生
  - scope='own_class' + employee_id 為 None → 拋出 ValueError
  - scope 為未知值 → 拋出 ValueError（含 'unknown scope' 訊息）
  - 助教身分（assistant_teacher_id）同樣納入篩選
"""

import os
import sys
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Classroom, Student
from models.employee import Employee
from services.scoping import student_scope


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory 測試 DB fixture（swap 全域 engine 模式）。"""
    db_path = tmp_path / "scoping_test.sqlite"
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
def setup_two_classrooms(db_session):
    """建立兩位教師、兩個班級、各一位學生，供 filter_clause 整合測試使用。"""
    teacher_a = Employee(employee_id="TA001", name="教師甲")
    teacher_b = Employee(employee_id="TB002", name="教師乙")
    db_session.add_all([teacher_a, teacher_b])
    db_session.flush()

    c1 = Classroom(
        name="星星班", school_year=113, semester=2, head_teacher_id=teacher_a.id
    )
    c2 = Classroom(
        name="月亮班", school_year=113, semester=2, head_teacher_id=teacher_b.id
    )
    db_session.add_all([c1, c2])
    db_session.flush()

    s1 = Student(
        student_id="S001", name="王小明", classroom_id=c1.id, lifecycle_status="active"
    )
    s2 = Student(
        student_id="S002", name="李小華", classroom_id=c2.id, lifecycle_status="active"
    )
    db_session.add_all([s1, s2])
    db_session.commit()

    return SimpleNamespace(
        teacher_a=teacher_a,
        teacher_b=teacher_b,
        c1=c1,
        c2=c2,
        s1=s1,
        s2=s2,
    )


def _user(employee_id):
    return SimpleNamespace(employee_id=employee_id, permission_names=[])


def test_filter_clause_all_returns_none(setup_two_classrooms):
    """scope='all' 時應回傳 None（呼叫端跳過篩選）。"""
    user = _user(setup_two_classrooms.teacher_a.id)
    assert student_scope.filter_clause(user, "all") is None


def test_filter_clause_own_class_returns_clause(setup_two_classrooms, db_session):
    """scope='own_class' 時只回傳教師擔任班導的班級學生。"""
    user = _user(setup_two_classrooms.teacher_a.id)
    clause = student_scope.filter_clause(user, "own_class")
    assert clause is not None
    visible = db_session.query(Student).filter(clause).all()
    names = {s.name for s in visible}
    assert names == {"王小明"}


def test_filter_clause_own_class_includes_assistant_teacher(
    db_session, setup_two_classrooms
):
    """scope='own_class' 時助教（assistant_teacher_id）也應納入可見班級。"""
    setup_two_classrooms.c1.assistant_teacher_id = setup_two_classrooms.teacher_b.id
    db_session.commit()

    user = _user(setup_two_classrooms.teacher_b.id)
    clause = student_scope.filter_clause(user, "own_class")
    visible = db_session.query(Student).filter(clause).all()
    names = {s.name for s in visible}
    # teacher_b 為 c1 助教 + c2 班導 → 兩位學生皆可見
    assert names == {"王小明", "李小華"}


def test_filter_clause_own_class_raises_without_employee_id():
    """scope='own_class' 但 employee_id 為 None 時應拋出 ValueError。"""
    user = SimpleNamespace(employee_id=None, permission_names=[])
    with pytest.raises(ValueError, match="employee_id"):
        student_scope.filter_clause(user, "own_class")


def test_filter_clause_unknown_scope_raises():
    """未知 scope 應拋出含 'unknown scope' 訊息的 ValueError。"""
    user = _user(1)
    with pytest.raises(ValueError, match="unknown scope"):
        student_scope.filter_clause(user, "own_campus")
