"""IDOR audit Phase 2：PII 欄位級遮罩（F-016/017/026/027/028）。

涵蓋 5 個 finding：
- F-016：bonus_preview 端點對非 admin/hr 遮罩 current_bonus / projected_bonus / estimated_bonus
- F-017：employees 列表/詳情對非 admin/hr 且非 self 遮罩 base_salary / hourly_rate / insurance_salary_level / pension_self_rate
- F-026：activity/registrations list/detail/pending 缺 STUDENTS_READ 遮罩 birthday，缺 GUARDIANS_READ 遮罩 parent_phone / email
- F-027：activity/registrations students/search 缺 STUDENTS_READ 直接 403
- F-028：activity/pos outstanding-by-student 缺 STUDENTS_READ 遮罩 birthday（保留 student_name / class_name）

所有遮罩規則基於 utils/portfolio_access.py 的 can_view_student_pii / can_view_guardian_pii
與 utils/salary_access.py 的 has_full_salary_view / can_view_salary_of。
"""

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock, patch

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
from api.bonus_preview import init_bonus_preview_services
from api.bonus_preview import router as bonus_preview_router
from api.employees import init_employee_services
from api.employees import router as employees_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    Employee,
    RegistrationCourse,
    Student,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission

# ─────────────────────────────────────────────────────────────────────────
# Shared client fixture（含 employees / bonus_preview / activity router）
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def pii_client(tmp_path):
    db_path = tmp_path / "pii.sqlite"
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

    fake_salary_engine = MagicMock()
    fake_salary_engine._school_wide_target = 160
    init_employee_services(fake_salary_engine)
    init_bonus_preview_services(fake_salary_engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(employees_router)
    app.include_router(bonus_preview_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(
    session,
    *,
    username,
    role,
    permissions,
    employee_id=None,
    password="Pass1234",
):
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=int(permissions),
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="Pass1234"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _create_employee(session, employee_id_str: str, name: str) -> Employee:
    emp = Employee(
        employee_id=employee_id_str,
        name=name,
        base_salary=42000,
        hourly_rate=200,
        insurance_salary_level=43900,
        pension_self_rate=0.06,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


# ─────────────────────────────────────────────────────────────────────────
# F-016：bonus_preview dashboard / impact-preview
# ─────────────────────────────────────────────────────────────────────────


class TestF016_BonusPreview:
    """非 admin/hr 看不到逐員 estimated_bonus / current_bonus / projected_bonus 金額。"""

    def _patch_compute(self, monkeypatch, classroom_id):
        """Patch _compute_all_bonus 回傳穩定的測試資料。"""
        sample = [
            {
                "employee_id": 101,
                "name": "李老師",
                "category": "帶班老師",
                "festivalBonus": 5000,
                "bonusBase": 1500,
                "remark": "",
                "targetEnrollment": 30,
                "classroom_id": classroom_id,
            },
            {
                "employee_id": 201,
                "name": "張主任",
                "category": "主管",
                "festivalBonus": 8000,
                "bonusBase": 2000,
                "remark": "",
                "targetEnrollment": 0,
                "classroom_id": None,
            },
        ]
        # current vs projected 不同（用於 impact-preview 的 diff）
        projected = [
            {**sample[0], "festivalBonus": 6000},
            {**sample[1], "festivalBonus": 9000},
        ]

        call_state = {"calls": 0}

        def fake_compute(session, engine, year, month, cls_count_map, school_total):
            call_state["calls"] += 1
            # 第一次呼叫回 current，之後回 projected
            return sample if call_state["calls"] == 1 else projected

        monkeypatch.setattr("api.bonus_preview._compute_all_bonus", fake_compute)

    def test_supervisor_with_students_read_sees_masked_bonus_amounts(
        self, pii_client, monkeypatch
    ):
        client, sf = pii_client
        with sf() as s:
            cls = Classroom(name="大班A", is_active=True)
            s.add(cls)
            s.flush()
            cls_id = cls.id
            _create_user(
                s,
                username="sv_bonus",
                role="supervisor",
                permissions=int(Permission.STUDENTS_READ)
                | int(Permission.STUDENTS_WRITE),
            )
            s.commit()

        self._patch_compute(monkeypatch, cls_id)

        _login(client, "sv_bonus")
        # dashboard
        res = client.get("/api/bonus-preview/dashboard?year=2026&month=4")
        assert res.status_code == 200, res.text
        data = res.json()
        # 各班教師 estimated_bonus / base_amount 應被遮罩
        for cr in data["classrooms"]:
            for t in cr["teachers"]:
                assert (
                    t["estimated_bonus"] is None
                ), f"estimated_bonus 應遮罩，實際 {t['estimated_bonus']}"
                assert (
                    t["base_amount"] is None
                ), f"base_amount 應遮罩，實際 {t['base_amount']}"
        # 全校總獎金亦應遮罩
        assert data["school_wide"]["estimated_total_bonus"] is None

        # impact-preview
        res2 = client.post(
            "/api/bonus-impact-preview",
            json={
                "operation": "add",
                "classroom_id": cls_id,
                "student_count_change": 1,
            },
        )
        assert res2.status_code == 200, res2.text
        d2 = res2.json()
        for cr in d2["affected_classrooms"]:
            for t in cr["teachers"]:
                assert t["current_bonus"] is None
                assert t["projected_bonus"] is None
                assert t["change"] is None
        for sw in d2["school_wide_impact"]:
            assert sw["current_bonus"] is None
            assert sw["projected_bonus"] is None
            assert sw["change"] is None

    def test_admin_sees_unmasked(self, pii_client, monkeypatch):
        client, sf = pii_client
        with sf() as s:
            cls = Classroom(name="大班A", is_active=True)
            s.add(cls)
            s.flush()
            cls_id = cls.id
            _create_user(s, username="adm_bonus", role="admin", permissions=-1)
            s.commit()

        self._patch_compute(monkeypatch, cls_id)

        _login(client, "adm_bonus")
        res = client.get("/api/bonus-preview/dashboard?year=2026&month=4")
        assert res.status_code == 200, res.text
        data = res.json()
        seen_amounts = []
        for cr in data["classrooms"]:
            for t in cr["teachers"]:
                seen_amounts.append(t["estimated_bonus"])
        assert 5000 in seen_amounts
        assert data["school_wide"]["estimated_total_bonus"] == 5000 + 8000

    def test_hr_sees_unmasked(self, pii_client, monkeypatch):
        client, sf = pii_client
        with sf() as s:
            cls = Classroom(name="大班A", is_active=True)
            s.add(cls)
            s.flush()
            cls_id = cls.id
            _create_user(
                s,
                username="hr_bonus",
                role="hr",
                permissions=int(Permission.STUDENTS_READ)
                | int(Permission.STUDENTS_WRITE)
                | int(Permission.SALARY_READ),
            )
            s.commit()

        self._patch_compute(monkeypatch, cls_id)

        _login(client, "hr_bonus")
        res = client.post(
            "/api/bonus-impact-preview",
            json={
                "operation": "add",
                "classroom_id": cls_id,
                "student_count_change": 1,
            },
        )
        assert res.status_code == 200, res.text
        d = res.json()
        # 至少一個教師應顯示真實金額
        any_teacher = any(
            t["current_bonus"] == 5000
            for cr in d["affected_classrooms"]
            for t in cr["teachers"]
        )
        any_school = any(sw["current_bonus"] == 8000 for sw in d["school_wide_impact"])
        assert any_teacher
        assert any_school


# ─────────────────────────────────────────────────────────────────────────
# F-017：employees list/detail
# ─────────────────────────────────────────────────────────────────────────


class TestF017_EmployeeListDetail:
    """非 admin/hr 且非 self 不可看 base_salary/hourly_rate/insurance_salary_level/pension_self_rate。"""

    SALARY_FIELDS = (
        "base_salary",
        "hourly_rate",
        "insurance_salary_level",
        "pension_self_rate",
    )

    def test_supervisor_without_salary_read_sees_masked_base_salary(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            self_emp = _create_employee(s, "S_self", "本人")
            other = _create_employee(s, "S_other", "他人")
            _create_user(
                s,
                username="sv_emp",
                role="supervisor",
                permissions=int(Permission.EMPLOYEES_READ),
                employee_id=self_emp.id,
            )
            s.commit()
            other_id = other.id

        _login(client, "sv_emp")
        res = client.get(f"/api/employees/{other_id}")
        assert res.status_code == 200, res.text
        body = res.json()
        for f in self.SALARY_FIELDS:
            assert body[f] is None, f"{f} 應遮罩，實際 {body[f]}"

    def test_self_view_unmasked(self, pii_client):
        """非 admin/hr 看自己時應看得到自己的薪資欄位。"""
        client, sf = pii_client
        with sf() as s:
            self_emp = _create_employee(s, "S_self2", "本人2")
            _create_user(
                s,
                username="sv_emp2",
                role="supervisor",
                permissions=int(Permission.EMPLOYEES_READ),
                employee_id=self_emp.id,
            )
            s.commit()
            self_id = self_emp.id

        _login(client, "sv_emp2")
        res = client.get(f"/api/employees/{self_id}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["base_salary"] == 42000
        assert body["hourly_rate"] == 200
        assert body["insurance_salary_level"] == 43900
        assert body["pension_self_rate"] == 0.06

    def test_admin_unmasked(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            other = _create_employee(s, "S_other3", "他人3")
            _create_user(s, username="adm_emp", role="admin", permissions=-1)
            s.commit()
            other_id = other.id

        _login(client, "adm_emp")
        res = client.get(f"/api/employees/{other_id}")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["base_salary"] == 42000

    def test_custom_role_with_employees_read_only_masked(self, pii_client):
        """自訂角色（非 admin/hr）持 EMPLOYEES_READ 但無 SALARY_READ — list 端點所有他人皆遮罩。"""
        client, sf = pii_client
        with sf() as s:
            self_emp = _create_employee(s, "S_self4", "本人4")
            _create_employee(s, "S_other4a", "他人4a")
            _create_employee(s, "S_other4b", "他人4b")
            _create_user(
                s,
                username="custom_emp",
                role="hr_lite",
                permissions=int(Permission.EMPLOYEES_READ),
                employee_id=self_emp.id,
            )
            s.commit()
            self_id = self_emp.id

        _login(client, "custom_emp")
        res = client.get("/api/employees")
        assert res.status_code == 200, res.text
        rows = res.json()
        assert len(rows) >= 3
        for row in rows:
            if row["id"] == self_id:
                # 自己應看得到
                assert row["base_salary"] == 42000
            else:
                # 他人應遮罩
                for f in self.SALARY_FIELDS:
                    assert row[f] is None, f"{row['name']} {f} 應遮罩，實際 {row[f]}"


# ─────────────────────────────────────────────────────────────────────────
# F-026：activity/registrations list/detail/pending
# ─────────────────────────────────────────────────────────────────────────


def _setup_activity_reg(
    session,
    *,
    student_name="王小明",
    birthday="2020-01-01",
    parent_phone="0912345678",
    email="parent@example.com",
    pending=False,
) -> ActivityRegistration:
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = (
        session.query(ActivityCourse)
        .filter(
            ActivityCourse.name == "美術",
            ActivityCourse.school_year == sy,
            ActivityCourse.semester == sem,
        )
        .first()
    )
    if not course:
        course = ActivityCourse(
            name="美術",
            price=1500,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
        )
        session.add(course)
        session.flush()

    reg = ActivityRegistration(
        student_name=student_name,
        birthday=birthday,
        class_name="大班A",
        classroom_id=None,
        student_id=None,
        parent_phone=parent_phone,
        email=email,
        is_active=True,
        pending_review=pending,
        match_status="pending" if pending else "matched",
        school_year=sy,
        semester=sem,
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
    session.flush()
    return reg


class TestF026_RegistrationsList:
    """list/detail/pending：缺 STUDENTS_READ 遮罩 birthday；缺 GUARDIANS_READ 遮罩 parent_phone/email。"""

    def test_caller_without_students_read_sees_birthday_masked(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            reg = _setup_activity_reg(s)
            # 自訂角色僅 ACTIVITY_READ + GUARDIANS_READ（缺 STUDENTS_READ）
            _create_user(
                s,
                username="act_no_st",
                role="activity_admin",
                permissions=int(Permission.ACTIVITY_READ)
                | int(Permission.GUARDIANS_READ),
            )
            s.commit()
            reg_id = reg.id

        _login(client, "act_no_st")
        # list
        res = client.get("/api/activity/registrations")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["total"] >= 1
        for item in data["items"]:
            assert item["birthday"] is None, item
            assert item["student_id"] is None, item
            assert item["classroom_id"] is None, item
            # parent_phone / email 仍可見（持 GUARDIANS_READ）
            assert item["parent_phone"] == "0912345678"
            assert item["email"] == "parent@example.com"

        # detail
        res2 = client.get(f"/api/activity/registrations/{reg_id}")
        assert res2.status_code == 200, res2.text
        d = res2.json()
        assert d["birthday"] is None
        assert d["student_id"] is None
        assert d["classroom_id"] is None
        assert d["parent_phone"] == "0912345678"
        assert d["email"] == "parent@example.com"

    def test_caller_without_guardians_read_sees_parent_phone_masked(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            reg = _setup_activity_reg(s, pending=True)
            # 自訂角色僅 ACTIVITY_READ + STUDENTS_READ（缺 GUARDIANS_READ）
            _create_user(
                s,
                username="act_no_gd",
                role="activity_admin",
                permissions=int(Permission.ACTIVITY_READ)
                | int(Permission.STUDENTS_READ),
            )
            s.commit()
            reg_id = reg.id

        _login(client, "act_no_gd")
        # list
        res = client.get("/api/activity/registrations")
        assert res.status_code == 200, res.text
        data = res.json()
        for item in data["items"]:
            assert item["parent_phone"] is None
            assert item["email"] is None
            assert item["birthday"] == "2020-01-01"  # 持 STUDENTS_READ 仍可見

        # pending
        res_p = client.get("/api/activity/registrations/pending")
        assert res_p.status_code == 200, res_p.text
        for item in res_p.json()["items"]:
            assert item["parent_phone"] is None
            assert item["email"] is None
            assert item["birthday"] == "2020-01-01"

        # detail
        res2 = client.get(f"/api/activity/registrations/{reg_id}")
        assert res2.status_code == 200, res2.text
        d = res2.json()
        assert d["parent_phone"] is None
        assert d["email"] is None
        assert d["birthday"] == "2020-01-01"

    def test_admin_unmasked(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            reg = _setup_activity_reg(s)
            _create_user(s, username="adm_act", role="admin", permissions=-1)
            s.commit()
            reg_id = reg.id

        _login(client, "adm_act")
        res = client.get(f"/api/activity/registrations/{reg_id}")
        assert res.status_code == 200, res.text
        d = res.json()
        assert d["birthday"] == "2020-01-01"
        assert d["parent_phone"] == "0912345678"
        assert d["email"] == "parent@example.com"

    def test_caller_with_both_perms_unmasked(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            reg = _setup_activity_reg(s)
            _create_user(
                s,
                username="act_full",
                role="activity_admin",
                permissions=int(Permission.ACTIVITY_READ)
                | int(Permission.STUDENTS_READ)
                | int(Permission.GUARDIANS_READ),
            )
            s.commit()
            reg_id = reg.id

        _login(client, "act_full")
        res = client.get(f"/api/activity/registrations/{reg_id}")
        assert res.status_code == 200, res.text
        d = res.json()
        assert d["birthday"] == "2020-01-01"
        assert d["parent_phone"] == "0912345678"
        assert d["email"] == "parent@example.com"


# ─────────────────────────────────────────────────────────────────────────
# F-027：activity/students/search
# ─────────────────────────────────────────────────────────────────────────


def _setup_search_student(session) -> Student:
    cls = Classroom(name="大班A", is_active=True)
    session.add(cls)
    session.flush()
    st = Student(
        student_id="S001",
        name="王小明",
        birthday=date(2020, 1, 1),
        parent_phone="0912345678",
        is_active=True,
        classroom_id=cls.id,
    )
    session.add(st)
    session.flush()
    return st


class TestF027_RegistrationsStudentsSearch:
    """students/search：缺 STUDENTS_READ → 403。"""

    def test_caller_without_students_read_403(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            _setup_search_student(s)
            _create_user(
                s,
                username="search_no",
                role="activity_admin",
                permissions=int(Permission.ACTIVITY_WRITE),
            )
            s.commit()

        _login(client, "search_no")
        res = client.get("/api/activity/students/search?q=王")
        assert res.status_code == 403, res.text

    def test_caller_with_students_read_200(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            _setup_search_student(s)
            _create_user(
                s,
                username="search_yes",
                role="activity_admin",
                permissions=int(Permission.ACTIVITY_WRITE)
                | int(Permission.STUDENTS_READ),
            )
            s.commit()

        _login(client, "search_yes")
        res = client.get("/api/activity/students/search?q=王")
        assert res.status_code == 200, res.text
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "王小明"

    def test_admin_200(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            _setup_search_student(s)
            _create_user(s, username="adm_search", role="admin", permissions=-1)
            s.commit()

        _login(client, "adm_search")
        res = client.get("/api/activity/students/search?q=王")
        assert res.status_code == 200, res.text


# ─────────────────────────────────────────────────────────────────────────
# F-028：activity/pos outstanding-by-student
# ─────────────────────────────────────────────────────────────────────────


class TestF028_POSOutstanding:
    """pos/outstanding-by-student：缺 STUDENTS_READ 遮罩 birthday；保留 student_name + class_name。"""

    def test_caller_without_students_read_sees_birthday_masked(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            reg = _setup_activity_reg(s)
            _create_user(
                s,
                username="pos_no_st",
                role="pos_clerk",
                permissions=int(Permission.ACTIVITY_READ),
            )
            s.commit()

        _login(client, "pos_no_st")
        res = client.get("/api/activity/pos/outstanding-by-student")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["groups"], data
        for g in data["groups"]:
            assert g["birthday"] in (None, ""), f"birthday 應遮罩，實際 {g['birthday']}"
            # student_name / class_name 為 POS 必要欄位，必須保留
            assert g["student_name"] == "王小明"
            assert g["class_name"] == "大班A"

    def test_admin_sees_birthday(self, pii_client):
        client, sf = pii_client
        with sf() as s:
            _setup_activity_reg(s)
            _create_user(s, username="adm_pos", role="admin", permissions=-1)
            s.commit()

        _login(client, "adm_pos")
        res = client.get("/api/activity/pos/outstanding-by-student")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["groups"], data
        # admin 應看到真實生日
        assert any(g["birthday"] == "2020-01-01" for g in data["groups"])
