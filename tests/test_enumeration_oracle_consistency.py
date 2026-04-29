"""IDOR 枚舉 oracle 一致化回歸測試（F-002, F-003, F-004, F-006, F-007, F-008,
F-009, F-010, F-011, F-029）。

統一斷言：所有「resource 不存在」與「resource 存在但非自己」回應 status code
與 detail 必須一致，避免攻擊者透過差異化 status code 枚舉 id 存在性。
"""

import os
import sys
from datetime import date, datetime, time, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.parent_portal import parent_router as parent_portal_router
from api.portal import router as portal_router
from models.activity import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    ActivitySession,
    RegistrationCourse,
)
from models.database import (
    Announcement,
    AnnouncementRecipient,
    Base,
    Classroom,
    Employee,
    Guardian,
    LeaveRecord,
    OvertimeRecord,
    Student,
    StudentLeaveRequest,
    User,
)
from models.dismissal import StudentDismissalCall
from models.fees import FeeItem, StudentFeeRecord
from utils.auth import create_access_token, hash_password
from utils.permissions import Permission

# ═══════════════════════════════════════════════════════════════════════════
# Common helpers
# ═══════════════════════════════════════════════════════════════════════════


def _setup_in_memory_db(tmp_path, name: str):
    db_path = tmp_path / f"{name}.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    return engine, session_factory, old_engine, old_session_factory


def _restore_db(old_engine, old_session_factory, engine):
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _setup_parent(
    session,
    *,
    line_user_id: str = "UF",
    student_name: str = "小明",
    classroom_name: str = "向日葵",
):
    """建立家長 + 學生 + Guardian。"""
    user = User(
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = (
        session.query(Classroom).filter(Classroom.name == classroom_name).first()
    )
    if not classroom:
        classroom = Classroom(name=classroom_name, is_active=True)
        session.add(classroom)
        session.flush()
    student = Student(
        student_id=f"S_{student_name}",
        name=student_name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="父親",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, guardian, student, classroom


def _parent_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permissions": 0,
            "token_version": user.token_version or 0,
        }
    )


def _create_employee(session, employee_id: str, name: str) -> Employee:
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _create_teacher_user(
    session,
    *,
    username: str,
    employee: Employee,
    permissions: int | None = None,
) -> User:
    if permissions is None:
        permissions = (
            Permission.STUDENTS_READ
            | Permission.STUDENTS_WRITE
            | Permission.DISMISSAL_CALLS_READ
            | Permission.DISMISSAL_CALLS_WRITE
            | Permission.ANNOUNCEMENTS_READ
        )
    user = User(
        employee_id=employee.id,
        username=username,
        password_hash=hash_password("TempPass123"),
        role="teacher",
        permissions=int(permissions),
        is_active=True,
        must_change_password=False,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _teacher_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permissions": user.permissions,
            "token_version": user.token_version or 0,
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# F-002 / F-003 / F-004：parent_portal fees / activity / leaves
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def parent_client(tmp_path):
    engine, sf, old_e, old_sf = _setup_in_memory_db(tmp_path, "enum-parent")
    app = FastAPI()
    app.include_router(parent_portal_router)
    with TestClient(app) as client:
        yield client, sf
    _restore_db(old_e, old_sf, engine)


class TestF002_FeesRecordsPayments:
    def _seed(self, session):
        user_a, _, student_a, _ = _setup_parent(
            session, line_user_id="UA", student_name="A", classroom_name="A班"
        )
        user_b, _, student_b, _ = _setup_parent(
            session, line_user_id="UB", student_name="B", classroom_name="B班"
        )
        item = FeeItem(name="學費", amount=10000, period="2026-1", is_active=True)
        session.add(item)
        session.flush()
        record_b = StudentFeeRecord(
            student_id=student_b.id,
            student_name=student_b.name,
            classroom_name="B班",
            fee_item_id=item.id,
            fee_item_name="學費",
            amount_due=10000,
            amount_paid=0,
            status="unpaid",
            period="2026-1",
        )
        session.add(record_b)
        session.flush()
        session.commit()
        return _parent_token(user_a), record_b.id

    def test_non_existent_record_id_returns_403(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            token_a, _ = self._seed(s)
        resp = client.get(
            "/api/parent/fees/records/999999/payments",
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "查無此資料或無權存取"

    def test_other_family_record_id_returns_403_same_detail(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            token_a, record_b_id = self._seed(s)
        resp_other = client.get(
            f"/api/parent/fees/records/{record_b_id}/payments",
            cookies={"access_token": token_a},
        )
        resp_missing = client.get(
            "/api/parent/fees/records/999999/payments",
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]

    def test_own_family_record_id_returns_200(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            user, _, student, _ = _setup_parent(s, line_user_id="UC", student_name="C")
            item = FeeItem(name="學費2", amount=5000, period="2026-1", is_active=True)
            s.add(item)
            s.flush()
            record = StudentFeeRecord(
                student_id=student.id,
                student_name=student.name,
                classroom_name="向日葵",
                fee_item_id=item.id,
                fee_item_name="學費",
                amount_due=5000,
                amount_paid=0,
                status="unpaid",
                period="2026-1",
            )
            s.add(record)
            s.flush()
            s.commit()
            token = _parent_token(user)
            rid = record.id
        resp = client.get(
            f"/api/parent/fees/records/{rid}/payments",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200


class TestF003_ActivityRegistrationsPayments:
    def _seed(self, session):
        user_a, _, student_a, _ = _setup_parent(
            session, line_user_id="UA3", student_name="A3", classroom_name="A3班"
        )
        user_b, _, student_b, _ = _setup_parent(
            session, line_user_id="UB3", student_name="B3", classroom_name="B3班"
        )
        reg_b = ActivityRegistration(
            student_name="B3",
            birthday="2020-01-01",
            class_name="B3班",
            is_active=True,
            school_year=115,
            semester=1,
            student_id=student_b.id,
            parent_phone="0911000003",
            classroom_id=student_b.classroom_id,
            match_status="manual",
            pending_review=False,
        )
        session.add(reg_b)
        session.flush()
        session.commit()
        return _parent_token(user_a), reg_b.id

    def test_non_existent_registration_id_returns_403(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            token, _ = self._seed(s)
        resp = client.get(
            "/api/parent/activity/registrations/999999/payments",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "查無此資料或無權存取"

    def test_other_family_registration_id_returns_403_same_detail(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            token_a, reg_b_id = self._seed(s)
        resp_other = client.get(
            f"/api/parent/activity/registrations/{reg_b_id}/payments",
            cookies={"access_token": token_a},
        )
        resp_missing = client.get(
            "/api/parent/activity/registrations/999999/payments",
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]

    def test_own_family_registration_id_returns_200(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            user, _, student, _ = _setup_parent(
                s, line_user_id="UD3", student_name="D3"
            )
            reg = ActivityRegistration(
                student_name="D3",
                birthday="2020-01-01",
                class_name="向日葵",
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student.id,
                parent_phone="0911000004",
                classroom_id=student.classroom_id,
                match_status="manual",
                pending_review=False,
            )
            s.add(reg)
            s.flush()
            s.commit()
            token = _parent_token(user)
            rid = reg.id
        resp = client.get(
            f"/api/parent/activity/registrations/{rid}/payments",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    def test_confirm_promotion_same_pattern(self, parent_client):
        """confirm-promotion 同 pattern：404 / 403 collapse 為 generic 403。"""
        client, sf = parent_client
        with sf() as s:
            token_a, reg_b_id = self._seed(s)
        body = {"course_id": 1}
        resp_other = client.post(
            f"/api/parent/activity/registrations/{reg_b_id}/confirm-promotion",
            json=body,
            cookies={"access_token": token_a},
        )
        resp_missing = client.post(
            "/api/parent/activity/registrations/999999/confirm-promotion",
            json=body,
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]
        assert resp_other.json()["detail"] == "查無此資料或無權存取"


class TestF004_ParentLeaves:
    def _seed(self, session):
        user_a, _, student_a, _ = _setup_parent(
            session, line_user_id="UA4", student_name="A4", classroom_name="A4班"
        )
        user_b, _, student_b, _ = _setup_parent(
            session, line_user_id="UB4", student_name="B4", classroom_name="B4班"
        )
        future = date.today() + timedelta(days=10)
        leave_b = StudentLeaveRequest(
            student_id=student_b.id,
            applicant_user_id=user_b.id,
            leave_type="病假",
            start_date=future,
            end_date=future,
            status="pending",
        )
        session.add(leave_b)
        session.flush()
        session.commit()
        return _parent_token(user_a), leave_b.id

    def test_get_non_existent_leave_id_returns_403(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            token, _ = self._seed(s)
        resp = client.get(
            "/api/parent/student-leaves/999999",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "查無此資料或無權存取"

    def test_get_other_family_leave_id_returns_403_same_detail(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            token_a, leave_b_id = self._seed(s)
        resp_other = client.get(
            f"/api/parent/student-leaves/{leave_b_id}",
            cookies={"access_token": token_a},
        )
        resp_missing = client.get(
            "/api/parent/student-leaves/999999",
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]

    def test_cancel_same_pattern(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            token_a, leave_b_id = self._seed(s)
        resp_other = client.post(
            f"/api/parent/student-leaves/{leave_b_id}/cancel",
            cookies={"access_token": token_a},
        )
        resp_missing = client.post(
            "/api/parent/student-leaves/999999/cancel",
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]
        assert resp_other.json()["detail"] == "查無此資料或無權存取"

    def test_own_family_get_returns_200(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            user, _, student, _ = _setup_parent(
                s, line_user_id="UD4", student_name="D4"
            )
            future = date.today() + timedelta(days=5)
            leave = StudentLeaveRequest(
                student_id=student.id,
                applicant_user_id=user.id,
                leave_type="病假",
                start_date=future,
                end_date=future,
                status="pending",
            )
            s.add(leave)
            s.flush()
            s.commit()
            token = _parent_token(user)
            lid = leave.id
        resp = client.get(
            f"/api/parent/student-leaves/{lid}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# F-006 / F-007 / F-008 / F-009 / F-010：portal endpoints
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def portal_client(tmp_path):
    engine, sf, old_e, old_sf = _setup_in_memory_db(tmp_path, "enum-portal")
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(portal_router)
    with TestClient(app) as client:
        yield client, sf
    _ip_attempts.clear()
    _account_failures.clear()
    _restore_db(old_e, old_sf, engine)


def _seed_two_classrooms_with_teachers(session):
    """建兩位教師各自管轄一班。回傳 (teacher_a, classroom_a, teacher_b, classroom_b)。"""
    emp_a = _create_employee(session, "T6A", "教師A")
    emp_b = _create_employee(session, "T6B", "教師B")
    classroom_a = Classroom(name="A班", is_active=True, head_teacher_id=emp_a.id)
    classroom_b = Classroom(name="B班", is_active=True, head_teacher_id=emp_b.id)
    session.add_all([classroom_a, classroom_b])
    session.flush()
    user_a = _create_teacher_user(session, username="teacher_a_enum", employee=emp_a)
    user_b = _create_teacher_user(session, username="teacher_b_enum", employee=emp_b)
    return emp_a, classroom_a, user_a, emp_b, classroom_b, user_b


class TestF006_DismissalCalls:
    def _seed(self, session):
        emp_a, c_a, user_a, emp_b, c_b, user_b = _seed_two_classrooms_with_teachers(
            session
        )
        student_b = Student(
            student_id="S_B6",
            name="B生",
            classroom_id=c_b.id,
            is_active=True,
        )
        session.add(student_b)
        session.flush()
        call_b = StudentDismissalCall(
            student_id=student_b.id,
            classroom_id=c_b.id,
            requested_by_user_id=user_b.id,
            status="pending",
            requested_at=datetime.now(),
        )
        session.add(call_b)
        session.flush()
        session.commit()
        return _teacher_token(user_a), call_b.id

    def test_non_existent_call_id_acknowledge_returns_403(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            token, _ = self._seed(s)
        resp = client.post(
            "/api/portal/dismissal-calls/999999/acknowledge",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "查無此通知或無權存取"

    def test_other_class_call_id_acknowledge_returns_403_same_detail(
        self, portal_client
    ):
        client, sf = portal_client
        with sf() as s:
            token_a, call_b_id = self._seed(s)
        resp_other = client.post(
            f"/api/portal/dismissal-calls/{call_b_id}/acknowledge",
            cookies={"access_token": token_a},
        )
        resp_missing = client.post(
            "/api/portal/dismissal-calls/999999/acknowledge",
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]

    def test_own_class_call_id_acknowledge_returns_200(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            emp_a = _create_employee(s, "T6X", "教師X")
            classroom = Classroom(name="X班", is_active=True, head_teacher_id=emp_a.id)
            s.add(classroom)
            s.flush()
            user_a = _create_teacher_user(s, username="t_own_ack", employee=emp_a)
            student = Student(
                student_id="S_X6",
                name="X生",
                classroom_id=classroom.id,
                is_active=True,
            )
            s.add(student)
            s.flush()
            call = StudentDismissalCall(
                student_id=student.id,
                classroom_id=classroom.id,
                requested_by_user_id=user_a.id,
                status="pending",
                requested_at=datetime.now(),
            )
            s.add(call)
            s.flush()
            s.commit()
            token = _teacher_token(user_a)
            cid = call.id
        resp = client.post(
            f"/api/portal/dismissal-calls/{cid}/acknowledge",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200


class TestF007_PortalIncidentsCreate:
    def _seed(self, session):
        emp_a, c_a, user_a, emp_b, c_b, user_b = _seed_two_classrooms_with_teachers(
            session
        )
        student_b = Student(
            student_id="S_B7",
            name="B生",
            classroom_id=c_b.id,
            is_active=True,
        )
        session.add(student_b)
        session.flush()
        session.commit()
        return _teacher_token(user_a), student_b.id

    def _payload(self, student_id: int) -> dict:
        return {
            "student_id": student_id,
            "incident_type": "意外受傷",
            "severity": "輕微",
            "occurred_at": "2026-04-28T10:00:00",
            "description": "測試",
            "action_taken": "已處理",
            "parent_notified": False,
        }

    def test_post_with_non_existent_student_id_returns_403(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            token, _ = self._seed(s)
        resp = client.post(
            "/api/portal/incidents",
            json=self._payload(999999),
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
        assert "查無此學生" in resp.json()["detail"]

    def test_post_with_other_class_student_id_returns_403_same_detail(
        self, portal_client
    ):
        client, sf = portal_client
        with sf() as s:
            token_a, student_b_id = self._seed(s)
        resp_other = client.post(
            "/api/portal/incidents",
            json=self._payload(student_b_id),
            cookies={"access_token": token_a},
        )
        resp_missing = client.post(
            "/api/portal/incidents",
            json=self._payload(999999),
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]

    def test_post_with_own_class_student_id_returns_201(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            emp_a = _create_employee(s, "T7X", "教師Y")
            classroom = Classroom(name="Y班", is_active=True, head_teacher_id=emp_a.id)
            s.add(classroom)
            s.flush()
            user_a = _create_teacher_user(s, username="t_own_inc", employee=emp_a)
            student = Student(
                student_id="S_Y7",
                name="Y生",
                classroom_id=classroom.id,
                is_active=True,
            )
            s.add(student)
            s.flush()
            s.commit()
            token = _teacher_token(user_a)
            sid = student.id
        resp = client.post(
            "/api/portal/incidents",
            json=self._payload(sid),
            cookies={"access_token": token},
        )
        assert resp.status_code == 201, resp.text


class TestF008_PortalAssessmentsCreate:
    def _seed(self, session):
        emp_a, c_a, user_a, emp_b, c_b, user_b = _seed_two_classrooms_with_teachers(
            session
        )
        student_b = Student(
            student_id="S_B8",
            name="B生",
            classroom_id=c_b.id,
            is_active=True,
        )
        session.add(student_b)
        session.flush()
        session.commit()
        return _teacher_token(user_a), student_b.id

    def _payload(self, student_id: int) -> dict:
        return {
            "student_id": student_id,
            "semester": "115-1",
            "assessment_type": "期中",
            "domain": "語文",
            "rating": "良",
            "content": "孩子表現良好",
            "suggestions": "繼續加油",
            "assessment_date": "2026-04-28",
        }

    def test_post_with_non_existent_student_id_returns_403(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            token, _ = self._seed(s)
        resp = client.post(
            "/api/portal/assessments",
            json=self._payload(999999),
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
        assert "查無此學生" in resp.json()["detail"]

    def test_post_with_other_class_student_id_returns_403_same_detail(
        self, portal_client
    ):
        client, sf = portal_client
        with sf() as s:
            token_a, student_b_id = self._seed(s)
        resp_other = client.post(
            "/api/portal/assessments",
            json=self._payload(student_b_id),
            cookies={"access_token": token_a},
        )
        resp_missing = client.post(
            "/api/portal/assessments",
            json=self._payload(999999),
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]

    def test_post_with_own_class_student_id_returns_201(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            emp_a = _create_employee(s, "T8X", "教師Z")
            classroom = Classroom(name="Z班", is_active=True, head_teacher_id=emp_a.id)
            s.add(classroom)
            s.flush()
            user_a = _create_teacher_user(s, username="t_own_assess", employee=emp_a)
            student = Student(
                student_id="S_Z8",
                name="Z生",
                classroom_id=classroom.id,
                is_active=True,
            )
            s.add(student)
            s.flush()
            s.commit()
            token = _teacher_token(user_a)
            sid = student.id
        resp = client.post(
            "/api/portal/assessments",
            json=self._payload(sid),
            cookies={"access_token": token},
        )
        assert resp.status_code == 201, resp.text


class TestF009_AnnouncementsRead:
    def _seed(self, session):
        emp_a = _create_employee(session, "T9A", "教師A9")
        emp_b = _create_employee(session, "T9B", "教師B9")
        user_a = _create_teacher_user(session, username="t_ann_a", employee=emp_a)
        user_b = _create_teacher_user(session, username="t_ann_b", employee=emp_b)
        # 公告 1：visible to all（無 recipients）
        ann_visible = Announcement(
            title="全員公告",
            content="任何人都看得到",
            priority="normal",
            is_pinned=False,
            created_by=emp_a.id,
            created_at=datetime.now(),
        )
        # 公告 2：only targeted to emp_b（A 看不到）
        ann_invisible = Announcement(
            title="僅 B 可看",
            content="B 限定",
            priority="normal",
            is_pinned=False,
            created_by=emp_a.id,
            created_at=datetime.now(),
        )
        session.add_all([ann_visible, ann_invisible])
        session.flush()
        session.add(
            AnnouncementRecipient(
                announcement_id=ann_invisible.id,
                employee_id=emp_b.id,
            )
        )
        session.flush()
        session.commit()
        return (
            _teacher_token(user_a),
            ann_visible.id,
            ann_invisible.id,
        )

    def test_mark_read_non_existent_returns_403(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            token, _, _ = self._seed(s)
        resp = client.post(
            "/api/portal/announcements/999999/read",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "查無此公告或無權存取"

    def test_mark_read_invisible_announcement_returns_403_same_detail(
        self, portal_client
    ):
        client, sf = portal_client
        with sf() as s:
            token_a, _, ann_invisible_id = self._seed(s)
        resp_invisible = client.post(
            f"/api/portal/announcements/{ann_invisible_id}/read",
            cookies={"access_token": token_a},
        )
        resp_missing = client.post(
            "/api/portal/announcements/999999/read",
            cookies={"access_token": token_a},
        )
        assert resp_invisible.status_code == 403
        assert resp_invisible.status_code == resp_missing.status_code
        assert resp_invisible.json()["detail"] == resp_missing.json()["detail"]

    def test_mark_read_invisible_does_not_write_announcement_read(self, portal_client):
        """F-009 副作用：不可見公告即使收到 mark-read 也不可寫入 AnnouncementRead，
        否則 unread-count 會被攻擊者污染。"""
        client, sf = portal_client
        with sf() as s:
            token_a, _, ann_invisible_id = self._seed(s)
        client.post(
            f"/api/portal/announcements/{ann_invisible_id}/read",
            cookies={"access_token": token_a},
        )
        with sf() as s:
            from models.database import AnnouncementRead

            count = (
                s.query(AnnouncementRead)
                .filter(AnnouncementRead.announcement_id == ann_invisible_id)
                .count()
            )
            assert count == 0

    def test_mark_read_visible_announcement_returns_200(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            token, ann_visible_id, _ = self._seed(s)
        resp = client.post(
            f"/api/portal/announcements/{ann_visible_id}/read",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200


class TestF010_PortalActivitySession:
    def _seed_no_own_class(self, session):
        """教師 A 無自班學生在此場次（場次中所有學生都屬於 B 班）。"""
        emp_a = _create_employee(session, "T10A", "教師A10")
        emp_b = _create_employee(session, "T10B", "教師B10")
        c_a = Classroom(name="A10班", is_active=True, head_teacher_id=emp_a.id)
        c_b = Classroom(name="B10班", is_active=True, head_teacher_id=emp_b.id)
        session.add_all([c_a, c_b])
        session.flush()
        user_a = _create_teacher_user(session, username="t_act_a", employee=emp_a)
        student_b = Student(
            student_id="S_B10",
            name="B生",
            classroom_id=c_b.id,
            is_active=True,
        )
        session.add(student_b)
        session.flush()
        course = ActivityCourse(
            name="繪畫", price=1000, school_year=115, semester=1, is_active=True
        )
        session.add(course)
        session.flush()
        reg_b = ActivityRegistration(
            student_name="B生",
            birthday="2020-01-01",
            class_name="B10班",
            is_active=True,
            school_year=115,
            semester=1,
            student_id=student_b.id,
            parent_phone="0911000010",
            classroom_id=c_b.id,
            match_status="manual",
            pending_review=False,
        )
        session.add(reg_b)
        session.flush()
        session.add(
            RegistrationCourse(
                registration_id=reg_b.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )
        sess = ActivitySession(
            course_id=course.id, session_date=date(2026, 4, 28), notes="測試場次"
        )
        session.add(sess)
        session.flush()
        session.commit()
        return _teacher_token(user_a), sess.id

    def _seed_own_class(self, session):
        emp_a = _create_employee(session, "T10X", "教師X10")
        c_a = Classroom(name="X10班", is_active=True, head_teacher_id=emp_a.id)
        session.add(c_a)
        session.flush()
        user_a = _create_teacher_user(session, username="t_act_own", employee=emp_a)
        student = Student(
            student_id="S_X10",
            name="X生",
            classroom_id=c_a.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        course = ActivityCourse(
            name="圍棋", price=1500, school_year=115, semester=1, is_active=True
        )
        session.add(course)
        session.flush()
        reg = ActivityRegistration(
            student_name="X生",
            birthday="2020-01-01",
            class_name="X10班",
            is_active=True,
            school_year=115,
            semester=1,
            student_id=student.id,
            parent_phone="0911000011",
            classroom_id=c_a.id,
            match_status="manual",
            pending_review=False,
        )
        session.add(reg)
        session.flush()
        session.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1500,
            )
        )
        sess = ActivitySession(
            course_id=course.id, session_date=date(2026, 4, 28), notes="自班場次"
        )
        session.add(sess)
        session.flush()
        session.commit()
        return _teacher_token(user_a), sess.id

    def test_get_session_with_no_own_class_students_returns_403(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            token, sid = self._seed_no_own_class(s)
        resp = client.get(
            f"/api/portal/activity/attendance/sessions/{sid}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "查無此場次或無權存取"

    def test_get_session_non_existent_same_detail(self, portal_client):
        """non-existent session id 也應 collapse 為同 generic 403。"""
        client, sf = portal_client
        with sf() as s:
            token, sid = self._seed_no_own_class(s)
        resp_other = client.get(
            f"/api/portal/activity/attendance/sessions/{sid}",
            cookies={"access_token": token},
        )
        resp_missing = client.get(
            "/api/portal/activity/attendance/sessions/999999",
            cookies={"access_token": token},
        )
        assert resp_other.status_code == 403
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]

    def test_get_session_with_own_class_students_returns_200(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            token, sid = self._seed_own_class(s)
        resp = client.get(
            f"/api/portal/activity/attendance/sessions/{sid}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200, resp.text


# ═══════════════════════════════════════════════════════════════════════════
# F-011：portal/leaves compensatory source_overtime_id
# ═══════════════════════════════════════════════════════════════════════════


class TestF011_PortalLeavesCompensatory:
    def _seed(self, session):
        emp_a = _create_employee(session, "T11A", "員工A11")
        emp_b = _create_employee(session, "T11B", "員工B11")
        user_a = _create_teacher_user(
            session,
            username="t_comp_a",
            employee=emp_a,
            permissions=Permission.STUDENTS_READ | Permission.STUDENTS_WRITE,
        )
        # B 的加班記錄（A 無權使用）
        ot_b = OvertimeRecord(
            employee_id=emp_b.id,
            overtime_date=date(2026, 4, 1),
            overtime_type="weekday",
            hours=2.0,
            use_comp_leave=True,
            comp_leave_granted=True,
            is_approved=True,
        )
        session.add(ot_b)
        session.flush()
        session.commit()
        return _teacher_token(user_a), ot_b.id, emp_a.id

    def _payload(self, ot_id: int) -> dict:
        # 用未來的工作日避免日期校驗問題
        future = date.today() + timedelta(days=14)
        # 找下個週一以避開週末
        while future.weekday() != 0:
            future += timedelta(days=1)
        return {
            "leave_type": "compensatory",
            "start_date": future.isoformat(),
            "end_date": future.isoformat(),
            "leave_hours": 1.0,
            "source_overtime_id": ot_id,
        }

    def test_compensatory_with_non_existent_overtime_id_returns_400_generic(
        self, portal_client
    ):
        client, sf = portal_client
        with sf() as s:
            token, _, _ = self._seed(s)
        resp = client.post(
            "/api/portal/my-leaves",
            json=self._payload(999999),
            cookies={"access_token": token},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "來源加班記錄無效或無權使用"

    def test_compensatory_with_other_employee_overtime_id_returns_400_same_generic(
        self, portal_client
    ):
        client, sf = portal_client
        with sf() as s:
            token_a, ot_b_id, _ = self._seed(s)
        resp_other = client.post(
            "/api/portal/my-leaves",
            json=self._payload(ot_b_id),
            cookies={"access_token": token_a},
        )
        resp_missing = client.post(
            "/api/portal/my-leaves",
            json=self._payload(999999),
            cookies={"access_token": token_a},
        )
        assert resp_other.status_code == 400
        assert resp_other.status_code == resp_missing.status_code
        assert resp_other.json()["detail"] == resp_missing.json()["detail"]

    def test_compensatory_with_own_overtime_id_passes_existence_check(
        self, portal_client
    ):
        """合法 own overtime → 不應該 hit「來源加班記錄無效或無權使用」400；
        通過存在性檢查後仍可能因其他下游驗證（如配額/排班）失敗，但 detail 必不同。"""
        client, sf = portal_client
        with sf() as s:
            emp = _create_employee(s, "T11Z", "員工Z11")
            user = _create_teacher_user(
                s,
                username="t_comp_own",
                employee=emp,
                permissions=Permission.STUDENTS_READ | Permission.STUDENTS_WRITE,
            )
            ot = OvertimeRecord(
                employee_id=emp.id,
                overtime_date=date(2026, 4, 1),
                overtime_type="weekday",
                hours=2.0,
                use_comp_leave=True,
                comp_leave_granted=True,
                is_approved=True,
            )
            s.add(ot)
            s.flush()
            s.commit()
            token = _teacher_token(user)
            ot_id = ot.id
        resp = client.post(
            "/api/portal/my-leaves",
            json=self._payload(ot_id),
            cookies={"access_token": token},
        )
        # 不論最終成敗（可能因排班/配額而 400），都不應出現 generic 「無效或無權使用」訊息。
        if resp.status_code != 201:
            assert resp.json()["detail"] != "來源加班記錄無效或無權使用"


# ═══════════════════════════════════════════════════════════════════════════
# F-029：activity/public update phone enumeration oracle
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def activity_public_client(tmp_path):
    engine, sf, old_e, old_sf = _setup_in_memory_db(tmp_path, "enum-activity-public")
    _public_register_limiter_instance._timestamps.clear()
    app = FastAPI()
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, sf
    _public_register_limiter_instance._timestamps.clear()
    _restore_db(old_e, old_sf, engine)


class TestF029_PublicUpdatePhone:
    def _seed_two_regs(self, session):
        from utils.academic import resolve_current_academic_term

        sy, sem = resolve_current_academic_term()
        classroom = Classroom(
            name="大象班", is_active=True, school_year=sy, semester=sem
        )
        session.add(classroom)
        session.flush()
        course = ActivityCourse(
            name="圍棋",
            price=1200,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        session.add(course)
        session.commit()
        return sy, sem

    def test_change_to_already_used_phone_returns_409_generic_no_oracle(
        self, activity_public_client
    ):
        client, sf = activity_public_client
        with sf() as s:
            sy, sem = self._seed_two_regs(s)

        # 家長 A 公開報名
        r_a = client.post(
            "/api/activity/public/register",
            json={
                "name": "甲生",
                "birthday": "2020-05-10",
                "parent_phone": "0911111111",
                "class": "大象班",
                "courses": [{"name": "圍棋"}],
                "supplies": [],
            },
        )
        assert r_a.status_code == 201, r_a.text
        reg_a_id = r_a.json()["id"]

        # 家長 B 用另一支電話報名
        r_b = client.post(
            "/api/activity/public/register",
            json={
                "name": "乙生",
                "birthday": "2020-06-15",
                "parent_phone": "0922222222",
                "class": "大象班",
                "courses": [{"name": "圍棋"}],
                "supplies": [],
            },
        )
        assert r_b.status_code == 201, r_b.text

        # A 改成 B 的電話
        r_upd = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_a_id,
                "name": "甲生",
                "birthday": "2020-05-10",
                "parent_phone": "0911111111",
                "new_parent_phone": "0922222222",
                "class": "大象班",
                "courses": [{"name": "圍棋"}],
                "supplies": [],
            },
        )
        assert r_upd.status_code == 409
        # 必須是 generic message（不洩漏是否「已被其他報名使用」）
        detail = r_upd.json()["detail"]
        assert "已被其他報名使用" not in detail
        assert "無法使用" in detail

    def test_change_to_unused_phone_returns_200(self, activity_public_client):
        client, sf = activity_public_client
        with sf() as s:
            sy, sem = self._seed_two_regs(s)

        r_a = client.post(
            "/api/activity/public/register",
            json={
                "name": "丙生",
                "birthday": "2020-07-20",
                "parent_phone": "0933333333",
                "class": "大象班",
                "courses": [{"name": "圍棋"}],
                "supplies": [],
            },
        )
        assert r_a.status_code == 201, r_a.text
        reg_a_id = r_a.json()["id"]

        # 改成沒人用的新電話
        r_upd = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_a_id,
                "name": "丙生",
                "birthday": "2020-07-20",
                "parent_phone": "0933333333",
                "new_parent_phone": "0944444444",
                "class": "大象班",
                "courses": [{"name": "圍棋"}],
                "supplies": [],
            },
        )
        assert r_upd.status_code == 200, r_upd.text
