"""tests/test_portfolio_access_scope_bridge.py

Phase 2.1 Task 1.5 bridge 擴展：assert_student_access / filter_student_ids_by_access /
student_ids_in_scope 三個 wrapper helper 接受 code= 參數。

未傳 code 時必須走原 role-based 路徑（向後相容 ~30 個既有 caller）；
傳 code 時改走 PermissionGrant.scope 判斷（Phase 1 已落地 is_unrestricted /
accessible_classroom_ids 的 code= 路徑）。
"""

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Classroom, Student
from models.employee import Employee
from utils.permissions import Permission
from utils.portfolio_access import (
    assert_student_access,
    filter_student_ids_by_access,
    student_ids_in_scope,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory 測試 DB fixture（swap 全域 engine 模式）。"""
    db_path = tmp_path / "portfolio_access_scope_bridge_test.sqlite"
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
def teacher_with_class(db_session):
    """建：1 名教師（任 A 班 head_teacher）+ 2 班級（A、B）+ 2 學生（一在 A、一在 B）。

    用於驗證 code= 參數的 scope 過濾行為，且測試 own_class scope 在跨班場景應排除他班學生。
    """
    teacher = Employee(employee_id="T001", name="王老師")
    db_session.add(teacher)
    db_session.flush()

    classroom_a = Classroom(
        name="星星班", school_year=113, semester=2, head_teacher_id=teacher.id
    )
    classroom_b = Classroom(
        name="月亮班", school_year=113, semester=2, head_teacher_id=None
    )
    db_session.add_all([classroom_a, classroom_b])
    db_session.flush()

    student_in_class = Student(
        student_id="S001",
        name="陳小明",
        classroom_id=classroom_a.id,
        lifecycle_status="active",
    )
    student_other_class = Student(
        student_id="S002",
        name="林小華",
        classroom_id=classroom_b.id,
        lifecycle_status="active",
    )
    db_session.add_all([student_in_class, student_other_class])
    db_session.commit()

    user_dict = {
        "role": "teacher",
        "employee_id": teacher.id,
        "permission_names": ["STUDENTS_READ:own_class"],
    }

    return {
        "teacher": teacher,
        "classroom_a_id": classroom_a.id,
        "classroom_b_id": classroom_b.id,
        "student_in_class_id": student_in_class.id,
        "student_other_class_id": student_other_class.id,
        "user_dict": user_dict,
    }


# ---------------------------------------------------------------------------
# student_ids_in_scope — with code arg
# ---------------------------------------------------------------------------


class TestStudentIdsInScopeWithCode:
    def test_teacher_own_class_with_code_returns_class_student_ids(
        self, db_session, teacher_with_class
    ):
        """teacher own_class scope + code → 只回傳自班學生 id。"""
        teacher = dict(teacher_with_class["user_dict"])
        teacher["permission_names"] = ["STUDENTS_READ:own_class"]
        result = student_ids_in_scope(
            db_session, teacher, code=Permission.STUDENTS_READ.value
        )
        assert isinstance(result, list)
        assert teacher_with_class["student_in_class_id"] in result
        assert teacher_with_class["student_other_class_id"] not in result

    def test_teacher_all_scope_with_code_returns_none(
        self, db_session, teacher_with_class
    ):
        """teacher all scope + code → None 表全放行（彙總端點 WHERE 子句跳過篩選）。"""
        teacher = dict(teacher_with_class["user_dict"])
        teacher["permission_names"] = ["STUDENTS_READ:all"]
        result = student_ids_in_scope(
            db_session, teacher, code=Permission.STUDENTS_READ.value
        )
        assert result is None

    def test_no_code_falls_back_to_role_based(self, db_session, teacher_with_class):
        """未傳 code → role=teacher 走班級篩選（向後相容）。"""
        teacher = dict(teacher_with_class["user_dict"])
        result = student_ids_in_scope(db_session, teacher)
        assert isinstance(result, list)
        # teacher 任 A 班 head_teacher，應看得到自班學生、看不到 B 班學生
        assert teacher_with_class["student_in_class_id"] in result
        assert teacher_with_class["student_other_class_id"] not in result


# ---------------------------------------------------------------------------
# assert_student_access — with code arg
# ---------------------------------------------------------------------------


class TestAssertStudentAccessWithCode:
    def test_teacher_all_scope_can_access_any_student(
        self, db_session, teacher_with_class
    ):
        """teacher all scope + code → 可存取他班學生。"""
        teacher = dict(teacher_with_class["user_dict"])
        teacher["permission_names"] = ["STUDENTS_READ:all"]
        student = assert_student_access(
            db_session,
            teacher,
            teacher_with_class["student_other_class_id"],
            code=Permission.STUDENTS_READ.value,
        )
        assert student is not None
        assert student.id == teacher_with_class["student_other_class_id"]

    def test_teacher_own_class_scope_403_on_other_class(
        self, db_session, teacher_with_class
    ):
        """teacher own_class scope + code → 存取他班學生 403。"""
        teacher = dict(teacher_with_class["user_dict"])
        teacher["permission_names"] = ["STUDENTS_READ:own_class"]
        with pytest.raises(HTTPException) as exc:
            assert_student_access(
                db_session,
                teacher,
                teacher_with_class["student_other_class_id"],
                code=Permission.STUDENTS_READ.value,
            )
        assert exc.value.status_code == 403

    def test_no_code_falls_back_to_role_based(self, db_session, teacher_with_class):
        """未傳 code → role=teacher 走 role-based（自班可存取、他班 403）。"""
        teacher = dict(teacher_with_class["user_dict"])
        # 自班 OK
        student = assert_student_access(
            db_session, teacher, teacher_with_class["student_in_class_id"]
        )
        assert student.id == teacher_with_class["student_in_class_id"]
        # 他班 403
        with pytest.raises(HTTPException) as exc:
            assert_student_access(
                db_session, teacher, teacher_with_class["student_other_class_id"]
            )
        assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# filter_student_ids_by_access — with code arg
# ---------------------------------------------------------------------------


class TestFilterStudentIdsByAccessWithCode:
    def test_filter_by_code_scope(self, db_session, teacher_with_class):
        """teacher own_class scope + code → 過濾掉他班學生 id。"""
        teacher = dict(teacher_with_class["user_dict"])
        teacher["permission_names"] = ["STUDENTS_READ:own_class"]
        result = filter_student_ids_by_access(
            db_session,
            teacher,
            [
                teacher_with_class["student_in_class_id"],
                teacher_with_class["student_other_class_id"],
            ],
            code=Permission.STUDENTS_READ.value,
        )
        assert teacher_with_class["student_in_class_id"] in result
        assert teacher_with_class["student_other_class_id"] not in result

    def test_filter_with_all_scope_returns_all(self, db_session, teacher_with_class):
        """teacher all scope + code → 回傳全部候選 id（不過濾）。"""
        teacher = dict(teacher_with_class["user_dict"])
        teacher["permission_names"] = ["STUDENTS_READ:all"]
        candidates = [
            teacher_with_class["student_in_class_id"],
            teacher_with_class["student_other_class_id"],
        ]
        result = filter_student_ids_by_access(
            db_session, teacher, candidates, code=Permission.STUDENTS_READ.value
        )
        assert result == set(candidates)

    def test_no_code_falls_back_to_role_based(self, db_session, teacher_with_class):
        """未傳 code → role=teacher 走班級篩選（向後相容）。"""
        teacher = dict(teacher_with_class["user_dict"])
        candidates = [
            teacher_with_class["student_in_class_id"],
            teacher_with_class["student_other_class_id"],
        ]
        result = filter_student_ids_by_access(db_session, teacher, candidates)
        assert teacher_with_class["student_in_class_id"] in result
        assert teacher_with_class["student_other_class_id"] not in result
