"""RA-MED-3：學生詳細頁醫療欄位讀取補 §6 medical_access_log。

GET /students/{id}（admin 後台）與 /portal/students/{id}/detail（教師端）會回明文
醫療欄位（allergy/medication）給有 STUDENTS_HEALTH_READ 的 caller，但原本不寫
medical_access_log（§6 特種個資取用稽核）。/medical 端點有寫、detail 端點漏寫。

修後：detail 端點當實際回出解密醫療內容（caller 有 health-read 且該生有醫療內容）
時補寫一筆 medical_access_log（generic reason）。list 端點不動。
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
from api.portal import router as portal_router
from api.students import router as students_router
from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
from models.database import Base, Employee, User
from models.medical_access_log import MedicalAccessLog
from utils.auth import create_access_token, hash_password

# ── students.py detail 端點（admin 後台，login-cookie auth）──


@pytest.fixture
def students_app(tmp_path):
    db_path = tmp_path / "med-audit-students.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(students_router)
    with TestClient(app) as c:
        yield c, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_admin_student(sf, *, perms, with_medical=True):
    with sf() as s:
        cls = Classroom(name="班", school_year=2025, semester=1, is_active=True)
        s.add(cls)
        s.flush()
        student = Student(
            student_id="MED01",
            name="小明",
            classroom_id=cls.id,
            is_active=True,
            enrollment_date=date(2025, 9, 1),
            lifecycle_status=LIFECYCLE_ACTIVE,
            allergy="花生" if with_medical else None,
            medication="氣喘吸入劑" if with_medical else None,
        )
        s.add(student)
        user = User(
            username="adm_med",
            password_hash=hash_password("Pass1234"),
            role="admin",
            permission_names=perms,
            is_active=True,
            must_change_password=False,
        )
        s.add(user)
        s.commit()
        return student.id


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "adm_med", "password": "Pass1234"}
    )


def test_students_detail_with_health_read_writes_medical_log(students_app):
    client, sf = students_app
    sid = _seed_admin_student(sf, perms=["STUDENTS_READ", "STUDENTS_HEALTH_READ"])
    assert _login(client).status_code == 200

    r = client.get(f"/api/students/{sid}")
    assert r.status_code == 200, r.text
    assert r.json()["allergy"] == "花生"  # 明文回出

    with sf() as s:
        logs = (
            s.query(MedicalAccessLog).filter(MedicalAccessLog.student_id == sid).all()
        )
        assert len(logs) == 1
        assert logs[0].reason  # 有 generic reason


def test_students_detail_without_health_read_no_log(students_app):
    client, sf = students_app
    sid = _seed_admin_student(sf, perms=["STUDENTS_READ"])  # 無 HEALTH_READ
    assert _login(client).status_code == 200

    r = client.get(f"/api/students/{sid}")
    assert r.status_code == 200, r.text
    assert r.json()["allergy"] is None  # 遮罩

    with sf() as s:
        logs = (
            s.query(MedicalAccessLog).filter(MedicalAccessLog.student_id == sid).all()
        )
        assert len(logs) == 0  # 未回出醫療內容 → 不寫 §6 log


def test_students_detail_health_read_but_no_medical_content_no_log(students_app):
    """有 health-read 但該生無醫療內容 → 不寫 log（避免無意義噪音）。"""
    client, sf = students_app
    sid = _seed_admin_student(
        sf, perms=["STUDENTS_READ", "STUDENTS_HEALTH_READ"], with_medical=False
    )
    assert _login(client).status_code == 200

    r = client.get(f"/api/students/{sid}")
    assert r.status_code == 200, r.text

    with sf() as s:
        logs = (
            s.query(MedicalAccessLog).filter(MedicalAccessLog.student_id == sid).all()
        )
        assert len(logs) == 0


# ── portal/students.py detail 端點（教師端，bearer-cookie auth）──


@pytest.fixture
def portal_app(tmp_path):
    db_path = tmp_path / "med-audit-portal.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(portal_router)
    with TestClient(app) as c:
        yield c, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_portal(sf, *, perms):
    with sf() as s:
        emp = Employee(
            employee_id="E1", name="老師A", is_active=True, base_salary=30000
        )
        s.add(emp)
        s.flush()
        cls = Classroom(name="A班", is_active=True, head_teacher_id=emp.id)
        s.add(cls)
        s.flush()
        student = Student(
            student_id="PMED01",
            name="小華",
            classroom_id=cls.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
            birthday=date(2020, 5, 5),
            allergy="花生",
            medication="氣喘吸入劑",
        )
        s.add(student)
        teacher = User(
            username="t1",
            password_hash="!",
            role="teacher",
            employee_id=emp.id,
            permission_names=perms,
            is_active=True,
            token_version=0,
        )
        s.add(teacher)
        s.commit()
        return {
            "student_id": student.id,
            "teacher_id": teacher.id,
            "emp_id": emp.id,
            "perms": perms,
        }


def _portal_token(uid, emp_id, perms):
    return create_access_token(
        {
            "user_id": uid,
            "employee_id": emp_id,
            "role": "teacher",
            "name": "t1",
            "permission_names": perms,
            "token_version": 0,
        }
    )


def test_portal_detail_with_health_read_writes_medical_log(portal_app):
    client, sf = portal_app
    seed = _seed_portal(
        sf, perms=["STUDENTS_READ", "PORTFOLIO_READ", "STUDENTS_HEALTH_READ"]
    )
    tk = _portal_token(seed["teacher_id"], seed["emp_id"], seed["perms"])

    r = client.get(
        f"/api/portal/students/{seed['student_id']}/detail",
        cookies={"access_token": tk},
    )
    assert r.status_code == 200, r.text
    assert r.json()["student"]["allergy_text"] == "花生"

    with sf() as s:
        logs = (
            s.query(MedicalAccessLog)
            .filter(MedicalAccessLog.student_id == seed["student_id"])
            .all()
        )
        assert len(logs) == 1
        assert logs[0].reason


def test_portal_detail_without_health_read_no_log(portal_app):
    client, sf = portal_app
    seed = _seed_portal(sf, perms=["STUDENTS_READ", "PORTFOLIO_READ"])  # 無 HEALTH_READ
    tk = _portal_token(seed["teacher_id"], seed["emp_id"], seed["perms"])

    r = client.get(
        f"/api/portal/students/{seed['student_id']}/detail",
        cookies={"access_token": tk},
    )
    assert r.status_code == 200, r.text
    assert r.json()["student"]["allergy_text"] is None  # 遮罩

    with sf() as s:
        logs = (
            s.query(MedicalAccessLog)
            .filter(MedicalAccessLog.student_id == seed["student_id"])
            .all()
        )
        assert len(logs) == 0
