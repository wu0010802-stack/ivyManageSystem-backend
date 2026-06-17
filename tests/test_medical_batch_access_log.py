"""BE-3-medical-log（OWASP A09）：批次醫療端點補 §6 medical_access_log。

三個會「批次回出」解密醫療欄位的端點原本繞過 §6 特種個資取用稽核：

- C17 GET /students          清單批次回出 allergy / medication
- C18 GET /classrooms/{id}   _serialize_classroom_detail 回出整班醫療欄位
- C20 GET /portfolio/today-medication  班級彙總回出 medication_name / dose / note

對照 students.py:760 detail 端點與 student_health.py:228 單筆讀取都有寫 §6 log，
這三處漏寫 → prod 醫療存取軌跡有缺口。

修後：caller 具 STUDENTS_HEALTH_READ 且實際回出 ≥1 名學生的非空醫療內容時，
補寫一筆 batch MedicalAccessLog（field_name=bundle、generic reason、記涉及學生數）。
無 health-read → mask 後不回出醫療 → 不需寫 log。
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
from api.classrooms import router as classrooms_router
from api.student_health import router as student_health_router
from api.students import router as students_router
from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
from models.database import Base, Employee, User
from models.medical_access_log import MEDICAL_FIELD_BUNDLE, MedicalAccessLog
from models.portfolio import StudentMedicationOrder
from utils.auth import create_access_token, hash_password


@pytest.fixture
def app_client(tmp_path):
    db_path = tmp_path / "med-batch.sqlite"
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
    app.include_router(classrooms_router)
    app.include_router(student_health_router)
    with TestClient(app) as c:
        yield c, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed(sf, *, perms, with_medical=True):
    """建立 admin user + 一個班 + 兩名（其中一名有醫療內容）學生。"""
    with sf() as s:
        cls = Classroom(name="班", school_year=2025, semester=1, is_active=True)
        s.add(cls)
        s.flush()
        s1 = Student(
            student_id="MB01",
            name="小明",
            classroom_id=cls.id,
            is_active=True,
            enrollment_date=date(2025, 9, 1),
            lifecycle_status=LIFECYCLE_ACTIVE,
            allergy="花生" if with_medical else None,
            medication="氣喘吸入劑" if with_medical else None,
        )
        s2 = Student(
            student_id="MB02",
            name="小華",
            classroom_id=cls.id,
            is_active=True,
            enrollment_date=date(2025, 9, 1),
            lifecycle_status=LIFECYCLE_ACTIVE,
            allergy=None,
            medication=None,
        )
        s.add_all([s1, s2])
        user = User(
            username="adm_mb",
            password_hash=hash_password("Pass1234"),
            role="admin",
            permission_names=perms,
            is_active=True,
            must_change_password=False,
        )
        s.add(user)
        s.flush()
        order = StudentMedicationOrder(
            student_id=s1.id,
            order_date=date.today(),
            medication_name="退燒藥",
            dose="1 顆",
            time_slots=["12:00"],
            note="飯後服用",
            created_by=user.id,
        )
        s.add(order)
        s.commit()
        return {"cls_id": cls.id, "s1": s1.id, "s2": s2.id}


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "adm_mb", "password": "Pass1234"}
    )


def _bundle_logs(sf):
    with sf() as s:
        return (
            s.query(MedicalAccessLog)
            .filter(MedicalAccessLog.field_name == MEDICAL_FIELD_BUNDLE)
            .all()
        )


# ── C17 GET /students 清單 ────────────────────────────────────────────


def test_students_list_with_health_read_writes_batch_log(app_client):
    client, sf = _seed_and_login(app_client)
    r = client.get("/api/students", params={"limit": 50})
    assert r.status_code == 200, r.text
    # 明文回出
    assert any(it.get("allergy") == "花生" for it in r.json()["items"])

    logs = _bundle_logs(sf)
    assert len(logs) == 1
    assert logs[0].reason


def test_students_list_without_health_read_no_log(app_client):
    client, sf = app_client
    _seed(sf, perms=["STUDENTS_READ"])  # 無 HEALTH_READ
    assert _login(client).status_code == 200
    r = client.get("/api/students", params={"limit": 50})
    assert r.status_code == 200, r.text
    assert all(it.get("allergy") is None for it in r.json()["items"])  # 遮罩
    assert len(_bundle_logs(sf)) == 0


def test_students_list_health_read_but_no_medical_no_log(app_client):
    client, sf = app_client
    _seed(
        sf,
        perms=["STUDENTS_READ", "STUDENTS_HEALTH_READ"],
        with_medical=False,
    )
    assert _login(client).status_code == 200
    r = client.get("/api/students", params={"limit": 50})
    assert r.status_code == 200, r.text
    assert len(_bundle_logs(sf)) == 0  # 全班無醫療內容 → 不寫噪音


# ── C18 GET /classrooms/{id} 詳細 ─────────────────────────────────────


def test_classroom_detail_with_health_read_writes_batch_log(app_client):
    client, sf = _seed_and_login(app_client, perms=_DETAIL_PERMS)
    cls_id = _last_seed["cls_id"]
    r = client.get(f"/api/classrooms/{cls_id}")
    assert r.status_code == 200, r.text
    assert any(st.get("allergy") == "花生" for st in r.json()["students"])

    logs = _bundle_logs(sf)
    assert len(logs) == 1
    assert logs[0].reason


def test_classroom_detail_without_health_read_no_log(app_client):
    client, sf = app_client
    seed = _seed(sf, perms=["CLASSROOMS_READ"])  # 無 HEALTH_READ
    assert _login(client).status_code == 200
    r = client.get(f"/api/classrooms/{seed['cls_id']}")
    assert r.status_code == 200, r.text
    assert all(st.get("allergy") is None for st in r.json()["students"])
    assert len(_bundle_logs(sf)) == 0


# ── C20 GET /portfolio/today-medication 彙總 ─────────────────────────


def test_today_medication_with_health_read_writes_batch_log(app_client):
    client, sf = app_client
    _seed(sf, perms=["STUDENTS_HEALTH_READ"])
    assert _login(client).status_code == 200
    r = client.get("/api/portfolio/today-medication")
    assert r.status_code == 200, r.text
    assert any(o.get("medication_name") == "退燒藥" for o in r.json()["orders"])

    logs = _bundle_logs(sf)
    assert len(logs) == 1
    assert logs[0].reason


def test_today_medication_no_orders_no_log(app_client):
    """有 health-read 但今日無用藥單 → 不寫噪音。"""
    client, sf = app_client
    with sf() as s:
        cls = Classroom(name="空班", school_year=2025, semester=1, is_active=True)
        s.add(cls)
        user = User(
            username="adm_mb",
            password_hash=hash_password("Pass1234"),
            role="admin",
            permission_names=["STUDENTS_HEALTH_READ"],
            is_active=True,
            must_change_password=False,
        )
        s.add(user)
        s.commit()
    assert _login(client).status_code == 200
    r = client.get("/api/portfolio/today-medication")
    assert r.status_code == 200, r.text
    assert r.json()["orders"] == []
    assert len(_bundle_logs(sf)) == 0


# ── 共用：seed + login（admin perms 含 health-read）────────────────────

_DETAIL_PERMS = ["CLASSROOMS_READ", "STUDENTS_HEALTH_READ"]
_last_seed: dict = {}


def _seed_and_login(app_client, perms=None):
    client, sf = app_client
    global _last_seed
    _last_seed = _seed(sf, perms=perms or ["STUDENTS_READ", "STUDENTS_HEALTH_READ"])
    assert _login(client).status_code == 200
    return client, sf
