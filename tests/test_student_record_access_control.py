"""
回歸測試：學生評量與事件紀錄的班級所有權驗證（V3/V4）

Bug 描述：
    PUT /api/student-assessments/{id} 與 DELETE /api/student-assessments/{id}
    只驗證記錄是否存在，未驗證操作者是否屬於該學生的班級。
    教師 A 可修改/刪除教師 B 班級的評量與事件記錄。

修復方式：
    PUT/DELETE 取出記錄後，查詢學生的 classroom_id，
    再呼叫 _require_classroom_access() 驗證操作者班級歸屬。
"""

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
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.student_assessments import router as assessments_router
from api.student_incidents import router as incidents_router
from models.database import (
    Base, Classroom, Employee, Student,
    StudentAssessment, StudentIncident, User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def student_access_client(tmp_path):
    """建立隔離的 sqlite 測試 app（學生記錄存取控制用）。"""
    db_path = tmp_path / "student-access-control.sqlite"
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
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(assessments_router)
    app.include_router(incidents_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_employee(session, code: str, name: str) -> Employee:
    emp = Employee(employee_id=code, name=name, base_salary=36000, is_active=True)
    session.add(emp)
    session.flush()
    return emp


def _create_user(session, *, username, password, role, permissions, employee=None) -> User:
    user = User(
        employee_id=employee.id if employee else None,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# 有 STUDENTS_WRITE 的教師權限值
_STUDENTS_WRITE_PERM = int(Permission.STUDENTS_READ | Permission.STUDENTS_WRITE)


class TestAssessmentOwnershipGuard:
    """V3：評量記錄 PUT/DELETE 必須驗證班級所有權。"""

    def _setup_two_classrooms(self, session):
        """建立兩個班級、各一位教師、共同一位學生（屬於班級 A）。"""
        # 班級 A 教師
        teacher_a = _create_employee(session, "TA001", "教師甲")
        # 班級 B 教師
        teacher_b = _create_employee(session, "TB001", "教師乙")

        cls_a = Classroom(
            name="大班A", school_year=2025, semester=1, is_active=True,
            head_teacher_id=teacher_a.id,
        )
        cls_b = Classroom(
            name="中班B", school_year=2025, semester=1, is_active=True,
            head_teacher_id=teacher_b.id,
        )
        session.add_all([cls_a, cls_b])
        session.flush()

        student = Student(
            student_id="S_OWNER_01", name="王小明",
            classroom_id=cls_a.id, is_active=True, enrollment_date=date(2025, 9, 1),
        )
        session.add(student)
        session.flush()

        return teacher_a, teacher_b, cls_a, cls_b, student

    def test_put_assessment_by_other_classroom_teacher_returns_403(self, student_access_client):
        """教師 B 嘗試修改屬於班級 A 學生的評量記錄，應回傳 403。"""
        client, session_factory = student_access_client
        assessment_id = None
        with session_factory() as session:
            teacher_a, teacher_b, cls_a, cls_b, student = self._setup_two_classrooms(session)
            _create_user(session, username="teacher_b_upd", password="PassB123",
                         role="teacher", permissions=_STUDENTS_WRITE_PERM, employee=teacher_b)
            assessment = StudentAssessment(
                student_id=student.id,
                semester="2025-1",
                assessment_type="期中",
                content="良好",
                assessment_date=date(2026, 1, 10),
                recorded_by=None,
            )
            session.add(assessment)
            session.commit()
            assessment_id = assessment.id

        login_res = _login(client, "teacher_b_upd", "PassB123")
        assert login_res.status_code == 200

        res = client.put(
            f"/api/student-assessments/{assessment_id}",
            json={"content": "竄改內容"},
        )
        assert res.status_code == 403, (
            f"跨班級修改評量應回傳 403，但回傳 {res.status_code}: {res.json()}"
        )

    def test_delete_assessment_by_other_classroom_teacher_returns_403(self, student_access_client):
        """教師 B 嘗試刪除屬於班級 A 學生的評量記錄，應回傳 403。"""
        client, session_factory = student_access_client
        assessment_id = None
        with session_factory() as session:
            teacher_a, teacher_b, cls_a, cls_b, student = self._setup_two_classrooms(session)
            _create_user(session, username="teacher_b_del", password="PassBD123",
                         role="teacher", permissions=_STUDENTS_WRITE_PERM, employee=teacher_b)
            assessment = StudentAssessment(
                student_id=student.id,
                semester="2025-1",
                assessment_type="期末",
                content="待觀察",
                assessment_date=date(2026, 2, 15),
                recorded_by=None,
            )
            session.add(assessment)
            session.commit()
            assessment_id = assessment.id

        login_res = _login(client, "teacher_b_del", "PassBD123")
        assert login_res.status_code == 200

        res = client.delete(f"/api/student-assessments/{assessment_id}")
        assert res.status_code == 403, (
            f"跨班級刪除評量應回傳 403，但回傳 {res.status_code}: {res.json()}"
        )

    def test_own_classroom_teacher_can_update_assessment(self, student_access_client):
        """教師 A 修改自己班級的學生評量記錄，應成功（200）。"""
        client, session_factory = student_access_client
        assessment_id = None
        with session_factory() as session:
            teacher_a, _, cls_a, _, student = self._setup_two_classrooms(session)
            _create_user(session, username="teacher_a_own", password="PassA123",
                         role="teacher", permissions=_STUDENTS_WRITE_PERM, employee=teacher_a)
            assessment = StudentAssessment(
                student_id=student.id,
                semester="2025-2",
                assessment_type="學期",
                content="進步很多",
                assessment_date=date(2026, 3, 1),
                recorded_by=None,
            )
            session.add(assessment)
            session.commit()
            assessment_id = assessment.id

        login_res = _login(client, "teacher_a_own", "PassA123")
        assert login_res.status_code == 200

        res = client.put(
            f"/api/student-assessments/{assessment_id}",
            json={"content": "非常進步"},
        )
        assert res.status_code == 200


class TestIncidentOwnershipGuard:
    """V4：事件紀錄 PUT/DELETE 必須驗證班級所有權。"""

    def _setup_incident(self, session):
        teacher_a = _create_employee(session, "IC_TA", "事件教師甲")
        teacher_b = _create_employee(session, "IC_TB", "事件教師乙")
        cls_a = Classroom(
            name="小班A", school_year=2025, semester=2, is_active=True,
            head_teacher_id=teacher_a.id,
        )
        cls_b = Classroom(
            name="小班B", school_year=2025, semester=2, is_active=True,
            head_teacher_id=teacher_b.id,
        )
        session.add_all([cls_a, cls_b])
        session.flush()
        student = Student(
            student_id="S_INC_01", name="李小花",
            classroom_id=cls_a.id, is_active=True, enrollment_date=date(2025, 9, 1),
        )
        session.add(student)
        session.flush()
        return teacher_a, teacher_b, student

    def test_put_incident_by_other_classroom_teacher_returns_403(self, student_access_client):
        """教師 B 修改班級 A 學生的事件紀錄，應回傳 403。"""
        client, session_factory = student_access_client
        with session_factory() as session:
            teacher_a, teacher_b, student = self._setup_incident(session)
            _create_user(session, username="inc_b_put", password="IncBP123",
                         role="teacher", permissions=_STUDENTS_WRITE_PERM, employee=teacher_b)
            incident = StudentIncident(
                student_id=student.id,
                incident_type="衝突",
                occurred_at=datetime(2026, 3, 10, 9, 0),
                description="與同學口角",
            )
            session.add(incident)
            session.commit()
            incident_id = incident.id

        login_res = _login(client, "inc_b_put", "IncBP123")
        assert login_res.status_code == 200

        res = client.put(
            f"/api/student-incidents/{incident_id}",
            json={"description": "竄改事件內容"},
        )
        assert res.status_code == 403, (
            f"跨班級修改事件應回傳 403，但回傳 {res.status_code}: {res.json()}"
        )

    def test_delete_incident_by_other_classroom_teacher_returns_403(self, student_access_client):
        """教師 B 刪除班級 A 學生的事件紀錄（含霸凌/傷害記錄），應回傳 403。"""
        client, session_factory = student_access_client
        with session_factory() as session:
            teacher_a, teacher_b, student = self._setup_incident(session)
            _create_user(session, username="inc_b_del", password="IncBD123",
                         role="teacher", permissions=_STUDENTS_WRITE_PERM, employee=teacher_b)
            incident = StudentIncident(
                student_id=student.id,
                incident_type="傷害",
                severity="嚴重",
                occurred_at=datetime(2026, 3, 12, 14, 0),
                description="摔倒受傷",
            )
            session.add(incident)
            session.commit()
            incident_id = incident.id

        login_res = _login(client, "inc_b_del", "IncBD123")
        assert login_res.status_code == 200

        res = client.delete(f"/api/student-incidents/{incident_id}")
        assert res.status_code == 403, (
            f"跨班級刪除事件應回傳 403，但回傳 {res.status_code}: {res.json()}"
        )

    def test_own_classroom_teacher_can_delete_incident(self, student_access_client):
        """教師 A 刪除自己班級的學生事件紀錄，應成功（200）。"""
        client, session_factory = student_access_client
        with session_factory() as session:
            teacher_a, _, student = self._setup_incident(session)
            _create_user(session, username="inc_a_own", password="IncAO123",
                         role="teacher", permissions=_STUDENTS_WRITE_PERM, employee=teacher_a)
            incident = StudentIncident(
                student_id=student.id,
                incident_type="其他",
                occurred_at=datetime(2026, 3, 15, 10, 0),
                description="一般事件",
            )
            session.add(incident)
            session.commit()
            incident_id = incident.id

        login_res = _login(client, "inc_a_own", "IncAO123")
        assert login_res.status_code == 200

        res = client.delete(f"/api/student-incidents/{incident_id}")
        assert res.status_code == 200
