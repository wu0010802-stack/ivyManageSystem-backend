"""越權修補回歸測試（2026-06-04 授權滲透測試發現）。

對應 .scratch/pentest-authz-2026-06-04.md：
- #1 CRITICAL：教師 token 越權撞考核管理端 router（應 require_staff_permission）
- #2 HIGH：permission_names=NULL 的教師經 in-code ROLE_TEMPLATES 漂移被提權成全園 scope
- #3 MEDIUM：未分班（classroom_id=NULL）學生的 incident 寫入繞過班級檢查
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.base as base_module
from models.database import Base, Student
from utils.auth import get_current_user
from utils.permissions import (
    resolve_grant,
    resolve_user_permissions,
)

# router import 置於 module 頂層，確保其 model 註冊進 Base.metadata
from api.appraisal import appraisal_router
from api.student_incidents import router as incidents_router


class _FakeUser:
    """單元測試用的 User stand-in。"""

    def __init__(self, role, permission_names):
        self.role = role
        self.permission_names = permission_names


# 教師模板中屬 class-scoped 的權限：必須帶 :own_class，否則 NULL-perm 教師被提權成全園。
_TEACHER_SCOPED_CODES = [
    "PORTFOLIO_READ",
    "PORTFOLIO_WRITE",
    "STUDENTS_HEALTH_READ",
    "STUDENTS_MEDICATION_ADMINISTER",
    "STUDENTS_SPECIAL_NEEDS_READ",
    "DISMISSAL_CALLS_READ",
    "DISMISSAL_CALLS_WRITE",
]


# ───────────────────────── #2 解析層（純單元）─────────────────────────
def test_null_perm_teacher_scope_aware_codes_are_own_class():
    """permission_names=None 的教師，scope-aware code 解析後必為 :own_class（非 bare=all）。"""
    perms = resolve_user_permissions(_FakeUser("teacher", None))
    for bare in _TEACHER_SCOPED_CODES:
        assert bare not in perms, f"教師模板含 bare {bare}（= 全園 scope，提權）"
        assert f"{bare}:own_class" in perms, f"教師模板缺 {bare}:own_class"


def test_null_perm_teacher_resolve_grant_scope_is_own_class():
    """end-to-end：NULL-perm 教師對 STUDENTS_HEALTH_READ 的 grant scope 必為 own_class。"""
    perms = resolve_user_permissions(_FakeUser("teacher", None))
    grant = resolve_grant({"permission_names": perms}, "STUDENTS_HEALTH_READ")
    assert grant is not None
    assert (
        grant.scope == "own_class"
    ), f"NULL-perm 教師被解析成 {grant.scope} scope（應 own_class）"


# ───────────────────────── 端點層 fixture ─────────────────────────
@pytest.fixture
def app_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'authz.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(appraisal_router)
    app.include_router(incidents_router)

    with TestClient(app) as client:
        yield client, session_factory, app

    app.dependency_overrides.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_as(app, user: dict):
    app.dependency_overrides[get_current_user] = lambda: user


_TEACHER_APPRAISAL = {
    "role": "teacher",
    "permission_names": ["APPRAISAL_READ"],
    "user_id": 1,
    "employee_id": 61,
    "username": "teacher",
}
_TEACHER_STUDENTS_WRITE = {
    "role": "teacher",
    "permission_names": ["STUDENTS_WRITE:own_class"],
    "user_id": 1,
    "employee_id": 999,  # 不擔任任何班導 → 無 own_class
    "username": "teacher",
}


# ───────────────────────── #1 考核管理端 ─────────────────────────
def test_teacher_blocked_from_management_appraisal_summaries(app_client):
    """教師持 APPRAISAL_READ 不得撞管理端考核 summaries（全員考核分數）。"""
    client, _sf, app = app_client
    _login_as(app, _TEACHER_APPRAISAL)
    resp = client.get("/api/appraisal/cycles/1/summaries")
    assert resp.status_code == 403, f"教師應被擋出考核管理端，實得 {resp.status_code}"


# ───────────────────────── #3 未分班學生寫入 ─────────────────────────
def test_teacher_cannot_write_incident_for_unassigned_student(app_client):
    """持 STUDENTS_WRITE:own_class 的教師，不得對未分班（classroom_id=NULL）學生寫 incident。"""
    client, session_factory, app = app_client
    s = session_factory()
    stu = Student(student_id="S999", name="未分班測試生", classroom_id=None)
    s.add(stu)
    s.commit()
    sid = stu.id
    s.close()

    _login_as(app, _TEACHER_STUDENTS_WRITE)
    resp = client.post(
        "/api/student-incidents",
        json={
            "student_id": sid,
            "incident_type": "行為觀察",
            "severity": "輕微",
            "occurred_at": "2026-06-04T09:00:00",
            "description": "regression",
        },
    )
    assert (
        resp.status_code == 403
    ), f"教師不應能寫未分班學生 incident，實得 {resp.status_code}"


# ───────────────────────── #4 calendar admin_feed 考核 layer ─────────────────────────
def test_teacher_cannot_see_appraisal_layer_in_calendar_feed(tmp_path):
    """教師持 APPRAISAL_READ 不得在 calendar admin_feed 看到考核 cycle metadata。"""
    from datetime import date

    from api.calendar_admin import _fetch_appraisal
    from models.appraisal import AppraisalCycle, Semester

    engine = create_engine(
        f"sqlite:///{tmp_path / 'cal.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    s = session_factory()
    s.add(
        AppraisalCycle(
            academic_year=114,
            semester=Semester.FIRST,
            start_date=date(2026, 1, 15),
            end_date=date(2026, 1, 20),
            base_score_calc_date=date(2026, 1, 18),
        )
    )
    s.commit()

    teacher = {"role": "teacher", "permission_names": ["APPRAISAL_READ", "CALENDAR"]}
    items = _fetch_appraisal(s, date(2026, 1, 1), date(2026, 1, 31), teacher)
    s.close()
    engine.dispose()
    assert items == [], f"教師不應看到考核 layer metadata，實得 {len(items)} 筆"
