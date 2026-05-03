"""教師端用藥列表（/api/portal/medications/today）測試。"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime

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
from models.portfolio import StudentMedicationLog, StudentMedicationOrder
from utils.auth import create_access_token
from utils.permissions import Permission


@pytest.fixture
def med_client(tmp_path):
    db_path = tmp_path / "med.sqlite"
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


def _seed_two_classrooms(sf) -> dict:
    """老師 A 帶 A 班（小明），老師 B 帶 B 班（小華）。各一張今日用藥單。"""
    perm = int(
        Permission.STUDENTS_HEALTH_READ.value
        | Permission.STUDENTS_MEDICATION_ADMINISTER.value
    )
    today = date.today()
    with sf() as session:
        e1 = Employee(employee_id="E1", name="老師A", is_active=True, base_salary=30000)
        e2 = Employee(employee_id="E2", name="老師B", is_active=True, base_salary=30000)
        session.add_all([e1, e2])
        session.flush()
        c1 = Classroom(name="A班", is_active=True, head_teacher_id=e1.id)
        c2 = Classroom(name="B班", is_active=True, head_teacher_id=e2.id)
        session.add_all([c1, c2])
        session.flush()
        s1 = Student(
            student_id="S1",
            name="小明",
            classroom_id=c1.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        s2 = Student(
            student_id="S2",
            name="小華",
            classroom_id=c2.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        session.add_all([s1, s2])
        session.flush()
        # 用藥單 + log
        o1 = StudentMedicationOrder(
            student_id=s1.id,
            order_date=today,
            medication_name="退燒藥",
            dose="5ml",
            time_slots=["08:30", "12:00"],
            source="parent",
        )
        o2 = StudentMedicationOrder(
            student_id=s2.id,
            order_date=today,
            medication_name="止咳",
            dose="1顆",
            time_slots=["10:00"],
            source="parent",
        )
        session.add_all([o1, o2])
        session.flush()
        session.add_all(
            [
                StudentMedicationLog(order_id=o1.id, scheduled_time="08:30"),
                StudentMedicationLog(
                    order_id=o1.id,
                    scheduled_time="12:00",
                    administered_at=datetime.now(),
                ),
                StudentMedicationLog(order_id=o2.id, scheduled_time="10:00"),
            ]
        )
        u1 = User(
            username="t1",
            password_hash="!",
            role="teacher",
            employee_id=e1.id,
            permissions=perm,
            is_active=True,
            token_version=0,
        )
        u2 = User(
            username="t2",
            password_hash="!",
            role="teacher",
            employee_id=e2.id,
            permissions=perm,
            is_active=True,
            token_version=0,
        )
        session.add_all([u1, u2])
        session.commit()
        return {
            "perm": perm,
            "u1_id": u1.id,
            "u1_emp": e1.id,
            "u2_id": u2.id,
            "u2_emp": e2.id,
            "c1_id": c1.id,
            "c2_id": c2.id,
        }


def _token(uid: int, emp: int, perm: int, name: str = "t") -> str:
    return create_access_token(
        {
            "user_id": uid,
            "employee_id": emp,
            "role": "teacher",
            "name": name,
            "permissions": perm,
            "token_version": 0,
        }
    )


class TestPortalMedicationsToday:
    def test_teacher_only_sees_own_classroom(self, med_client):
        client, sf = med_client
        seed = _seed_two_classrooms(sf)
        tk = _token(seed["u1_id"], seed["u1_emp"], seed["perm"])
        rsp = client.get("/api/portal/medications/today", cookies={"access_token": tk})
        assert rsp.status_code == 200, rsp.text
        groups = rsp.json()["groups"]
        # 老師 A 只看到自己的 A 班
        assert len(groups) == 1
        g = groups[0]
        assert g["classroom_name"] == "A班"
        # A 班今日 2 筆 log（pending + administered）
        statuses = sorted([i["status"] for i in g["items"]])
        assert statuses == ["administered", "pending"]
        assert g["stats"] == {"pending": 1, "administered": 1, "skipped": 0}
        # 沒有 B 班學生
        names = {i["student_name"] for i in g["items"]}
        assert "小華" not in names
        assert "小明" in names

    def test_classroom_filter_403_when_not_mine(self, med_client):
        client, sf = med_client
        seed = _seed_two_classrooms(sf)
        # 老師 A 帶 classroom_id=B 班 → 403
        tk = _token(seed["u1_id"], seed["u1_emp"], seed["perm"])
        rsp = client.get(
            f"/api/portal/medications/today?classroom_id={seed['c2_id']}",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 403
