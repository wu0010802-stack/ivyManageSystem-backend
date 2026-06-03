"""RA-HIGH-3 回歸：principal/teacher 不可改非自班學生的 guardian。

漏洞：guardian 寫入端點（create/update/delete_guardian）只守 GUARDIANS_WRITE
權限，未做 per-student scope 檢查。園長（role=principal，非 unrestricted 但
ROLE_TEMPLATES 含 GUARDIANS_WRITE）原可改全園任一家長 PII。

本檔守護修法：三端點對齊 READ 端點的 assert_student_access（保留 principal/
teacher 對「自班學生」寫家長的能力，只擋跨班）。

依 tests/test_class_scope_extensions.py 的 fixture 模式實作。
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.students import router as students_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    Base,
    Classroom,
    Employee,
    Student,
    User,
)
from models.guardian import Guardian
from utils.auth import hash_password

# principal 在 ROLE_TEMPLATES 含 GUARDIANS_WRITE，且非 unrestricted（_UNRESTRICTED_ROLES
# 僅 admin/hr/supervisor）→ 屬本漏洞主角。加上 STUDENTS_READ 讓 assert_student_access
# 的 accessible_classroom_ids 走得到（不影響 scope 結果）。
_PRINCIPAL_PERMS = ["GUARDIANS_WRITE", "STUDENTS_READ"]


@pytest.fixture
def guardian_app(tmp_path):
    """建立隔離 FastAPI app（auth + students routers）。"""
    db_path = tmp_path / "guardian-access.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragma_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(students_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_employee(session, code: str, name: str) -> Employee:
    emp = Employee(
        employee_id=code,
        name=name,
        base_salary=32000,
        is_active=True,
        hire_date=date(2024, 1, 1),
    )
    session.add(emp)
    session.flush()
    return emp


def _create_user(session, *, username, password, role, permission_names, employee=None):
    if isinstance(permission_names, str):
        permission_names = [permission_names]
    user = User(
        employee_id=employee.id if employee else None,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permission_names=permission_names,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _seed_two_classrooms(session) -> dict:
    """A 班 + B 班；emp_p 是 A 班導（principal）；B 班無 principal 任職 → 跨班。"""
    emp_p = _create_employee(session, "EP01", "園長")
    emp_b = _create_employee(session, "EB01", "教師 B")

    cls_a = Classroom(
        name="A 班",
        school_year=2025,
        semester=1,
        is_active=True,
        head_teacher_id=emp_p.id,
    )
    cls_b = Classroom(
        name="B 班",
        school_year=2025,
        semester=1,
        is_active=True,
        head_teacher_id=emp_b.id,
    )
    session.add_all([cls_a, cls_b])
    session.flush()

    st_a = Student(
        student_id="SA01",
        name="A 班學生",
        classroom_id=cls_a.id,
        is_active=True,
        enrollment_date=date(2025, 9, 1),
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    st_b = Student(
        student_id="SB01",
        name="B 班學生",
        classroom_id=cls_b.id,
        is_active=True,
        enrollment_date=date(2025, 9, 1),
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add_all([st_a, st_b])
    session.commit()
    for obj in (emp_p, emp_b, cls_a, cls_b, st_a, st_b):
        session.refresh(obj)
    return {
        "emp_p": emp_p,
        "emp_b": emp_b,
        "cls_a": cls_a,
        "cls_b": cls_b,
        "st_a": st_a,
        "st_b": st_b,
    }


def _seed_guardian(session, student_id: int) -> int:
    g = Guardian(
        student_id=student_id,
        name="家長",
        phone="0912345678",
        email="parent@example.com",
        relation="父",
        is_primary=True,
    )
    session.add(g)
    session.commit()
    session.refresh(g)
    return g.id


# ---------------------------------------------------------------------------
# 跨班（principal 不是 B 班導）→ 三端點皆應 403
# ---------------------------------------------------------------------------


class TestPrincipalCannotWriteOtherClassGuardian:
    def test_principal_cannot_create_guardian_for_other_class_student(
        self, guardian_app
    ):
        client, factory = guardian_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="prin_create",
                password="Pass1234",
                role="principal",
                permission_names=_PRINCIPAL_PERMS,
                employee=seed["emp_p"],
            )
            s.commit()
            other_st = seed["st_b"].id
        assert _login(client, "prin_create", "Pass1234").status_code == 200
        r = client.post(
            f"/api/students/{other_st}/guardians",
            json={"name": "X", "relation": "父親", "phone": "0900000001"},
        )
        assert r.status_code == 403, r.text

    def test_principal_cannot_update_other_class_guardian(self, guardian_app):
        client, factory = guardian_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="prin_update",
                password="Pass1234",
                role="principal",
                permission_names=_PRINCIPAL_PERMS,
                employee=seed["emp_p"],
            )
            gid = _seed_guardian(s, seed["st_b"].id)
        assert _login(client, "prin_update", "Pass1234").status_code == 200
        r = client.patch(
            f"/api/students/guardians/{gid}",
            json={"phone": "0900000000"},
        )
        assert r.status_code == 403, r.text

    def test_principal_cannot_delete_other_class_guardian(self, guardian_app):
        client, factory = guardian_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="prin_delete",
                password="Pass1234",
                role="principal",
                permission_names=_PRINCIPAL_PERMS,
                employee=seed["emp_p"],
            )
            gid = _seed_guardian(s, seed["st_b"].id)
        assert _login(client, "prin_delete", "Pass1234").status_code == 200
        r = client.delete(f"/api/students/guardians/{gid}")
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 自班（principal 是 A 班導）→ 三端點仍應放行（不過度限縮）
# ---------------------------------------------------------------------------


class TestPrincipalCanWriteOwnClassGuardian:
    def test_principal_can_create_guardian_for_own_class_student(self, guardian_app):
        client, factory = guardian_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="prin_create_own",
                password="Pass1234",
                role="principal",
                permission_names=_PRINCIPAL_PERMS,
                employee=seed["emp_p"],
            )
            s.commit()
            own_st = seed["st_a"].id
        assert _login(client, "prin_create_own", "Pass1234").status_code == 200
        r = client.post(
            f"/api/students/{own_st}/guardians",
            json={"name": "自班家長", "relation": "母親", "phone": "0911222333"},
        )
        assert r.status_code == 201, r.text

    def test_principal_can_update_own_class_guardian(self, guardian_app):
        client, factory = guardian_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="prin_update_own",
                password="Pass1234",
                role="principal",
                permission_names=_PRINCIPAL_PERMS,
                employee=seed["emp_p"],
            )
            gid = _seed_guardian(s, seed["st_a"].id)
        assert _login(client, "prin_update_own", "Pass1234").status_code == 200
        r = client.patch(
            f"/api/students/guardians/{gid}",
            json={"phone": "0900000099"},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# admin（unrestricted）→ 跨班仍放行（行為不變）
# ---------------------------------------------------------------------------


class TestAdminUnrestricted:
    def test_admin_can_update_other_class_guardian(self, guardian_app):
        client, factory = guardian_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="gd_admin_w",
                password="Pass1234",
                role="admin",
                permission_names=["*"],
                employee=None,
            )
            gid = _seed_guardian(s, seed["st_b"].id)
        assert _login(client, "gd_admin_w", "Pass1234").status_code == 200
        r = client.patch(
            f"/api/students/guardians/{gid}",
            json={"phone": "0900000111"},
        )
        assert r.status_code == 200, r.text
