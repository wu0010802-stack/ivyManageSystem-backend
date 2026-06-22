"""T4：portal 場次詳情端點須 scope-aware FK 遮罩（P2 PII 洩漏修補）。

GET /api/portal/activity/attendance/sessions/{session_id} 原本呼叫
_build_session_detail_response 時未傳 mask_student_ids / student_pii_visible_classroom_ids，
導致 STUDENTS_READ:own_class 的教師可取得全校所有學生的 student_id 與 classroom_id。

修補後預期行為：
- own_class 教師：自班學生 FK 可見，他班學生 FK 為 None
- full-scope（bare STUDENTS_READ）：所有 FK 可見
- 兩者 registration_id / student_name / is_present 均不受影響
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portal import router as portal_router
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    RegistrationCourse,
)
from models.database import Base, Classroom, Employee, Student, User
from utils.auth import hash_password

PASSWORD = "Temp123456"


@pytest.fixture
def portal_client(tmp_path):
    db_path = tmp_path / "portal_session_scope.sqlite"
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
    app.include_router(portal_router)

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
    """兩班 + 各一學生 + 各一筆報名 + 一場次；三種使用者：

    - own_teacher：ACTIVITY_READ/WRITE + STUDENTS_READ:own_class，導師班 = 大象班（c1）
    - all_staff  ：ACTIVITY_READ/WRITE + bare STUDENTS_READ（等價 :all）
    - admin_user ：admin 角色（wildcard *）
    """
    with sf() as s:
        emp = Employee(
            employee_id="PT001", name="王老師", base_salary=32000, is_active=True
        )
        # all_staff 也需員工關聯（Finding 2：portal 點名端點要求 employee 身分）；
        # 此員工不掛任何班級導師欄位，bare STUDENTS_READ 仍為全園 scope。
        emp_staff = Employee(
            employee_id="HR001", name="行政", base_salary=32000, is_active=True
        )
        s.add_all([emp, emp_staff])
        s.flush()

        c1 = Classroom(name="大象班", is_active=True, head_teacher_id=emp.id)
        c2 = Classroom(name="長頸鹿班", is_active=True)
        s.add_all([c1, c2])
        s.flush()

        st1 = Student(
            student_id="PS001",
            name="自班生",
            is_active=True,
            classroom_id=c1.id,
            birthday=date(2020, 1, 1),
            parent_phone="0911111111",
        )
        st2 = Student(
            student_id="PS002",
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

        # own_teacher：只有 own_class scope
        s.add(
            User(
                username="portal_own_teacher",
                password_hash=hash_password(PASSWORD),
                role="activity_clerk",
                employee_id=emp.id,
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "STUDENTS_READ:own_class",
                ],
                is_active=True,
                must_change_password=False,
            )
        )

        # all_staff：bare STUDENTS_READ（全園可見）
        s.add(
            User(
                username="portal_all_staff",
                password_hash=hash_password(PASSWORD),
                role="hr",
                employee_id=emp_staff.id,
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "STUDENTS_READ",
                ],
                is_active=True,
                must_change_password=False,
            )
        )

        s.commit()

        return {
            "c1": c1.id,
            "c2": c2.id,
            "st1": st1.id,
            "st2": st2.id,
            "session": sess.id,
            "reg_own": regs["own"],
            "reg_other": regs["other"],
            "course": course.id,
        }


# ── 1. own_class 教師：他班學生 FK 應被遮罩 ───────────────────────────────────


class TestPortalSessionDetailScopeMasking:
    def test_own_class_masks_other_class_student_id_and_classroom_id(
        self, portal_client
    ):
        """(a) own_class 教師查場次詳情：他班學生 student_id & classroom_id 須為 None。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "portal_own_teacher")
        res = c.get(f"/api/portal/activity/attendance/sessions/{ids['session']}")
        assert res.status_code == 200, res.text
        by_name = {s["student_name"]: s for s in res.json()["students"]}
        # 他班生的 FK 必須被遮罩
        assert (
            by_name["他班生"]["student_id"] is None
        ), "PII 洩漏：他班生 student_id 未被遮罩"
        assert (
            by_name["他班生"]["classroom_id"] is None
        ), "PII 洩漏：他班生 classroom_id 未被遮罩"

    def test_own_class_sees_own_student_fk(self, portal_client):
        """(b) own_class 教師查場次詳情：自班學生 student_id & classroom_id 可見。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "portal_own_teacher")
        res = c.get(f"/api/portal/activity/attendance/sessions/{ids['session']}")
        assert res.status_code == 200, res.text
        by_name = {s["student_name"]: s for s in res.json()["students"]}
        # 自班生的 FK 必須可見
        assert (
            by_name["自班生"]["student_id"] == ids["st1"]
        ), "自班生 student_id 不應被遮罩"
        assert (
            by_name["自班生"]["classroom_id"] == ids["c1"]
        ), "自班生 classroom_id 不應被遮罩"

    def test_registration_id_and_name_preserved_for_own_class(self, portal_client):
        """(c) 反回歸：registration_id / student_name / is_present 在 own_class 者不受影響。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "portal_own_teacher")
        res = c.get(f"/api/portal/activity/attendance/sessions/{ids['session']}")
        assert res.status_code == 200, res.text
        by_name = {s["student_name"]: s for s in res.json()["students"]}
        # 自班
        assert by_name["自班生"]["registration_id"] == ids["reg_own"]
        assert by_name["自班生"]["student_name"] == "自班生"
        assert "is_present" in by_name["自班生"]
        # 他班（名稱與 registration_id 仍可見，僅 FK 遮罩）
        assert by_name["他班生"]["registration_id"] == ids["reg_other"]
        assert by_name["他班生"]["student_name"] == "他班生"
        assert "is_present" in by_name["他班生"]

    def test_full_scope_sees_all_fks(self, portal_client):
        """(c) full-scope（bare STUDENTS_READ）caller 可見所有 student_id & classroom_id。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "portal_all_staff")
        res = c.get(f"/api/portal/activity/attendance/sessions/{ids['session']}")
        assert res.status_code == 200, res.text
        by_name = {s["student_name"]: s for s in res.json()["students"]}
        assert (
            by_name["他班生"]["student_id"] == ids["st2"]
        ), "full-scope 不應遮罩他班生 student_id"
        assert (
            by_name["他班生"]["classroom_id"] == ids["c2"]
        ), "full-scope 不應遮罩他班生 classroom_id"
        assert by_name["自班生"]["student_id"] == ids["st1"]
        assert by_name["自班生"]["classroom_id"] == ids["c1"]

    def test_own_class_group_by_classroom_masks_other_classroom_id(self, portal_client):
        """group_by=classroom 模式下：非自班 group 的 classroom_id 也須被遮罩。"""
        c, sf = portal_client
        ids = _seed(sf)
        _login(c, "portal_own_teacher")
        res = c.get(
            f"/api/portal/activity/attendance/sessions/{ids['session']}",
            params={"group_by": "classroom"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert "groups" in data, "group_by=classroom 回應應包含 groups"
        # 找到對應他班的 group（classroom_id 應為 None 因為被遮罩）
        masked_groups = [g for g in data["groups"] if g.get("classroom_id") is None]
        # 至少有一組被遮罩（長頸鹿班）
        assert (
            len(masked_groups) >= 1
        ), "group_by=classroom 模式下他班 classroom_id 未被遮罩"
        # 自班的 group 應保留 classroom_id
        visible_groups = [
            g for g in data["groups"] if g.get("classroom_id") == ids["c1"]
        ]
        assert len(visible_groups) == 1, "自班 group 的 classroom_id 應可見"
