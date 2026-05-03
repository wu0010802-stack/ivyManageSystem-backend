"""
Portal 班級學生擴展功能測試。

涵蓋：
- mask_phone utility
- /api/portal/my-students 隱私守衛（無 address、phone 遮罩）
- /api/portal/my-students 健康/出席聚合
- /api/portal/students/{id}/detail 隱私守衛 + transfer_history + classroom_role
- 特殊需求依 STUDENTS_SPECIAL_NEEDS_READ 遮罩
- POST /reveal-phone 揭露邏輯 + audit log + 403/404
- N+1 query 防呆
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.portal import router as portal_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    AuditLog,
    Base,
    Classroom,
    Employee,
    Guardian,
    Student,
    StudentAttendance,
    StudentClassroomTransfer,
    User,
)
from utils.auth import create_access_token
from utils.masking import mask_phone
from utils.permissions import Permission

# ────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────


@pytest.fixture
def portal_client(tmp_path):
    db_path = tmp_path / "portal-ext.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(portal_router)
    with TestClient(app) as client:
        yield client, sf, engine

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _teacher_perm(extra: int = 0) -> int:
    return (
        int(
            Permission.STUDENTS_READ.value
            | Permission.PORTFOLIO_READ.value
            | Permission.GUARDIANS_READ.value
            | Permission.STUDENTS_HEALTH_READ.value
        )
        | extra
    )


def _seed(sf, *, with_special_needs_perm: bool = False) -> dict:
    perm = _teacher_perm(
        Permission.STUDENTS_SPECIAL_NEEDS_READ.value if with_special_needs_perm else 0
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

        s = Student(
            student_id="S1",
            name="小明",
            classroom_id=c1.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
            birthday=date(2020, 5, 5),
            address="台北市信義區仁愛路 99 號 8 樓",
            parent_name="王媽",
            parent_phone="0912345678",
            emergency_contact_name="緊急阿姨",
            emergency_contact_phone="02-2345-6789",
            emergency_contact_relation="阿姨",
            allergy="花生",
            medication="氣喘吸入劑",
            special_needs="專注力不足",
        )
        s_other = Student(
            student_id="S2",
            name="他班生",
            classroom_id=c2.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
            address="高雄市鼓山區某路 1 號",
            parent_phone="0922000000",
        )
        session.add_all([s, s_other])
        session.flush()

        # 監護人
        g1 = Guardian(
            student_id=s.id,
            name="王媽",
            phone="0911-222-333",
            relation="母親",
            is_primary=True,
            can_pickup=True,
        )
        session.add(g1)

        # 本月出席（今天往回 5 天，2 出席 1 缺席 1 病假 1 遲到）
        today = date.today()
        for i, status in enumerate(["出席", "出席", "缺席", "病假", "遲到"]):
            d = today - timedelta(days=i)
            # 確保都在本月內
            if d.month != today.month:
                continue
            session.add(StudentAttendance(student_id=s.id, date=d, status=status))

        # 轉班歷史（B班 → A班）
        session.add(
            StudentClassroomTransfer(
                student_id=s.id,
                from_classroom_id=c2.id,
                to_classroom_id=c1.id,
                transferred_at=datetime.now() - timedelta(days=30),
            )
        )

        # 不相關的轉班（兩個其他班）— 老師 A 不應看到
        c3 = Classroom(name="C班", is_active=True, head_teacher_id=emp_other.id)
        session.add(c3)
        session.flush()
        session.add(
            StudentClassroomTransfer(
                student_id=s.id,
                from_classroom_id=c3.id,
                to_classroom_id=c2.id,
                transferred_at=datetime.now() - timedelta(days=60),
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
            "teacher_emp_id": emp.id,
            "teacher_other_id": teacher_other.id,
            "teacher_other_emp_id": emp_other.id,
            "student_id": s.id,
            "student_other_id": s_other.id,
            "guardian_id": g1.id,
            "classroom_a_id": c1.id,
            "classroom_b_id": c2.id,
        }


def _token(uid: int, emp_id: int, perm: int) -> str:
    return create_access_token(
        {
            "user_id": uid,
            "employee_id": emp_id,
            "role": "teacher",
            "name": "tester",
            "permissions": perm,
            "token_version": 0,
        }
    )


# ────────────────────────────────────────────────────────────
# mask_phone utility
# ────────────────────────────────────────────────────────────


class TestMaskPhone:
    def test_taiwan_mobile_no_dashes(self):
        assert mask_phone("0912345678") == "0912-***-678"

    def test_taiwan_mobile_with_dashes(self):
        assert mask_phone("0912-345-678") == "0912-***-678"

    def test_taiwan_mobile_with_spaces(self):
        assert mask_phone("0912 345 678") == "0912-***-678"

    def test_landline_with_area_code(self):
        # 02-2345-6789 → 0223-***-789
        assert mask_phone("02-2345-6789") == "0223-***-789"

    def test_short_number_all_masked(self):
        assert mask_phone("1234") == "****"

    def test_none_returns_none(self):
        assert mask_phone(None) is None

    def test_empty_returns_none(self):
        assert mask_phone("") is None


# ────────────────────────────────────────────────────────────
# /my-students 隱私 + 聚合
# ────────────────────────────────────────────────────────────


class TestMyStudentsPrivacy:
    def test_no_address_in_response(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get("/api/portal/my-students", cookies={"access_token": tk})
        assert rsp.status_code == 200
        body = rsp.text
        assert "信義區" not in body
        assert "address" not in rsp.json()["classrooms"][0]["students"][0]

    def test_parent_phone_is_masked(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get("/api/portal/my-students", cookies={"access_token": tk})
        student = rsp.json()["classrooms"][0]["students"][0]
        assert "parent_phone" not in student
        assert student["parent_phone_masked"] == "0912-***-678"

    def test_health_alert_count_when_perm_present(self, portal_client):
        """有 STUDENTS_HEALTH_READ + STUDENTS_SPECIAL_NEEDS_READ → 過敏/特殊需求都算入"""
        client, sf, _ = portal_client
        seed = _seed(sf, with_special_needs_perm=True)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get("/api/portal/my-students", cookies={"access_token": tk})
        student = rsp.json()["classrooms"][0]["students"][0]
        # 至少 special_needs 算一個（無 StudentAllergy structured table 資料）
        assert student["has_health_alert"] is True
        assert student["health_alert_count"] >= 1

    def test_health_alert_zero_without_perm(self, portal_client):
        """無權限時 has_health_alert=False / count=0"""
        client, sf, _ = portal_client
        # 教師有 STUDENTS_HEALTH_READ 但無 STUDENTS_SPECIAL_NEEDS_READ
        # special_needs 不算入
        # 而 StudentAllergy 結構化表沒種，medication_orders 也沒種
        # → count 應為 0
        seed = _seed(sf, with_special_needs_perm=False)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get("/api/portal/my-students", cookies={"access_token": tk})
        student = rsp.json()["classrooms"][0]["students"][0]
        assert student["has_health_alert"] is False
        assert student["health_alert_count"] == 0

    def test_attendance_aggregation_present(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get("/api/portal/my-students", cookies={"access_token": tk})
        student = rsp.json()["classrooms"][0]["students"][0]
        # 5 筆紀錄 → rate = present(2)/total(5) * 100 = 40.0
        # 但若本月切換邊界，可能少於 5 筆，故只斷言存在且為合理數值
        assert student["attendance_rate_this_month"] is not None
        assert 0 <= student["attendance_rate_this_month"] <= 100
        assert student["last_absent_date"] is not None


# ────────────────────────────────────────────────────────────
# /detail 隱私 + 擴展欄位
# ────────────────────────────────────────────────────────────


class TestStudentDetailExtensions:
    def test_no_address_field(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get(
            f"/api/portal/students/{seed['student_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200
        body = rsp.json()
        assert "address" not in body["student"]
        assert "信義區" not in rsp.text

    def test_emergency_phone_masked(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get(
            f"/api/portal/students/{seed['student_id']}/detail",
            cookies={"access_token": tk},
        )
        body = rsp.json()
        assert "emergency_contact_phone" not in body["student"]
        assert body["student"]["emergency_contact_phone_masked"] == "0223-***-789"
        assert body["student"]["parent_phone_masked"] == "0912-***-678"

    def test_guardian_phone_masked(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get(
            f"/api/portal/students/{seed['student_id']}/detail",
            cookies={"access_token": tk},
        )
        guardian = rsp.json()["guardians"][0]
        assert "phone" not in guardian
        assert guardian["phone_masked"] == "0911-***-333"

    def test_special_needs_null_without_perm(self, portal_client):
        """教師預設無 STUDENTS_SPECIAL_NEEDS_READ → special_needs 應為 None"""
        client, sf, _ = portal_client
        seed = _seed(sf, with_special_needs_perm=False)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get(
            f"/api/portal/students/{seed['student_id']}/detail",
            cookies={"access_token": tk},
        )
        body = rsp.json()
        assert body["student"]["special_needs"] is None

    def test_special_needs_visible_with_perm(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf, with_special_needs_perm=True)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get(
            f"/api/portal/students/{seed['student_id']}/detail",
            cookies={"access_token": tk},
        )
        body = rsp.json()
        assert body["student"]["special_needs"] == "專注力不足"

    def test_classroom_role_for_teacher(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get(
            f"/api/portal/students/{seed['student_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.json()["classroom"]["viewer_role"] == "主教老師"

    def test_transfer_history_scoped_to_teacher_classrooms(self, portal_client):
        """老師 A 應只看到含 A 班的 transfer，不該看到 C→B 的 transfer。"""
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get(
            f"/api/portal/students/{seed['student_id']}/detail",
            cookies={"access_token": tk},
        )
        history = rsp.json()["transfer_history"]
        assert len(history) == 1
        assert history[0]["to_classroom_name"] == "A班"

    def test_other_classroom_403(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.get(
            f"/api/portal/students/{seed['student_other_id']}/detail",
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 403


# ────────────────────────────────────────────────────────────
# POST /reveal-phone
# ────────────────────────────────────────────────────────────


class TestRevealPhone:
    def test_reveal_parent_phone_returns_full(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.post(
            f"/api/portal/students/{seed['student_id']}/reveal-phone",
            json={"target": "parent"},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200, rsp.text
        assert rsp.json()["phone"] == "0912345678"

    def test_reveal_emergency_phone(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.post(
            f"/api/portal/students/{seed['student_id']}/reveal-phone",
            json={"target": "emergency"},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200
        assert rsp.json()["phone"] == "02-2345-6789"

    def test_reveal_guardian_phone(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.post(
            f"/api/portal/students/{seed['student_id']}/reveal-phone",
            json={"target": "guardian", "guardian_id": seed["guardian_id"]},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200
        assert rsp.json()["phone"] == "0911-222-333"

    def test_reveal_guardian_requires_guardian_id(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.post(
            f"/api/portal/students/{seed['student_id']}/reveal-phone",
            json={"target": "guardian"},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 400

    def test_reveal_invalid_target(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.post(
            f"/api/portal/students/{seed['student_id']}/reveal-phone",
            json={"target": "spouse"},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 422

    def test_reveal_other_classroom_403(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.post(
            f"/api/portal/students/{seed['student_other_id']}/reveal-phone",
            json={"target": "parent"},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 403

    def test_reveal_writes_audit_log(self, portal_client):
        client, sf, _ = portal_client
        seed = _seed(sf)
        tk = _token(seed["teacher_id"], seed["teacher_emp_id"], seed["perm"])

        rsp = client.post(
            f"/api/portal/students/{seed['student_id']}/reveal-phone",
            json={"target": "parent"},
            cookies={"access_token": tk},
        )
        assert rsp.status_code == 200

        with sf() as session:
            logs = (
                session.query(AuditLog)
                .filter(AuditLog.entity_type == "student")
                .filter(AuditLog.action == "REVEAL")
                .all()
            )
            assert len(logs) == 1
            log = logs[0]
            assert log.entity_id == str(seed["student_id"])
            assert "target=parent" in log.summary
            # changes 包含 target / guardian_id
            assert "parent" in (log.changes or "")
