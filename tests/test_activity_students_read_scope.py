"""S7（D1）：STUDENTS_READ:own_class 經才藝端點不得看全校學生 PII。

機制：ACTIVITY_* 不在 SCOPE_AWARE_CODES，api/activity/ 過去零處 scope 檢查；
持 ACTIVITY_WRITE + STUDENTS_READ:own_class 的自訂角色可經 /students/search
拿全校在籍學生（生日/學號/家長手機），各列表的 PII 遮罩判斷也只看
「是否持 STUDENTS_READ」（bare has_permission 對 :own_class 也回 True）。

修法：api/activity/_shared.resolve_student_pii_scope 以
PermissionGrant.scope 解析可視範圍（own_class → 管轄班級集合），
search 端點加班級過濾、各遮罩點改 per-row scope-aware。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    Base,
    Classroom,
    Employee,
    RegistrationCourse,
    Student,
    User,
)
from utils.auth import hash_password

PASSWORD = "Temp123456"


@pytest.fixture
def scope_client(tmp_path):
    db_path = tmp_path / "scope.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login(c, username):
    r = c.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r


def _seed(sf):
    """兩班 + 各一學生 + 各一筆報名；三種使用者：

    - own_teacher：自訂角色 activity_clerk + STUDENTS_READ:own_class，導師班=大象班
      （teacher 角色被 require_staff_permission 擋管理端，威脅模型是自訂角色）
    - all_staff  ：ACTIVITY_READ/WRITE + bare STUDENTS_READ（等價 :all）
    回傳 dict（id 們）。
    """
    with sf() as s:
        emp = Employee(
            employee_id="T001", name="王老師", base_salary=32000, is_active=True
        )
        s.add(emp)
        s.flush()
        c1 = Classroom(name="大象班", is_active=True, head_teacher_id=emp.id)
        c2 = Classroom(name="長頸鹿班", is_active=True)
        s.add_all([c1, c2])
        s.flush()
        st1 = Student(
            student_id="S001",
            name="自班生",
            is_active=True,
            classroom_id=c1.id,
            birthday=date(2020, 1, 1),
            parent_phone="0911111111",
        )
        st2 = Student(
            student_id="S002",
            name="他班生",
            is_active=True,
            classroom_id=c2.id,
            birthday=date(2020, 2, 2),
            parent_phone="0922222222",
        )
        s.add_all([st1, st2])
        s.flush()

        course = ActivityCourse(name="圍棋", price=1000, capacity=30, is_active=True)
        s.add(course)
        s.flush()

        from utils.academic import resolve_current_academic_term

        sy, sem = resolve_current_academic_term()
        regs = {}
        for key, st, cls in (("own", st1, c1), ("other", st2, c2)):
            reg = ActivityRegistration(
                student_name=st.name,
                birthday=st.birthday.isoformat(),
                class_name=cls.name,
                classroom_id=cls.id,
                student_id=st.id,
                parent_phone=st.parent_phone,
                is_active=True,
                match_status="matched",
                pending_review=True,
                school_year=sy,
                semester=sem,
            )
            s.add(reg)
            s.flush()
            s.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1000,
                )
            )
            regs[key] = reg.id

        sess = ActivitySession(
            course_id=course.id, session_date=date.today(), created_by="seed"
        )
        s.add(sess)
        s.flush()

        s.add(
            User(
                username="own_teacher",
                password_hash=hash_password(PASSWORD),
                role="activity_clerk",
                employee_id=emp.id,
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "STUDENTS_READ:own_class",
                    "GUARDIANS_READ",
                ],
                is_active=True,
            )
        )
        s.add(
            User(
                username="all_staff",
                password_hash=hash_password(PASSWORD),
                role="hr",
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "STUDENTS_READ",
                    "GUARDIANS_READ",
                ],
                is_active=True,
            )
        )
        ids = {
            "c1": c1.id,
            "c2": c2.id,
            "st1": st1.id,
            "st2": st2.id,
            "session": sess.id,
            "reg_own": regs["own"],
            "reg_other": regs["other"],
        }
        s.commit()
        return ids


# ── 1. /students/search ────────────────────────────────────────────────────


class TestSearchScopeFilter:
    def test_own_class_search_only_returns_own_students(self, scope_client):
        c, sf = scope_client
        _seed(sf)
        _login(c, "own_teacher")
        res = c.get("/api/activity/students/search", params={"q": "生"})
        assert res.status_code == 200, res.text
        names = [it["name"] for it in res.json()["items"]]
        assert names == ["自班生"]

    def test_all_scope_search_returns_all(self, scope_client):
        c, sf = scope_client
        _seed(sf)
        _login(c, "all_staff")
        res = c.get("/api/activity/students/search", params={"q": "生"})
        assert res.status_code == 200, res.text
        names = sorted(it["name"] for it in res.json()["items"])
        assert names == ["他班生", "自班生"]

    def test_own_class_without_classroom_returns_empty(self, scope_client):
        c, sf = scope_client
        _seed(sf)
        with sf() as s:
            s.add(
                User(
                    username="no_class_teacher",
                    password_hash=hash_password(PASSWORD),
                    role="activity_clerk",
                    permission_names=[
                        "ACTIVITY_READ",
                        "ACTIVITY_WRITE",
                        "STUDENTS_READ:own_class",
                    ],
                    is_active=True,
                )
            )
            s.commit()
        _login(c, "no_class_teacher")
        res = c.get("/api/activity/students/search", params={"q": "生"})
        assert res.status_code == 200, res.text
        assert res.json()["items"] == []


# ── 2. pending 清單 per-row 遮罩 ────────────────────────────────────────────


class TestPendingListScopeMasking:
    def test_own_class_masks_other_class_rows(self, scope_client):
        c, sf = scope_client
        _seed(sf)
        _login(c, "own_teacher")
        res = c.get("/api/activity/registrations/pending", params={"status": "pending"})
        assert res.status_code == 200, res.text
        by_name = {it["student_name"]: it for it in res.json()["items"]}
        assert by_name["自班生"]["birthday"] == "2020-01-01"
        assert by_name["自班生"]["classroom_id"] is not None
        assert by_name["他班生"]["birthday"] is None
        assert by_name["他班生"]["classroom_id"] is None

    def test_all_scope_sees_all_rows(self, scope_client):
        c, sf = scope_client
        _seed(sf)
        _login(c, "all_staff")
        res = c.get("/api/activity/registrations/pending", params={"status": "pending"})
        assert res.status_code == 200, res.text
        by_name = {it["student_name"]: it for it in res.json()["items"]}
        assert by_name["他班生"]["birthday"] == "2020-02-02"


# ── 3. registrations 列表 / 詳情 per-row 遮罩 ──────────────────────────────


class TestRegistrationsScopeMasking:
    def test_list_masks_other_class_rows(self, scope_client):
        c, sf = scope_client
        _seed(sf)
        _login(c, "own_teacher")
        res = c.get("/api/activity/registrations")
        assert res.status_code == 200, res.text
        by_name = {it["student_name"]: it for it in res.json()["items"]}
        assert by_name["自班生"]["birthday"] == "2020-01-01"
        assert by_name["自班生"]["student_id"] is not None
        assert by_name["他班生"]["birthday"] is None
        assert by_name["他班生"]["student_id"] is None
        assert by_name["他班生"]["classroom_id"] is None

    def test_detail_masks_other_class(self, scope_client):
        c, sf = scope_client
        ids = _seed(sf)
        _login(c, "own_teacher")
        res = c.get(f"/api/activity/registrations/{ids['reg_other']}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["birthday"] is None
        assert body["student_id"] is None
        assert body["classroom_id"] is None

    def test_detail_own_class_visible(self, scope_client):
        c, sf = scope_client
        ids = _seed(sf)
        _login(c, "own_teacher")
        res = c.get(f"/api/activity/registrations/{ids['reg_own']}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["birthday"] == "2020-01-01"
        assert body["student_id"] == ids["st1"]


# ── 4. 點名場次詳情 per-row 遮罩 ───────────────────────────────────────────


class TestSessionDetailScopeMasking:
    def test_own_class_masks_other_class_student_ids(self, scope_client):
        c, sf = scope_client
        ids = _seed(sf)
        _login(c, "own_teacher")
        res = c.get(f"/api/activity/attendance/sessions/{ids['session']}")
        assert res.status_code == 200, res.text
        by_name = {s["student_name"]: s for s in res.json()["students"]}
        assert by_name["自班生"]["student_id"] == ids["st1"]
        assert by_name["自班生"]["classroom_id"] == ids["c1"]
        assert by_name["他班生"]["student_id"] is None
        assert by_name["他班生"]["classroom_id"] is None

    def test_all_scope_unmasked(self, scope_client):
        c, sf = scope_client
        ids = _seed(sf)
        _login(c, "all_staff")
        res = c.get(f"/api/activity/attendance/sessions/{ids['session']}")
        assert res.status_code == 200, res.text
        by_name = {s["student_name"]: s for s in res.json()["students"]}
        assert by_name["他班生"]["student_id"] == ids["st2"]


# ── 5. POS outstanding-by-student per-group 遮罩 ───────────────────────────


class TestPosScopeMasking:
    def test_own_class_masks_other_group_birthday(self, scope_client):
        c, sf = scope_client
        _seed(sf)
        _login(c, "own_teacher")
        res = c.get("/api/activity/pos/outstanding-by-student")
        assert res.status_code == 200, res.text
        by_name = {g["student_name"]: g for g in res.json()["groups"]}
        assert by_name["自班生"]["birthday"] == "2020-01-01"
        assert by_name["他班生"]["birthday"] is None
