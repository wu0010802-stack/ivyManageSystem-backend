"""P2-5 / P2-6 回歸（2026-06-23 全系統資安掃描）：教師端批量醫療端點補 §6
medical_access_log 留痕 + home/summary 過敏警示權限遮罩。

RA-MED-3 修補只補了 4 條路徑，遺漏兩條教師每日首屏批量路徑：
- P2-5：GET /portal/home/summary（過敏彙總）、/portal/class-hub/today（今日用藥）、
  /portal/medications/today 批量回出解密醫療內容卻零 §6 留痕。
- P2-6：home/summary 對 allergy_alerts 無條件回出，未做 STUDENTS_HEALTH_READ 遮罩
  （對照 students / classrooms / class-hub 皆有遮罩或權限閘）。

對齊 classrooms.py:431 的 emit_batch_medical_access_log + mask 模式。
DB 隔離：SQLite + monkeypatch base_module。
"""

from __future__ import annotations

import os
import sys
from datetime import date as date_cls

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.portal import router as portal_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import Base, Classroom, Employee, Student, User
from models.medical_access_log import MEDICAL_FIELD_BUNDLE, MedicalAccessLog
from models.portfolio import (
    StudentAllergy,
    StudentMedicationLog,
    StudentMedicationOrder,
)
from utils.auth import create_access_token

_HEALTH = "STUDENTS_HEALTH_READ"
_BASE_PERMS = ["DASHBOARD", "PORTFOLIO_READ", "STUDENTS_READ"]


@pytest.fixture
def portal_client(tmp_path):
    db_path = tmp_path / "portal_med.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine, old_sf = base_module._engine, base_module._SessionFactory
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


def _seed_teacher(sf, *, perms: list[str], with_medical: bool = True) -> dict:
    """班導 + 一個學生（可選帶過敏 + 今日待執行用藥）。"""
    today = date_cls.today()  # noqa: DTZ011
    with sf() as s:
        emp = Employee(employee_id="E_MED", name="林老師", is_active=True)
        s.add(emp)
        s.flush()
        classroom = Classroom(name="海星班", is_active=True, head_teacher_id=emp.id)
        s.add(classroom)
        s.flush()
        stu = Student(
            student_id="SMED01",
            name="陳小寶",
            classroom_id=classroom.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        s.add(stu)
        u = User(
            username="t_med",
            password_hash="!",
            role="teacher",
            employee_id=emp.id,
            permission_names=perms,
            is_active=True,
            token_version=0,
        )
        s.add(u)
        s.flush()
        if with_medical:
            s.add(
                StudentAllergy(
                    student_id=stu.id,
                    allergen="花生",
                    severity="嚴重",
                    reaction_symptom="呼吸困難",
                    active=True,
                )
            )
            order = StudentMedicationOrder(
                student_id=stu.id,
                order_date=today,
                medication_name="退燒藥",
                dose="5ml",
                time_slots=["12:00"],
                created_by=u.id,
            )
            s.add(order)
            s.flush()
            s.add(
                StudentMedicationLog(
                    order_id=order.id,
                    scheduled_time="12:00",
                    skipped=False,
                )
            )
        s.commit()
        return {
            "user_id": u.id,
            "employee_id": emp.id,
            "username": u.username,
            "permission_names": perms,
            "token_version": 0,
            "classroom_id": classroom.id,
            "student_id": stu.id,
        }


def _token(seed: dict) -> str:
    return create_access_token(
        {
            "user_id": seed["user_id"],
            "employee_id": seed["employee_id"],
            "role": "teacher",
            "name": seed["username"],
            "permission_names": seed["permission_names"],
            "token_version": seed["token_version"],
        }
    )


def _bundle_logs(sf):
    with sf() as s:
        return (
            s.query(MedicalAccessLog)
            .filter(MedicalAccessLog.field_name == MEDICAL_FIELD_BUNDLE)
            .all()
        )


def _get(client, path: str, tk: str):
    return client.get(path, cookies={"access_token": tk})


# ── P2-6：home/summary 過敏遮罩 ──────────────────────────────────────────


def test_home_summary_without_health_read_masks_allergy_and_no_log(portal_client):
    client, sf = portal_client
    seed = _seed_teacher(sf, perms=_BASE_PERMS)  # 無 HEALTH_READ
    r = _get(client, "/api/portal/home/summary", _token(seed))
    assert r.status_code == 200, r.text
    cards = r.json()["classrooms"]
    # 過敏為醫療特種個資，無 HEALTH_READ 不得回出
    assert all(not c.get("allergy_alerts") for c in cards), "無 HEALTH_READ 須遮罩過敏"
    assert len(_bundle_logs(sf)) == 0


def test_home_summary_with_health_read_returns_allergy_and_writes_log(portal_client):
    client, sf = portal_client
    seed = _seed_teacher(sf, perms=_BASE_PERMS + [_HEALTH])
    r = _get(client, "/api/portal/home/summary", _token(seed))
    assert r.status_code == 200, r.text
    cards = r.json()["classrooms"]
    assert any(c.get("allergy_alerts") for c in cards), "有 HEALTH_READ 應回出過敏"
    logs = _bundle_logs(sf)
    assert len(logs) == 1, "回出過敏內容須補一筆 §6 batch log"


# ── P2-5：class-hub/today 與 medications/today 用藥留痕 ────────────────────


def test_class_hub_today_with_health_read_writes_log(portal_client):
    client, sf = portal_client
    seed = _seed_teacher(sf, perms=_BASE_PERMS + [_HEALTH])
    r = _get(client, "/api/portal/class-hub/today", _token(seed))
    assert r.status_code == 200, r.text
    logs = _bundle_logs(sf)
    assert len(logs) == 1, "class-hub 今日用藥回出須補 §6 batch log"


def test_medications_today_with_health_read_writes_log(portal_client):
    client, sf = portal_client
    seed = _seed_teacher(sf, perms=_BASE_PERMS + [_HEALTH])
    r = _get(client, "/api/portal/medications/today", _token(seed))
    assert r.status_code == 200, r.text
    logs = _bundle_logs(sf)
    assert len(logs) == 1, "medications/today 用藥回出須補 §6 batch log"
