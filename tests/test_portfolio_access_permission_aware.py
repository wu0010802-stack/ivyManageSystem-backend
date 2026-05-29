"""tests/test_portfolio_access_permission_aware.py

utils/portfolio_access.py — PermissionGrant-aware 路徑單元測試。

涵蓋：
  - is_unrestricted 無 code 參數 → role-based 判斷（向後相容）
  - is_unrestricted 帶 code 參數 → grant scope 判斷
  - accessible_classroom_ids 帶 code 參數 → 正確篩班
  - accessible_classroom_ids 無 code 參數 → 原 role-based 向後相容
"""

import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Classroom, Student
from models.employee import Employee
from utils.portfolio_access import accessible_classroom_ids, is_unrestricted

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory 測試 DB fixture（swap 全域 engine 模式）。"""
    db_path = tmp_path / "portfolio_access_test.sqlite"
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
def setup_teacher_with_classroom(db_session):
    """建立一位教師、一個班級（教師為導師），供測試用。"""
    teacher = Employee(employee_id="T001", name="王老師")
    db_session.add(teacher)
    db_session.flush()

    classroom = Classroom(
        name="星星班", school_year=113, semester=2, head_teacher_id=teacher.id
    )
    db_session.add(classroom)
    db_session.flush()

    student = Student(
        student_id="S001",
        name="陳小明",
        classroom_id=classroom.id,
        lifecycle_status="active",
    )
    db_session.add(student)
    db_session.commit()

    return SimpleNamespace(teacher=teacher, classroom=classroom, student=student)


# ---------------------------------------------------------------------------
# is_unrestricted — no code arg (role-based, backward compat)
# ---------------------------------------------------------------------------


def test_is_unrestricted_no_code_role_admin_returns_true():
    """admin role 無 code 參數 → True（向後相容）。"""
    user = {"role": "admin", "permission_names": ["X"]}
    assert is_unrestricted(user) is True


def test_is_unrestricted_no_code_role_teacher_returns_false():
    """teacher role 無 code 參數 → False（向後相容）。"""
    user = {"role": "teacher", "permission_names": ["STUDENTS_READ"]}
    assert is_unrestricted(user) is False


# ---------------------------------------------------------------------------
# is_unrestricted — with code arg (PermissionGrant-aware path)
# ---------------------------------------------------------------------------


def test_is_unrestricted_with_code_wildcard_returns_true():
    """wildcard ['*'] 帶 code → True（admin/wildcard 全放行）。"""
    user = {"role": "admin", "permission_names": ["*"]}
    assert is_unrestricted(user, code="STUDENTS_READ") is True


def test_is_unrestricted_with_code_all_returns_true():
    """持有 STUDENTS_READ:all 帶 code → True（自訂角色跨班存取）。"""
    user = {"role": "teacher", "permission_names": ["STUDENTS_READ:all"]}
    assert is_unrestricted(user, code="STUDENTS_READ") is True


def test_is_unrestricted_with_code_own_class_returns_false():
    """持有 STUDENTS_READ:own_class 帶 code → False（限自班）。"""
    user = {"role": "teacher", "permission_names": ["STUDENTS_READ:own_class"]}
    assert is_unrestricted(user, code="STUDENTS_READ") is False


def test_is_unrestricted_with_code_not_held_returns_false():
    """未持有 code 帶 code → False（fail-closed）。"""
    user = {"role": "teacher", "permission_names": ["DASHBOARD"]}
    assert is_unrestricted(user, code="STUDENTS_READ") is False


def test_is_unrestricted_with_code_bare_returns_true():
    """持有 bare 'STUDENTS_READ'（無 scope 修飾詞）帶 code → True（向後相容 = :all）。"""
    user = {"role": "teacher", "permission_names": ["STUDENTS_READ"]}
    assert is_unrestricted(user, code="STUDENTS_READ") is True


# ---------------------------------------------------------------------------
# accessible_classroom_ids — with code arg
# ---------------------------------------------------------------------------


def test_accessible_classroom_ids_with_code_all_returns_empty_list(
    db_session, setup_teacher_with_classroom
):
    """teacher with STUDENTS_READ:all + code 參數 → [] 表全放行。"""
    teacher = setup_teacher_with_classroom.teacher
    user = {
        "role": "teacher",
        "employee_id": teacher.id,
        "permission_names": ["STUDENTS_READ:all"],
    }
    result = accessible_classroom_ids(db_session, user, code="STUDENTS_READ")
    assert result == []


def test_accessible_classroom_ids_with_code_own_class_returns_class_ids(
    db_session, setup_teacher_with_classroom
):
    """teacher with STUDENTS_READ:own_class + code 參數 → 回傳自班 id 清單。"""
    teacher = setup_teacher_with_classroom.teacher
    classroom = setup_teacher_with_classroom.classroom
    user = {
        "role": "teacher",
        "employee_id": teacher.id,
        "permission_names": ["STUDENTS_READ:own_class"],
    }
    result = accessible_classroom_ids(db_session, user, code="STUDENTS_READ")
    assert result == [classroom.id]


# ---------------------------------------------------------------------------
# accessible_classroom_ids — no code (backward compat)
# ---------------------------------------------------------------------------


def test_accessible_classroom_ids_no_code_admin_returns_empty_list(db_session):
    """admin role 無 code 參數 → [] 向後相容（全放行）。"""
    user = {"role": "admin", "permission_names": ["*"]}
    result = accessible_classroom_ids(db_session, user)
    assert result == []
