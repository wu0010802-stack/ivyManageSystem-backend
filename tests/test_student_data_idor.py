"""跨班學生資料 IDOR 回歸測試（F-018, F-019, F-020, F-021）。

涵蓋：
- F-018：students / classrooms / profile 端點下，未授權者讀健康欄位被遮罩
- F-019：student_communications CRUD 全部接班級 scope
- F-020：student_attendance batch / by-student / monthly / export / list 接班級 scope
- F-021：student_leaves list 接班級 scope（approve/reject 已於 Task 4 移除）

依 tests/test_portfolio_batch_a.py 與 tests/test_student_record_access_control.py
的 fixture 模式實作（SQLite in-memory + FastAPI TestClient）。
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.classrooms import router as classrooms_router
from api.student_attendance import router as student_attendance_router
from api.student_communications import router as student_communications_router
from api.student_leaves import router as student_leaves_router
from api.students import router as students_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    Base,
    Classroom,
    Employee,
    Student,
    StudentAttendance,
    StudentLeaveRequest,
    User,
)
from models.student_log import ParentCommunicationLog
from utils.auth import hash_password
from utils.permissions import Permission

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def idor_app(tmp_path):
    """建立隔離 FastAPI app（含 students / classrooms / communications /
    attendance / leaves 全部 routers）。"""
    db_path = tmp_path / "student-idor.sqlite"
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
    # 注意：student_communications 必須在 students_router 之前註冊，
    # 否則 /api/students/communications 會被 /api/students/{student_id} 截走（422）
    app.include_router(student_communications_router)
    app.include_router(students_router)
    app.include_router(classrooms_router)
    app.include_router(student_attendance_router)
    app.include_router(student_leaves_router)

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
    """建立 2 班 + 各 1 學生；教師 A 是 A 班導師、教師 B 是 B 班導師。

    學生包含敏感欄位以驗證遮罩。
    """
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
        allergy="花生",
        medication="氣喘吸入劑",
        special_needs="注意力不足",
    )
    st_b = Student(
        student_id="SB01",
        name="B 班學生",
        classroom_id=cls_b.id,
        is_active=True,
        enrollment_date=date(2025, 9, 1),
        lifecycle_status=LIFECYCLE_ACTIVE,
        allergy="海鮮",
        medication="抗組織胺",
        special_needs="ADHD",
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


# 一般教師（僅有 STUDENTS_READ/WRITE，無健康/特需）
_BASIC_PERMS = int(Permission.STUDENTS_READ | Permission.STUDENTS_WRITE)
# 教師加上健康讀取
_HEALTH_READ_PERMS = int(
    Permission.STUDENTS_READ
    | Permission.STUDENTS_WRITE
    | Permission.STUDENTS_HEALTH_READ
)
# 教師加上特殊需求讀取（無健康）
_SPECIAL_NEEDS_ONLY_PERMS = int(
    Permission.STUDENTS_READ
    | Permission.STUDENTS_WRITE
    | Permission.STUDENTS_SPECIAL_NEEDS_READ
)
# 教師持兩者全（健康 + 特需）
_FULL_PERMS = int(
    Permission.STUDENTS_READ
    | Permission.STUDENTS_WRITE
    | Permission.STUDENTS_HEALTH_READ
    | Permission.STUDENTS_SPECIAL_NEEDS_READ
    | Permission.CLASSROOMS_READ
)


# ---------------------------------------------------------------------------
# F-018 健康欄位遮罩
# ---------------------------------------------------------------------------


class TestF018HealthFieldMasking:
    def test_user_without_health_perm_sees_null_allergy_via_students_get(
        self, idor_app
    ):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            # 用 admin 角色搭配受限 perms：通過 staff gate 但缺健康/特需位元
            _create_user(
                s,
                username="adm_basic",
                password="Pass1234",
                role="admin",
                permissions=_BASIC_PERMS,
                employee=None,
            )
            st_a_id = seed["st_a"].id
            s.commit()
        assert _login(client, "adm_basic", "Pass1234").status_code == 200

        r = client.get(f"/api/students/{st_a_id}")
        assert r.status_code == 200
        body = r.json()
        # 缺 STUDENTS_HEALTH_READ：allergy / medication 必須遮罩成 None
        assert body["allergy"] is None
        assert body["medication"] is None
        # 缺 STUDENTS_SPECIAL_NEEDS_READ：special_needs 也應遮罩
        assert body["special_needs"] is None

    def test_user_without_health_perm_sees_null_in_students_list(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            _seed_two_classrooms(s)
            _create_user(
                s,
                username="adm_basic_list",
                password="Pass1234",
                role="admin",
                permissions=_BASIC_PERMS,
                employee=None,
            )
            s.commit()
        assert _login(client, "adm_basic_list", "Pass1234").status_code == 200
        r = client.get("/api/students")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) >= 1
        for item in items:
            assert item["allergy"] is None
            assert item["medication"] is None
            assert item["special_needs"] is None

    def test_user_without_health_perm_sees_null_medication_via_classroom_detail(
        self, idor_app
    ):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            # admin 角色但只給 CLASSROOMS_READ（缺健康/特需）→ 仍要遮罩
            _create_user(
                s,
                username="cls_reader",
                password="Pass1234",
                role="admin",
                permissions=int(Permission.STUDENTS_READ | Permission.CLASSROOMS_READ),
                employee=None,
            )
            cls_a_id = seed["cls_a"].id
            s.commit()
        assert _login(client, "cls_reader", "Pass1234").status_code == 200
        r = client.get(f"/api/classrooms/{cls_a_id}")
        assert r.status_code == 200
        body = r.json()
        assert len(body["students"]) >= 1
        for stu in body["students"]:
            assert stu["allergy"] is None
            assert stu["medication"] is None
            assert stu["special_needs"] is None

    def test_user_without_health_perm_sees_null_special_needs_via_profile(
        self, idor_app
    ):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="prof_basic",
                password="Pass1234",
                role="admin",
                permissions=_BASIC_PERMS,
                employee=None,
            )
            st_a_id = seed["st_a"].id
            s.commit()
        assert _login(client, "prof_basic", "Pass1234").status_code == 200
        r = client.get(f"/api/students/{st_a_id}/profile")
        assert r.status_code == 200, r.text
        body = r.json()
        # health 區塊內健康欄位均遮罩
        assert body["health"]["allergy"] is None
        assert body["health"]["medication"] is None
        assert body["health"]["special_needs"] is None

    def test_user_with_health_perm_sees_all_fields(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="t_a_full",
                password="Pass1234",
                role="admin",
                permissions=_FULL_PERMS,
                employee=None,
            )
            st_a_id = seed["st_a"].id
            cls_a_id = seed["cls_a"].id
            s.commit()
        assert _login(client, "t_a_full", "Pass1234").status_code == 200

        r = client.get(f"/api/students/{st_a_id}")
        assert r.json()["allergy"] == "花生"
        assert r.json()["medication"] == "氣喘吸入劑"
        assert r.json()["special_needs"] == "注意力不足"

        r2 = client.get(f"/api/classrooms/{cls_a_id}")
        assert any(s["allergy"] == "花生" for s in r2.json()["students"])

        r3 = client.get(f"/api/students/{st_a_id}/profile")
        assert r3.json()["health"]["allergy"] == "花生"
        assert r3.json()["health"]["special_needs"] == "注意力不足"

    def test_user_with_special_needs_only_sees_special_needs_but_null_allergy(
        self, idor_app
    ):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="t_only_sn",
                password="Pass1234",
                role="admin",
                permissions=_SPECIAL_NEEDS_ONLY_PERMS,
                employee=None,
            )
            st_a_id = seed["st_a"].id
            s.commit()
        assert _login(client, "t_only_sn", "Pass1234").status_code == 200
        r = client.get(f"/api/students/{st_a_id}")
        body = r.json()
        # 有 STUDENTS_SPECIAL_NEEDS_READ：special_needs 不遮罩
        assert body["special_needs"] == "注意力不足"
        # 缺 STUDENTS_HEALTH_READ：allergy / medication 仍遮罩
        assert body["allergy"] is None
        assert body["medication"] is None


# ---------------------------------------------------------------------------
# F-019 student_communications 班級 scope
# ---------------------------------------------------------------------------


class TestF019CommunicationsScope:
    def _seed_log(self, session, student_id: int) -> int:
        log = ParentCommunicationLog(
            student_id=student_id,
            communication_date=date(2026, 4, 20),
            communication_type="電話",
            topic="主題",
            content="內容",
            recorded_by=None,
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        return log.id

    def test_teacher_cannot_create_log_for_other_class_student(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="tA_create_other",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            other = seed["st_b"].id
            s.commit()
        assert _login(client, "tA_create_other", "Pass1234").status_code == 200
        r = client.post(
            "/api/students/communications",
            json={
                "student_id": other,
                "communication_date": "2026-04-20",
                "communication_type": "電話",
                "content": "嘗試竄改",
            },
        )
        assert r.status_code == 403, r.text

    def test_teacher_cannot_list_log_for_other_class(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="tA_list_other",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_log(s, seed["st_b"].id)
            other_classroom = seed["cls_b"].id
            s.commit()
        assert _login(client, "tA_list_other", "Pass1234").status_code == 200
        # classroom_id 帶 B 班 → 教師 A 必須 403
        r = client.get(f"/api/students/communications?classroom_id={other_classroom}")
        assert r.status_code == 403, r.text

    def test_teacher_list_no_filter_only_own_class(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="tA_list_default",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            self._seed_log(s, seed["st_a"].id)
            self._seed_log(s, seed["st_b"].id)
            own_student_ids = {seed["st_a"].id}
            s.commit()
        assert _login(client, "tA_list_default", "Pass1234").status_code == 200
        r = client.get("/api/students/communications")
        assert r.status_code == 200
        items = r.json()["items"]
        # 教師 A 不帶 filter，應只看到自己班學生的紀錄
        seen_ids = {item["student_id"] for item in items}
        assert seen_ids.issubset(own_student_ids)

    def test_teacher_cannot_update_log_for_other_class_student(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="tA_upd_other",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            log_id = self._seed_log(s, seed["st_b"].id)
        assert _login(client, "tA_upd_other", "Pass1234").status_code == 200
        r = client.put(
            f"/api/students/communications/{log_id}", json={"content": "竄改"}
        )
        assert r.status_code == 403, r.text

    def test_teacher_cannot_delete_log_for_other_class_student(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="tA_del_other",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            log_id = self._seed_log(s, seed["st_b"].id)
        assert _login(client, "tA_del_other", "Pass1234").status_code == 200
        r = client.delete(f"/api/students/communications/{log_id}")
        assert r.status_code == 403, r.text

    def test_teacher_can_crud_log_for_own_class_student(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="tA_own",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            own_id = seed["st_a"].id
            s.commit()
        assert _login(client, "tA_own", "Pass1234").status_code == 200
        # POST own
        r = client.post(
            "/api/students/communications",
            json={
                "student_id": own_id,
                "communication_date": "2026-04-20",
                "communication_type": "電話",
                "content": "正常紀錄",
            },
        )
        assert r.status_code == 201, r.text
        log_id = r.json()["id"]
        # PUT own
        r2 = client.put(
            f"/api/students/communications/{log_id}", json={"content": "更新內容"}
        )
        assert r2.status_code == 200, r2.text
        # DELETE own
        r3 = client.delete(f"/api/students/communications/{log_id}")
        assert r3.status_code == 200, r3.text

    def test_admin_unrestricted_can_access_other_class_log(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="admin_unr",
                password="Pass1234",
                role="admin",
                permissions=int(Permission.STUDENTS_READ | Permission.STUDENTS_WRITE),
                employee=None,
            )
            log_id = self._seed_log(s, seed["st_b"].id)
        assert _login(client, "admin_unr", "Pass1234").status_code == 200
        r = client.put(
            f"/api/students/communications/{log_id}", json={"content": "admin 更新"}
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# F-020 student_attendance 班級 scope
# ---------------------------------------------------------------------------


class TestF020AttendanceScope:
    def test_teacher_batch_with_other_class_student_id_rejected(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="att_tA",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            other_id = seed["st_b"].id
            own_id = seed["st_a"].id
            s.commit()
        assert _login(client, "att_tA", "Pass1234").status_code == 200
        # 嘗試把 B 班學生塞進 batch
        r = client.post(
            "/api/student-attendance/batch",
            json={
                "date": "2026-04-20",
                "entries": [
                    {"student_id": own_id, "status": "出席"},
                    {"student_id": other_id, "status": "缺席"},
                ],
            },
        )
        assert r.status_code == 403, r.text
        # 而且整批不可部分成功
        with factory() as s:
            cnt = (
                s.query(StudentAttendance)
                .filter(StudentAttendance.student_id == own_id)
                .count()
            )
            assert cnt == 0

    def test_teacher_get_by_student_other_class(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="att_tA_byStu",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            other_id = seed["st_b"].id
            s.commit()
        assert _login(client, "att_tA_byStu", "Pass1234").status_code == 200
        r = client.get(f"/api/student-attendance/by-student?student_id={other_id}")
        assert r.status_code == 403, r.text

    def test_teacher_get_other_classroom_daily_attendance(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="att_tA_daily",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            other_cls = seed["cls_b"].id
            s.commit()
        assert _login(client, "att_tA_daily", "Pass1234").status_code == 200
        r = client.get(
            f"/api/student-attendance?date=2026-04-20&classroom_id={other_cls}"
        )
        assert r.status_code == 403, r.text

    def test_teacher_can_view_own_class_student_attendance(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="att_tA_own",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            own_id = seed["st_a"].id
            own_cls = seed["cls_a"].id
            s.commit()
        assert _login(client, "att_tA_own", "Pass1234").status_code == 200
        r = client.get(f"/api/student-attendance/by-student?student_id={own_id}")
        assert r.status_code == 200
        r2 = client.post(
            "/api/student-attendance/batch",
            json={
                "date": "2026-04-20",
                "entries": [{"student_id": own_id, "status": "出席"}],
            },
        )
        assert r2.status_code == 200, r2.text
        r3 = client.get(
            f"/api/student-attendance?date=2026-04-20&classroom_id={own_cls}"
        )
        assert r3.status_code == 200

    def test_admin_unrestricted_attendance(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="att_admin",
                password="Pass1234",
                role="admin",
                permissions=int(Permission.STUDENTS_READ | Permission.STUDENTS_WRITE),
                employee=None,
            )
            other_id = seed["st_b"].id
            other_cls = seed["cls_b"].id
            s.commit()
        assert _login(client, "att_admin", "Pass1234").status_code == 200
        r = client.get(f"/api/student-attendance/by-student?student_id={other_id}")
        assert r.status_code == 200
        r2 = client.post(
            "/api/student-attendance/batch",
            json={
                "date": "2026-04-20",
                "entries": [{"student_id": other_id, "status": "出席"}],
            },
        )
        assert r2.status_code == 200, r2.text

    def test_teacher_export_full_school_rejected(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="att_tA_export",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            s.commit()
        assert _login(client, "att_tA_export", "Pass1234").status_code == 200
        # 全園 export（不帶 classroom_id）必須 403
        r = client.get("/api/student-attendance/export?year=2026&month=4")
        assert r.status_code == 403, r.text

    def test_teacher_export_other_classroom_rejected(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="att_tA_exp_o",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            other_cls = seed["cls_b"].id
            s.commit()
        assert _login(client, "att_tA_exp_o", "Pass1234").status_code == 200
        r = client.get(
            f"/api/student-attendance/export?year=2026&month=4&classroom_id={other_cls}"
        )
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# F-021 student_leaves list（approve/reject 已於 Task 4 移除，僅保留 list scope）
# ---------------------------------------------------------------------------


class TestF021LeavesList:
    def test_teacher_cannot_list_other_classroom_leaves(self, idor_app):
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            _create_user(
                s,
                username="lv_tA_list",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            other_cls = seed["cls_b"].id
            s.commit()
        assert _login(client, "lv_tA_list", "Pass1234").status_code == 200
        r = client.get(f"/api/student-leaves?classroom_id={other_cls}")
        assert r.status_code == 403, r.text

    def test_teacher_list_no_filter_only_own_class(self, idor_app):
        """無 classroom_id 參數時，教師應只看到自己班的請假。"""
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            user_a = _create_user(
                s,
                username="lv_tA_nofilter",
                password="Pass1234",
                role="staff",
                permissions=_BASIC_PERMS,
                employee=seed["emp_a"],
            )
            st_a_id = seed["st_a"].id
            st_b_id = seed["st_b"].id
            # A 班學生 approved leave
            leave_a = StudentLeaveRequest(
                student_id=st_a_id,
                applicant_user_id=user_a.id,
                leave_type="病假",
                start_date=date.today() + timedelta(days=1),
                end_date=date.today() + timedelta(days=1),
                status="approved",
            )
            # B 班學生 approved leave
            leave_b = StudentLeaveRequest(
                student_id=st_b_id,
                applicant_user_id=user_a.id,
                leave_type="事假",
                start_date=date.today() + timedelta(days=2),
                end_date=date.today() + timedelta(days=2),
                status="approved",
            )
            s.add_all([leave_a, leave_b])
            s.commit()
            leave_a_id = leave_a.id
        assert _login(client, "lv_tA_nofilter", "Pass1234").status_code == 200
        r = client.get("/api/student-leaves")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        ids = [item["id"] for item in items]
        assert leave_a_id in ids, "A 班請假應出現在結果中"
        assert all(
            item["student_id"] == st_a_id for item in items
        ), "結果應僅含 A 班學生，不應含 B 班"

    def test_admin_unrestricted_list_all(self, idor_app):
        """admin（is_unrestricted）無 classroom_id 應可看全部請假。"""
        client, factory = idor_app
        with factory() as s:
            seed = _seed_two_classrooms(s)
            admin = _create_user(
                s,
                username="lv_adm_all",
                password="Pass1234",
                role="admin",
                permissions=-1,
                employee=None,
            )
            leave_a = StudentLeaveRequest(
                student_id=seed["st_a"].id,
                applicant_user_id=admin.id,
                leave_type="病假",
                start_date=date.today() + timedelta(days=1),
                end_date=date.today() + timedelta(days=1),
                status="approved",
            )
            leave_b = StudentLeaveRequest(
                student_id=seed["st_b"].id,
                applicant_user_id=admin.id,
                leave_type="事假",
                start_date=date.today() + timedelta(days=2),
                end_date=date.today() + timedelta(days=2),
                status="approved",
            )
            s.add_all([leave_a, leave_b])
            s.commit()
            leave_a_id = leave_a.id
            leave_b_id = leave_b.id
        assert _login(client, "lv_adm_all", "Pass1234").status_code == 200
        r = client.get("/api/student-leaves")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        ids = [item["id"] for item in items]
        assert leave_a_id in ids, "admin 應能看到 A 班請假"
        assert leave_b_id in ids, "admin 應能看到 B 班請假"
