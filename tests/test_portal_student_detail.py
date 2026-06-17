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
from models.classroom import LIFECYCLE_ACTIVE, LIFECYCLE_GRADUATED
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
from models.portfolio import StudentAllergy, StudentMedicationOrder
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
    perm = ["STUDENTS_READ", "PORTFOLIO_READ", "STUDENTS_HEALTH_READ"]
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
            permission_names=perm,
            is_active=True,
            token_version=0,
        )
        teacher_other = User(
            username="t2",
            password_hash="!",
            role="teacher",
            employee_id=emp_other.id,
            permission_names=perm,
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
            "permission_names": perm,
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

    def test_health_arrays_hidden_without_health_read_perm(self, detail_client):
        """TPA-1：班級成員教師若 token 不含 STUDENTS_HEALTH_READ，detail 的結構化
        健康陣列（過敏/投藥）須為空——不可洩漏 §6 醫療資料（與 deprecated 扁平
        allergy_text/medication_text 欄位閘對齊）。"""
        client, sf = detail_client
        seed = _seed(sf)
        # 種一筆 7 天內投藥單，證明 recent_medication_orders 也會外洩
        with sf() as session:
            session.add(
                StudentMedicationOrder(
                    student_id=seed["student_my_id"],
                    order_date=date.today(),
                    medication_name="普拿疼",
                    dose="1 顆",
                    time_slots=["08:30"],
                )
            )
            session.commit()
        # 同班教師 t1，但 token 權限不含 STUDENTS_HEALTH_READ
        tk = _token(
            seed["teacher_id"],
            seed["teacher_emp_id"],
            "t1",
            ["STUDENTS_READ", "PORTFOLIO_READ"],
        )
        rsp = client.get(
            f"/api/portal/students/{seed['student_my_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text
        body = rsp.json()
        # deprecated 扁平欄位本就有閘
        assert body["student"]["allergy_text"] is None
        assert body["student"]["medication_text"] is None
        # 結構化健康陣列也必須遮蔽（修補前會洩漏 花生 + 普拿疼）
        assert body["health"]["allergies"] == [], body["health"]
        assert body["health"]["recent_medication_orders"] == [], body["health"]

    def test_health_arrays_visible_with_health_read_perm(self, detail_client):
        """TPA-1 對照：持有 STUDENTS_HEALTH_READ 的同班教師仍能讀結構化健康陣列。"""
        client, sf = detail_client
        seed = _seed(sf)
        tk = _token(
            seed["teacher_id"],
            seed["teacher_emp_id"],
            "t1",
            seed["perm"],  # 含 STUDENTS_HEALTH_READ
        )
        rsp = client.get(
            f"/api/portal/students/{seed['student_my_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text
        body = rsp.json()
        assert len(body["health"]["allergies"]) == 1
        assert body["health"]["allergies"][0]["allergen"] == "花生"

    def test_medical_access_logged_for_structured_only_health(self, detail_client):
        """TPA-1 補稽核盲區：即使 legacy 扁平 allergy/medication 欄為空，只要實際輸出
        結構化健康資料（過敏/投藥），§6 medical_access_log 仍須留痕。"""
        from models.medical_access_log import MedicalAccessLog

        client, sf = detail_client
        seed = _seed(sf)
        with sf() as session:
            stu = session.get(Student, seed["student_my_id"])
            stu.allergy = None  # 清掉 legacy 扁平欄
            stu.medication = None
            session.commit()
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], "t1", seed["perm"])
        rsp = client.get(
            f"/api/portal/students/{seed['student_my_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text
        # 結構化過敏（花生）有被輸出 → §6 須留一筆
        with sf() as session:
            logs = (
                session.query(MedicalAccessLog)
                .filter(MedicalAccessLog.student_id == seed["student_my_id"])
                .all()
            )
        assert len(logs) == 1, f"結構化健康資料輸出未留 §6 稽核: {len(logs)}"

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

    def test_terminal_lifecycle_own_class_student_403(self, detail_client):
        """R4-2：自班學生轉終態（畢業/退學）後，前班導不可再讀其 detail。"""
        client, sf = detail_client
        seed = _seed(sf)
        with sf() as session:
            stu = session.get(Student, seed["student_my_id"])
            stu.lifecycle_status = LIFECYCLE_GRADUATED
            stu.is_active = False
            session.commit()
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], "t1", seed["perm"])
        rsp = client.get(
            f"/api/portal/students/{seed['student_my_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 403, f"終態學生前班導不應可讀 detail: {rsp.text}"

    def test_terminal_lifecycle_own_class_reveal_phone_403(self, detail_client):
        """R4-2：終態學生的 reveal-phone 同樣須擋（避免揭未遮罩家長電話）。"""
        client, sf = detail_client
        seed = _seed(sf)
        with sf() as session:
            stu = session.get(Student, seed["student_my_id"])
            stu.lifecycle_status = LIFECYCLE_GRADUATED
            stu.is_active = False
            session.commit()
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], "t1", seed["perm"])
        rsp = client.post(
            f"/api/portal/students/{seed['student_my_id']}/reveal-phone",
            json={"target": "parent"},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 403, f"終態學生前班導不應可揭電話: {rsp.text}"

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
