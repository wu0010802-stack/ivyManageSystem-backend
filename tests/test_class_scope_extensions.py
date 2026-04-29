"""跨班學生資料 IDOR 回歸測試（F-022, F-023, F-024, F-025, F-034）。

涵蓋第二批班級 scope 擴充：
- F-022 student_change_logs：list / summary / export 套 student_ids_in_scope；
  CRUD（POST/PUT/DELETE）補 assert_student_access
- F-023 student_incidents / student_assessments：list 在 student_id /
  classroom_id 皆未帶時，對非 is_unrestricted caller 自動限縮
- F-024 students/records timeline：service 接受 current_user 參數，內部以
  student_ids_in_scope 限縮三類查詢
- F-025 students/{id}/guardians：endpoint 入口 assert_student_access
- F-034 fees /records：student_id filter 過 assert_student_access；
  非 is_unrestricted 不可不帶 student_id 列出全校（403）

依 tests/test_student_data_idor.py 的 fixture 模式實作。
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.fees import router as fees_router
from api.student_assessments import router as assessments_router
from api.student_change_logs import router as student_change_logs_router
from api.student_incidents import router as incidents_router
from api.students import router as students_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    Base,
    Classroom,
    Employee,
    Student,
    StudentAssessment,
    StudentIncident,
    User,
)
from models.fees import FeeItem, StudentFeeRecord
from models.guardian import Guardian
from models.student_log import StudentChangeLog
from utils.auth import hash_password
from utils.permissions import Permission

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scope_app(tmp_path):
    """建立隔離 FastAPI app（含五個 finding 涉及的 routers）。"""
    db_path = tmp_path / "class-scope.sqlite"
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
    # student_change_logs prefix=/api/students/change-logs，必須在 students_router
    # 之前註冊，否則被 /api/students/{student_id} 截走（同 communications）。
    app.include_router(student_change_logs_router)
    app.include_router(students_router)
    app.include_router(incidents_router)
    app.include_router(assessments_router)
    app.include_router(fees_router)

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


def _create_user(
    session,
    *,
    username: str,
    password: str,
    role: str,
    permissions: int,
    employee: Employee | None = None,
) -> User:
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


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _seed_two_classrooms(session) -> dict:
    """建立 2 班 + 各 1 學生；教師 A 是 A 班導、教師 B 是 B 班導。"""
    emp_a = _create_employee(session, "EA01", "教師 A")
    emp_b = _create_employee(session, "EB01", "教師 B")

    cls_a = Classroom(
        name="A 班",
        school_year=2025,
        semester=1,
        is_active=True,
        head_teacher_id=emp_a.id,
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
    for obj in (emp_a, emp_b, cls_a, cls_b, st_a, st_b):
        session.refresh(obj)
    return {
        "emp_a": emp_a,
        "emp_b": emp_b,
        "cls_a": cls_a,
        "cls_b": cls_b,
        "st_a": st_a,
        "st_b": st_b,
    }


_BASIC_STUDENT_PERMS = int(Permission.STUDENTS_READ | Permission.STUDENTS_WRITE)
_GUARDIANS_READ_PERMS = int(Permission.STUDENTS_READ | Permission.GUARDIANS_READ)
_FEES_READ_PERMS = int(Permission.FEES_READ)


# ---------------------------------------------------------------------------
# F-022 student_change_logs
# ---------------------------------------------------------------------------


class TestF022_StudentChangeLogs:
    def _seed_log(
        self,
        session,
        student_id: int,
        classroom_id: int,
        *,
        source: str = "manual",
    ) -> int:
        log = StudentChangeLog(
            student_id=student_id,
            school_year=114,
            semester=2,
            event_type="休學",
            event_date=date(2026, 4, 20),
            classroom_id=classroom_id,
            reason="家庭因素",
            notes="補登",
            source=source,
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        return log.id

    def test_teacher_list_filtered_to_own_class(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="cl_tA",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_STUDENT_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_log(s, seed["st_a"].id, seed["cls_a"].id)
            self._seed_log(s, seed["st_b"].id, seed["cls_b"].id)
            own_student_ids = {seed["st_a"].id}
        assert _login(client, "cl_tA", "Pass1234").status_code == 200
        r = client.get("/api/students/change-logs?school_year=114&semester=2")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        seen = {it["student_id"] for it in items}
        # 教師 A 只能看到自己班學生的 change log（且至少有那一筆）
        assert seen == own_student_ids, seen

    def test_teacher_cannot_get_other_class_log_by_id(self, scope_app):
        # PUT/DELETE 取出 log 後應 assert_student_access
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="cl_tA_put",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_STUDENT_PERMS,
                employee=seed["emp_a"],
            )
            log_id = self._seed_log(s, seed["st_b"].id, seed["cls_b"].id)
        assert _login(client, "cl_tA_put", "Pass1234").status_code == 200
        r = client.put(f"/api/students/change-logs/{log_id}", json={"notes": "竄改"})
        assert r.status_code == 403, r.text
        r2 = client.delete(f"/api/students/change-logs/{log_id}")
        assert r2.status_code == 403, r2.text

    def test_teacher_cannot_create_log_for_other_class(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="cl_tA_post",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_STUDENT_PERMS,
                employee=seed["emp_a"],
            )
            other_st = seed["st_b"].id
            s.commit()
        assert _login(client, "cl_tA_post", "Pass1234").status_code == 200
        r = client.post(
            "/api/students/change-logs",
            json={
                "student_id": other_st,
                "school_year": 114,
                "semester": 2,
                "event_type": "休學",
                "event_date": "2026-04-20",
                "reason": "竄改",
            },
        )
        assert r.status_code == 403, r.text

    def test_teacher_summary_and_export_filtered(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="cl_tA_sum",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_STUDENT_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_log(s, seed["st_a"].id, seed["cls_a"].id)
            self._seed_log(s, seed["st_b"].id, seed["cls_b"].id)
            self._seed_log(s, seed["st_b"].id, seed["cls_b"].id)
        assert _login(client, "cl_tA_sum", "Pass1234").status_code == 200
        # summary：教師 A 總數應只算自己班那一筆
        r = client.get("/api/students/change-logs/summary?school_year=114&semester=2")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 1
        # export：CSV 內只應包含自己班學生 row
        r2 = client.get("/api/students/change-logs/export?school_year=114&semester=2")
        assert r2.status_code == 200, r2.text
        body = r2.text
        assert "A 班學生" in body
        assert "B 班學生" not in body

    def test_admin_unrestricted(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="cl_admin",
                password="Pass1234",
                role="admin",
                permissions=int(Permission.STUDENTS_READ | Permission.STUDENTS_WRITE),
                employee=None,
            )
            log_id = self._seed_log(s, seed["st_b"].id, seed["cls_b"].id)
            other_st_id = seed["st_b"].id
        assert _login(client, "cl_admin", "Pass1234").status_code == 200
        r = client.get("/api/students/change-logs?school_year=114&semester=2")
        assert r.status_code == 200
        # admin 看得到 B 班的 log
        seen = {it["student_id"] for it in r.json()["items"]}
        assert other_st_id in seen, seen
        # PUT 跨班也應允許
        r2 = client.put(
            f"/api/students/change-logs/{log_id}", json={"notes": "admin 更新"}
        )
        assert r2.status_code == 200, r2.text


# ---------------------------------------------------------------------------
# F-023 student_incidents / student_assessments list
# ---------------------------------------------------------------------------


class TestF023_IncidentsAssessmentsList:
    def _seed_incident(self, session, student_id: int) -> int:
        inc = StudentIncident(
            student_id=student_id,
            incident_type="行為觀察",
            severity="輕微",
            occurred_at=datetime(2026, 4, 20, 10, 0),
            description="測試",
            parent_notified=False,
        )
        session.add(inc)
        session.commit()
        session.refresh(inc)
        return inc.id

    def _seed_assessment(self, session, student_id: int) -> int:
        asm = StudentAssessment(
            student_id=student_id,
            semester="114-2",
            assessment_type="月評",
            content="測試內容",
            assessment_date=date(2026, 4, 20),
        )
        session.add(asm)
        session.commit()
        session.refresh(asm)
        return asm.id

    def test_teacher_list_no_filter_returns_only_own_class_incidents(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="inc_tA",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_STUDENT_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_incident(s, seed["st_a"].id)
            self._seed_incident(s, seed["st_b"].id)
            own_id = seed["st_a"].id
        assert _login(client, "inc_tA", "Pass1234").status_code == 200
        r = client.get("/api/student-incidents")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        seen = {it["student_id"] for it in items}
        assert seen == {own_id}

    def test_teacher_list_no_filter_returns_only_own_class_assessments(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="asm_tA",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_STUDENT_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_assessment(s, seed["st_a"].id)
            self._seed_assessment(s, seed["st_b"].id)
            own_id = seed["st_a"].id
        assert _login(client, "asm_tA", "Pass1234").status_code == 200
        r = client.get("/api/student-assessments")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        seen = {it["student_id"] for it in items}
        assert seen == {own_id}

    def test_admin_no_filter_returns_school_wide(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="inc_admin",
                password="Pass1234",
                role="admin",
                permissions=_BASIC_STUDENT_PERMS,
                employee=None,
            )
            self._seed_incident(s, seed["st_a"].id)
            self._seed_incident(s, seed["st_b"].id)
            self._seed_assessment(s, seed["st_a"].id)
            self._seed_assessment(s, seed["st_b"].id)
        assert _login(client, "inc_admin", "Pass1234").status_code == 200
        r = client.get("/api/student-incidents")
        assert r.status_code == 200
        seen = {it["student_id"] for it in r.json()["items"]}
        assert len(seen) == 2  # both classes
        r2 = client.get("/api/student-assessments")
        assert r2.status_code == 200
        seen2 = {it["student_id"] for it in r2.json()["items"]}
        assert len(seen2) == 2

    def test_teacher_with_explicit_filter_still_blocked_for_other_class(
        self, scope_app
    ):
        # 既有路徑（帶 classroom_id 的）已有檢查 — 確保不被打破
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="inc_tA_block",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_STUDENT_PERMS,
                employee=seed["emp_a"],
            )
            other_cls = seed["cls_b"].id
            s.commit()
        assert _login(client, "inc_tA_block", "Pass1234").status_code == 200
        r = client.get(f"/api/student-incidents?classroom_id={other_cls}")
        assert r.status_code == 403
        r2 = client.get(f"/api/student-assessments?classroom_id={other_cls}")
        assert r2.status_code == 403


# ---------------------------------------------------------------------------
# F-024 students/records timeline
# ---------------------------------------------------------------------------


class TestF024_StudentsRecordsTimeline:
    def _seed_records(self, session, st_a_id: int, st_b_id: int):
        # 兩班各一筆 incident、assessment、change_log
        session.add_all(
            [
                StudentIncident(
                    student_id=st_a_id,
                    incident_type="行為",
                    severity="輕微",
                    occurred_at=datetime(2026, 4, 1, 10, 0),
                    description="A",
                    parent_notified=False,
                ),
                StudentIncident(
                    student_id=st_b_id,
                    incident_type="行為",
                    severity="輕微",
                    occurred_at=datetime(2026, 4, 2, 10, 0),
                    description="B",
                    parent_notified=False,
                ),
                StudentAssessment(
                    student_id=st_a_id,
                    semester="114-2",
                    assessment_type="月評",
                    content="A 評",
                    assessment_date=date(2026, 4, 3),
                ),
                StudentAssessment(
                    student_id=st_b_id,
                    semester="114-2",
                    assessment_type="月評",
                    content="B 評",
                    assessment_date=date(2026, 4, 4),
                ),
                StudentChangeLog(
                    student_id=st_a_id,
                    school_year=114,
                    semester=2,
                    event_type="休學",
                    event_date=date(2026, 4, 5),
                    source="manual",
                ),
                StudentChangeLog(
                    student_id=st_b_id,
                    school_year=114,
                    semester=2,
                    event_type="休學",
                    event_date=date(2026, 4, 6),
                    source="manual",
                ),
            ]
        )
        session.commit()

    def test_teacher_timeline_filtered_to_own_class_students(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="tl_tA",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_STUDENT_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_records(s, seed["st_a"].id, seed["st_b"].id)
            own_id = seed["st_a"].id
        assert _login(client, "tl_tA", "Pass1234").status_code == 200
        r = client.get("/api/students/records")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        # 不帶 classroom_id 也只能看到自己班學生的紀錄（三類）
        seen = {it["student_id"] for it in items}
        assert seen == {own_id}
        # 三類紀錄都應出現
        types = {it["record_type"] for it in items}
        assert types == {"incident", "assessment", "change_log"}

    def test_admin_timeline_returns_school_wide(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="tl_admin",
                password="Pass1234",
                role="admin",
                permissions=_BASIC_STUDENT_PERMS,
                employee=None,
            )
            self._seed_records(s, seed["st_a"].id, seed["st_b"].id)
        assert _login(client, "tl_admin", "Pass1234").status_code == 200
        r = client.get("/api/students/records")
        assert r.status_code == 200
        items = r.json()["items"]
        seen = {it["student_id"] for it in items}
        assert len(seen) == 2


# ---------------------------------------------------------------------------
# F-025 students/{id}/guardians
# ---------------------------------------------------------------------------


class TestF025_GuardiansList:
    def _seed_guardian(self, session, student_id: int) -> int:
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

    def test_teacher_cannot_list_other_class_guardians(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="gd_tA_other",
                password="Pass1234",
                role="staff",
                permissions=_GUARDIANS_READ_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_guardian(s, seed["st_b"].id)
            other_st = seed["st_b"].id
        assert _login(client, "gd_tA_other", "Pass1234").status_code == 200
        r = client.get(f"/api/students/{other_st}/guardians")
        assert r.status_code == 403, r.text

    def test_teacher_can_list_own_class_guardians(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="gd_tA_own",
                password="Pass1234",
                role="staff",
                permissions=_GUARDIANS_READ_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_guardian(s, seed["st_a"].id)
            own_st = seed["st_a"].id
        assert _login(client, "gd_tA_own", "Pass1234").status_code == 200
        r = client.get(f"/api/students/{own_st}/guardians")
        assert r.status_code == 200, r.text
        assert len(r.json()["items"]) >= 1

    def test_admin_unrestricted(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="gd_admin",
                password="Pass1234",
                role="admin",
                permissions=_GUARDIANS_READ_PERMS,
                employee=None,
            )
            self._seed_guardian(s, seed["st_b"].id)
            other_st = seed["st_b"].id
        assert _login(client, "gd_admin", "Pass1234").status_code == 200
        r = client.get(f"/api/students/{other_st}/guardians")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# F-034 fees /records
# ---------------------------------------------------------------------------


class TestF034_FeesRecords:
    def _seed_fee_records(self, session, st_a_id: int, st_b_id: int):
        item = FeeItem(
            name="月費",
            amount=3000,
            period="114-2",
            is_active=True,
        )
        session.add(item)
        session.flush()
        session.add_all(
            [
                StudentFeeRecord(
                    student_id=st_a_id,
                    student_name="A 班學生",
                    classroom_name="A 班",
                    fee_item_id=item.id,
                    fee_item_name="月費",
                    amount_due=3000,
                    amount_paid=0,
                    status="unpaid",
                    period="114-2",
                ),
                StudentFeeRecord(
                    student_id=st_b_id,
                    student_name="B 班學生",
                    classroom_name="B 班",
                    fee_item_id=item.id,
                    fee_item_name="月費",
                    amount_due=3000,
                    amount_paid=0,
                    status="unpaid",
                    period="114-2",
                ),
            ]
        )
        session.commit()

    def test_teacher_list_with_other_class_student_id(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="fee_tA_other",
                password="Pass1234",
                role="staff",
                permissions=_FEES_READ_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_fee_records(s, seed["st_a"].id, seed["st_b"].id)
            other_st = seed["st_b"].id
        assert _login(client, "fee_tA_other", "Pass1234").status_code == 200
        r = client.get(f"/api/fees/records?student_id={other_st}")
        assert r.status_code == 403, r.text

    def test_teacher_list_with_own_class_student_id(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="fee_tA_own",
                password="Pass1234",
                role="staff",
                permissions=_FEES_READ_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_fee_records(s, seed["st_a"].id, seed["st_b"].id)
            own_st = seed["st_a"].id
        assert _login(client, "fee_tA_own", "Pass1234").status_code == 200
        r = client.get(f"/api/fees/records?student_id={own_st}")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert all(it["student_id"] == own_st for it in items)

    def test_teacher_list_without_student_id_rejected(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="fee_tA_nofilter",
                password="Pass1234",
                role="staff",
                permissions=_FEES_READ_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_fee_records(s, seed["st_a"].id, seed["st_b"].id)
        assert _login(client, "fee_tA_nofilter", "Pass1234").status_code == 200
        r = client.get("/api/fees/records")
        assert r.status_code == 403, r.text

    def test_admin_unrestricted(self, scope_app):
        client, factory = scope_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="fee_admin",
                password="Pass1234",
                role="admin",
                permissions=_FEES_READ_PERMS,
                employee=None,
            )
            self._seed_fee_records(s, seed["st_a"].id, seed["st_b"].id)
            other_st = seed["st_b"].id
        assert _login(client, "fee_admin", "Pass1234").status_code == 200
        r1 = client.get("/api/fees/records")
        assert r1.status_code == 200
        assert r1.json()["total"] == 2
        r2 = client.get(f"/api/fees/records?student_id={other_st}")
        assert r2.status_code == 200
        assert all(it["student_id"] == other_st for it in r2.json()["items"])
