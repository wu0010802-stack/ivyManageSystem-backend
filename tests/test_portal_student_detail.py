"""教師端學生個案彙總（/api/portal/students/{id}/detail）測試。"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.portal import router as portal_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    Base,
    Classroom,
    Employee,
    Guardian,
    Student,
    StudentAttendance,
    StudentContactBookEntry,
    User,
)
from models.portfolio import StudentAllergy
from utils.auth import create_access_token
from utils.permissions import Permission


@pytest.fixture
def detail_client(tmp_path):
    db_path = tmp_path / "detail.sqlite"
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


def _seed(sf) -> dict:
    perm = int(
        Permission.STUDENTS_READ.value
        | Permission.PORTFOLIO_READ.value
        | Permission.STUDENTS_HEALTH_READ.value
    )
    with sf() as session:
        emp = Employee(
            employee_id="E1", name="老師A", is_active=True, base_salary=30000
        )
        emp_other = Employee(
            employee_id="E2", name="老師B", is_active=True, base_salary=30000
        )
        session.add_all([emp, emp_other])
        session.flush()
        c1 = Classroom(name="A班", is_active=True, head_teacher_id=emp.id)
        c2 = Classroom(name="B班", is_active=True, head_teacher_id=emp_other.id)
        session.add_all([c1, c2])
        session.flush()
        s_in_my = Student(
            student_id="S1",
            name="小明",
            classroom_id=c1.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
            birthday=date(2020, 5, 5),
            allergy="(legacy)",
            special_needs="專注力不足",
        )
        s_in_other = Student(
            student_id="S2",
            name="他班生",
            classroom_id=c2.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        session.add_all([s_in_my, s_in_other])
        session.flush()
        # 監護人
        session.add(
            Guardian(
                student_id=s_in_my.id,
                name="王媽",
                phone="0912",
                relation="母親",
                is_primary=True,
                can_pickup=True,
            )
        )
        # 過敏（active + inactive）
        session.add(
            StudentAllergy(
                student_id=s_in_my.id,
                allergen="花生",
                severity="severe",
                active=True,
            )
        )
        session.add(
            StudentAllergy(
                student_id=s_in_my.id,
                allergen="塵蟎",
                severity="mild",
                active=False,
            )
        )
        # 30 天內出席
        today = date.today()
        for d, status in [
            (today - timedelta(days=1), "出席"),
            (today - timedelta(days=2), "缺席"),
            (today - timedelta(days=3), "病假"),
        ]:
            session.add(StudentAttendance(student_id=s_in_my.id, date=d, status=status))
        # 聯絡簿（取近 5）
        for i in range(7):
            session.add(
                StudentContactBookEntry(
                    student_id=s_in_my.id,
                    classroom_id=c1.id,
                    log_date=today - timedelta(days=i),
                    teacher_note=f"D{i}",
                )
            )

        teacher = User(
            username="t1",
            password_hash="!",
            role="teacher",
            employee_id=emp.id,
            permissions=perm,
            is_active=True,
            token_version=0,
        )
        teacher_other = User(
            username="t2",
            password_hash="!",
            role="teacher",
            employee_id=emp_other.id,
            permissions=perm,
            is_active=True,
            token_version=0,
        )
        session.add_all([teacher, teacher_other])
        session.commit()
        return {
            "perm": perm,
            "teacher_id": teacher.id,
            "teacher_other_id": teacher_other.id,
            "teacher_emp_id": emp.id,
            "teacher_other_emp_id": emp_other.id,
            "student_my_id": s_in_my.id,
            "student_other_id": s_in_other.id,
            "teacher_username": teacher.username,
            "teacher_other_username": teacher_other.username,
        }


def _token(uid: int, emp_id: int, username: str, perm: int) -> str:
    return create_access_token(
        {
            "user_id": uid,
            "employee_id": emp_id,
            "role": "teacher",
            "name": username,
            "permissions": perm,
            "token_version": 0,
        }
    )


class TestStudentDetail:
    def test_returns_full_payload_for_my_student(self, detail_client):
        client, sf = detail_client
        seed = _seed(sf)
        tk = _token(
            seed["teacher_id"],
            seed["teacher_emp_id"],
            "t1",
            seed["perm"],
        )
        rsp = client.get(
            f"/api/portal/students/{seed['student_my_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text
        body = rsp.json()
        assert body["student"]["name"] == "小明"
        assert body["student"]["birthday"] == "2020-05-05"
        assert body["classroom"]["name"] == "A班"
        # guardians
        assert len(body["guardians"]) == 1
        assert body["guardians"][0]["is_primary"] is True
        assert body["guardians"][0]["can_pickup"] is True
        # health.allergies 只列 active
        assert len(body["health"]["allergies"]) == 1
        assert body["health"]["allergies"][0]["allergen"] == "花生"
        # attendance summary
        assert body["attendance_30d"]["summary"]["present"] == 1
        assert body["attendance_30d"]["summary"]["absent"] == 1
        assert body["attendance_30d"]["summary"]["leave"] == 1
        # contact book 限 5 筆
        assert len(body["contact_book_recent"]) == 5

    def test_other_classroom_student_403(self, detail_client):
        client, sf = detail_client
        seed = _seed(sf)
        # 老師 A 嘗試看 B 班學生 → 403
        tk = _token(
            seed["teacher_id"],
            seed["teacher_emp_id"],
            "t1",
            seed["perm"],
        )
        rsp = client.get(
            f"/api/portal/students/{seed['student_other_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 403

    def test_404_when_student_missing(self, detail_client):
        client, sf = detail_client
        seed = _seed(sf)
        tk = _token(
            seed["teacher_id"],
            seed["teacher_emp_id"],
            "t1",
            seed["perm"],
        )
        rsp = client.get(
            "/api/portal/students/999999/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 404
