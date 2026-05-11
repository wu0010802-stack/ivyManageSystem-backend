"""tests/test_gov_moe_iep.py — IEP 個別化教育計畫 endpoint tests (Phase 4A)."""

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
from api.gov_moe import router as gov_moe_router
from models.base import Base
from models.database import Student, User
from models.classroom import Classroom  # noqa: F401 — registers classrooms table
from models.gov_moe import StudentIEPRecord  # noqa: F401 — registers iep table
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures (copied verbatim from test_gov_moe_disability_documents.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def gov_moe_client(tmp_path):
    db_path = tmp_path / "gov_moe_iep.sqlite"
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
    app.include_router(gov_moe_router, prefix="/api")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ---------------------------------------------------------------------------
# Auth helper (copied verbatim from Sub-system C/B tests)
# ---------------------------------------------------------------------------


def _login_admin(client, session_factory):
    with session_factory() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("AdminPass1"),
                role="admin",
                permissions=-1,
                is_active=True,
            )
        )
        s.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "AdminPass1"}
    )
    return resp.json().get("access_token") or resp.cookies.get("access_token")


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_student_and_classroom(sf, classroom_id=None):
    from models.classroom import Student, Classroom

    with sf() as s:
        cls_id = classroom_id
        if not cls_id:
            c = Classroom(name="向日葵班")
            s.add(c)
            s.commit()
            s.refresh(c)
            cls_id = c.id
        st = Student(
            name="王小明",
            student_id="S0001",
            is_active=True,
            classroom_id=cls_id,
            disability_type="自閉症",
            disability_level="輕度",
        )
        s.add(st)
        s.commit()
        s.refresh(st)
        return st.id, cls_id


# ---------------------------------------------------------------------------
# A1 Tests
# ---------------------------------------------------------------------------


def test_iep_list_empty(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/gov-moe/iep",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json() == []


def test_iep_create_starts_in_draft(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    sid, _ = _seed_student_and_classroom(sf)
    r = client.post(
        "/api/gov-moe/iep",
        json={
            "student_id": sid,
            "school_year": 2026,
            "semester": 1,
            "current_status": "認知尚可，需語言治療支持",
            "long_term_goals": "提升口語溝通",
            "short_term_goals": [
                {
                    "goal": "10 詞彙",
                    "criteria": "8/10 命名正確",
                    "due_date": "2026-12-01",
                    "status": "active",
                }
            ],
            "iep_team_members": [{"role": "班導", "name": "陳老師"}],
            "meeting_dates": {"initial": "2026-09-15"},
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "draft"
    assert body["school_year"] == 2026 and body["semester"] == 1


def test_iep_duplicate_year_semester_returns_409(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    sid, _ = _seed_student_and_classroom(sf)
    payload = {"student_id": sid, "school_year": 2026, "semester": 1}
    auth = {"Authorization": f"Bearer {tok}"}
    client.post("/api/gov-moe/iep", json=payload, headers=auth)
    r = client.post("/api/gov-moe/iep", json=payload, headers=auth)
    assert r.status_code == 409


def test_iep_scope_班導_only_sees_own_classroom(gov_moe_client):
    """班導 只能看到自己班級學生的 IEP，不能看到其他班級的 IEP。"""
    from models.classroom import Classroom, Student
    from models.employee import Employee

    client, sf = gov_moe_client
    admin_tok = _login_admin(client, sf)

    # 建立兩個班級與各自的學生
    with sf() as s:
        cls_a = Classroom(name="A 班")
        cls_b = Classroom(name="B 班")
        s.add_all([cls_a, cls_b])
        s.commit()
        s.refresh(cls_a)
        s.refresh(cls_b)

        st_a = Student(
            name="學生 A",
            student_id="A001",
            is_active=True,
            classroom_id=cls_a.id,
            disability_type="自閉症",
        )
        st_b = Student(
            name="學生 B",
            student_id="B001",
            is_active=True,
            classroom_id=cls_b.id,
            disability_type="自閉症",
        )
        s.add_all([st_a, st_b])
        s.commit()
        s.refresh(st_a)
        s.refresh(st_b)

        # 建立 A 班的班導員工與對應帳號
        emp = Employee(
            name="陳老師",
            employee_id="T001",
            is_active=True,
            classroom_id=cls_a.id,
            supervisor_role=None,
        )
        s.add(emp)
        s.commit()
        s.refresh(emp)

        teacher_user = User(
            username="t1",
            password_hash=hash_password("Teach123"),
            role="teacher",
            permissions=(1 << 48),  # STUDENTS_SPECIAL_NEEDS_WRITE
            is_active=True,
            employee_id=emp.id,
        )
        s.add(teacher_user)
        s.commit()

        sid_a = st_a.id
        sid_b = st_b.id

    # admin 為兩個學生各建一筆 IEP
    auth_admin = {"Authorization": f"Bearer {admin_tok}"}
    for sid in (sid_a, sid_b):
        r = client.post(
            "/api/gov-moe/iep",
            json={"student_id": sid, "school_year": 2026, "semester": 1},
            headers=auth_admin,
        )
        assert r.status_code == 201, r.text

    # 班導登入
    resp = client.post(
        "/api/auth/login", json={"username": "t1", "password": "Teach123"}
    )
    assert resp.status_code == 200, resp.text
    teacher_tok = resp.json().get("access_token") or resp.cookies.get("access_token")

    # 班導查詢 IEP 列表：只應看到 A 班學生的 IEP
    r = client.get(
        "/api/gov-moe/iep",
        headers={"Authorization": f"Bearer {teacher_tok}"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1, f"期望 1 筆，實際 {len(rows)} 筆：{rows}"
    assert rows[0]["student_id"] == sid_a


# ---------------------------------------------------------------------------
# A2 Tests: clone semantics + state transitions
# ---------------------------------------------------------------------------


def test_iep_clone_preserves_goals_clears_evaluations(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    sid, _ = _seed_student_and_classroom(sf)
    auth = {"Authorization": f"Bearer {tok}"}
    src = client.post(
        "/api/gov-moe/iep",
        json={
            "student_id": sid,
            "school_year": 2026,
            "semester": 1,
            "current_status": "認知尚可",
            "long_term_goals": "提升口語",
            "short_term_goals": [{"goal": "10 詞彙"}],
            "iep_team_members": [{"role": "班導", "name": "陳老師"}],
            "mid_term_evaluation": "已達成 5 詞",
            "final_evaluation": "達 9 詞",
            "meeting_dates": {"initial": "2026-09-15"},
        },
        headers=auth,
    ).json()
    r = client.post(
        f"/api/gov-moe/iep/{src['id']}/clone",
        json={"target_school_year": 2026, "target_semester": 2},
        headers=auth,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "draft"
    assert body["current_status"] == "認知尚可"
    assert body["long_term_goals"] == "提升口語"
    assert body["short_term_goals"] == [{"goal": "10 詞彙"}]
    assert body["iep_team_members"] == [{"role": "班導", "name": "陳老師"}]
    assert body["mid_term_evaluation"] is None
    assert body["final_evaluation"] is None
    assert body["meeting_dates"] is None


def test_iep_clone_target_existing_returns_409(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    sid, _ = _seed_student_and_classroom(sf)
    auth = {"Authorization": f"Bearer {tok}"}
    s1 = client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid, "school_year": 2026, "semester": 1},
        headers=auth,
    ).json()
    client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid, "school_year": 2026, "semester": 2},
        headers=auth,
    )
    r = client.post(
        f"/api/gov-moe/iep/{s1['id']}/clone",
        json={"target_school_year": 2026, "target_semester": 2},
        headers=auth,
    )
    assert r.status_code == 409


def test_iep_state_transitions(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    sid, _ = _seed_student_and_classroom(sf)
    auth = {"Authorization": f"Bearer {tok}"}
    iep = client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid, "school_year": 2026, "semester": 1},
        headers=auth,
    ).json()
    r = client.put(f"/api/gov-moe/iep/{iep['id']}/submit", headers=auth)
    assert r.json()["status"] == "pending_review"
    r = client.put(f"/api/gov-moe/iep/{iep['id']}/approve", headers=auth)
    assert r.json()["status"] == "approved"
    r = client.put(f"/api/gov-moe/iep/{iep['id']}/close", headers=auth)
    assert r.json()["status"] == "closed"


def test_iep_cannot_edit_after_approved(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    sid, _ = _seed_student_and_classroom(sf)
    auth = {"Authorization": f"Bearer {tok}"}
    iep = client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid, "school_year": 2026, "semester": 1},
        headers=auth,
    ).json()
    client.put(f"/api/gov-moe/iep/{iep['id']}/submit", headers=auth)
    client.put(f"/api/gov-moe/iep/{iep['id']}/approve", headers=auth)
    r = client.put(
        f"/api/gov-moe/iep/{iep['id']}", json={"current_status": "edited"}, headers=auth
    )
    assert r.status_code == 409
