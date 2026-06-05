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
                permission_names=["*"],
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

        # portfolio_access 走 Classroom.head_teacher_id 三角 OR（非 Employee.classroom_id）
        cls_a.head_teacher_id = emp.id
        s.add(cls_a)
        s.commit()

        teacher_user = User(
            username="t1",
            password_hash=hash_password("Teach123"),
            role="teacher",
            # Phase 2.2 起 teacher perm 用 :own_class scope（permscope03 backfill 同步）；
            # bare 'STUDENTS_SPECIAL_NEEDS_WRITE' 在 resolve_grant 等同 :all 會繞過 scoping
            permission_names=["STUDENTS_SPECIAL_NEEDS_WRITE:own_class"],
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


def test_iep_create_rejects_cross_classroom_student(gov_moe_client):
    """班導 不可為其他班學生建立 IEP（IDOR via body.student_id）。

    P1-5 修補目標：原本 create_iep 直接 **payload.model_dump() 入庫，未驗證
    student_id 是否在 caller scope 內；持 STUDENTS_SPECIAL_NEEDS_WRITE 的班導
    可為跨班學生建檔污染唯一鍵 / 業務紀錄。
    """
    from models.classroom import Classroom, Student
    from models.employee import Employee

    client, sf = gov_moe_client

    with sf() as s:
        cls_a = Classroom(name="A 班")
        cls_b = Classroom(name="B 班")
        s.add_all([cls_a, cls_b])
        s.commit()
        s.refresh(cls_a)
        s.refresh(cls_b)

        st_b = Student(
            name="B 班學生",
            student_id="B900",
            is_active=True,
            classroom_id=cls_b.id,
            disability_type="自閉症",
        )
        s.add(st_b)
        s.commit()
        s.refresh(st_b)

        emp = Employee(
            name="A 班班導",
            employee_id="T900",
            is_active=True,
            classroom_id=cls_a.id,
            supervisor_role=None,
        )
        s.add(emp)
        s.commit()
        s.refresh(emp)

        # portfolio_access 走 Classroom.head_teacher_id 三角 OR
        cls_a.head_teacher_id = emp.id
        s.add(cls_a)
        s.commit()

        s.add(
            User(
                username="teacher_a",
                password_hash=hash_password("Teach123"),
                role="teacher",
                # Phase 2.2 起 teacher perm 用 :own_class scope（bare 等同 :all 繞過 scoping）
                permission_names=["STUDENTS_SPECIAL_NEEDS_WRITE:own_class"],
                is_active=True,
                employee_id=emp.id,
            )
        )
        s.commit()
        sid_b = st_b.id

    resp = client.post(
        "/api/auth/login",
        json={"username": "teacher_a", "password": "Teach123"},
    )
    assert resp.status_code == 200
    tok = resp.json().get("access_token") or resp.cookies.get("access_token")

    # A 班班導試圖為 B 班學生建 IEP → 必須 403
    r = client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid_b, "school_year": 2026, "semester": 1},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 403, r.text
    # Task 7 起 IEP scope 改 delegate 至 portfolio_access，403 detail 統一為「您無權存取此學生」
    assert "無權" in r.json()["detail"]


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


def test_iep_supervisor_can_approve_and_close(gov_moe_client):
    """園長/主任（持 STUDENTS_IEP_APPROVE permission）可批核並結案 IEP。

    批核權改走 permission 治理（require_permission）而非 supervisor_role 字串旁路；
    主任帳號透過角色 template / backfill 取得 STUDENTS_IEP_APPROVE。
    """
    from models.classroom import Classroom
    from models.employee import Employee

    client, sf = gov_moe_client
    admin_tok = _login_admin(client, sf)
    sid, cls_id = _seed_student_and_classroom(sf)

    # 建立「主任」員工 + 對應帳號（非 admin 角色，僅靠 supervisor_role 取得權限）
    with sf() as s:
        emp = Employee(
            name="主任王",
            employee_id="D001",
            is_active=True,
            classroom_id=cls_id,
            supervisor_role="主任",
        )
        s.add(emp)
        s.commit()
        s.refresh(emp)
        u = User(
            username="dir1",
            password_hash=hash_password("DirPass1"),
            role="teacher",
            permission_names=[
                "STUDENTS_SPECIAL_NEEDS_WRITE",
                "STUDENTS_IEP_APPROVE",
            ],
            is_active=True,
            employee_id=emp.id,
        )
        s.add(u)
        s.commit()

    # admin 建 IEP + submit
    auth_admin = {"Authorization": f"Bearer {admin_tok}"}
    iep = client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid, "school_year": 2026, "semester": 1},
        headers=auth_admin,
    ).json()
    client.put(f"/api/gov-moe/iep/{iep['id']}/submit", headers=auth_admin)

    # 主任登入 → approve / close 皆應成功
    resp = client.post(
        "/api/auth/login", json={"username": "dir1", "password": "DirPass1"}
    )
    dir_tok = resp.json().get("access_token") or resp.cookies.get("access_token")
    auth_dir = {"Authorization": f"Bearer {dir_tok}"}

    r = client.put(f"/api/gov-moe/iep/{iep['id']}/approve", headers=auth_dir)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    r = client.put(f"/api/gov-moe/iep/{iep['id']}/close", headers=auth_dir)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed"


def test_iep_approve_blocked_without_iep_permission(gov_moe_client):
    """旁路堵塞：持 supervisor_role='主任' 但無 STUDENTS_IEP_APPROVE 不得批核。

    避免 EMPLOYEES_WRITE 改 Employee.supervisor_role 成隱性 IEP 批核提權通道
    （繞過 ROLES_MANAGE 治理）。批核權改純由 permission 決定。
    """
    from models.employee import Employee

    client, sf = gov_moe_client
    admin_tok = _login_admin(client, sf)
    sid, cls_id = _seed_student_and_classroom(sf)

    with sf() as s:
        emp = Employee(
            name="主任李",
            employee_id="D002",
            is_active=True,
            classroom_id=cls_id,
            supervisor_role="主任",
        )
        s.add(emp)
        s.commit()
        s.refresh(emp)
        u = User(
            username="dir2",
            password_hash=hash_password("DirPass2"),
            role="teacher",
            permission_names=["STUDENTS_SPECIAL_NEEDS_WRITE"],  # 無 IEP_APPROVE
            is_active=True,
            employee_id=emp.id,
        )
        s.add(u)
        s.commit()

    auth_admin = {"Authorization": f"Bearer {admin_tok}"}
    iep = client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid, "school_year": 2026, "semester": 1},
        headers=auth_admin,
    ).json()
    client.put(f"/api/gov-moe/iep/{iep['id']}/submit", headers=auth_admin)

    resp = client.post(
        "/api/auth/login", json={"username": "dir2", "password": "DirPass2"}
    )
    dir_tok = resp.json().get("access_token") or resp.cookies.get("access_token")
    r = client.put(
        f"/api/gov-moe/iep/{iep['id']}/approve",
        headers={"Authorization": f"Bearer {dir_tok}"},
    )
    assert r.status_code == 403, f"supervisor_role 旁路應已堵塞: {r.text}"


def test_iep_班導_cannot_approve(gov_moe_client):
    """round 5 P1：純班導（無 supervisor_role）不得批核 IEP，仍 403。"""
    from models.classroom import Classroom
    from models.employee import Employee

    client, sf = gov_moe_client
    admin_tok = _login_admin(client, sf)
    sid, cls_id = _seed_student_and_classroom(sf)

    with sf() as s:
        emp = Employee(
            name="陳老師",
            employee_id="T001",
            is_active=True,
            classroom_id=cls_id,
            supervisor_role=None,
        )
        s.add(emp)
        s.commit()
        s.refresh(emp)
        u = User(
            username="t1",
            password_hash=hash_password("Teach123"),
            role="teacher",
            permission_names=["STUDENTS_SPECIAL_NEEDS_WRITE"],
            is_active=True,
            employee_id=emp.id,
        )
        s.add(u)
        s.commit()

    auth_admin = {"Authorization": f"Bearer {admin_tok}"}
    iep = client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid, "school_year": 2026, "semester": 1},
        headers=auth_admin,
    ).json()
    client.put(f"/api/gov-moe/iep/{iep['id']}/submit", headers=auth_admin)

    resp = client.post(
        "/api/auth/login", json={"username": "t1", "password": "Teach123"}
    )
    teacher_tok = resp.json().get("access_token") or resp.cookies.get("access_token")
    r = client.put(
        f"/api/gov-moe/iep/{iep['id']}/approve",
        headers={"Authorization": f"Bearer {teacher_tok}"},
    )
    assert r.status_code == 403, r.text


def test_iep_approve_scoped_role_cannot_approve_out_of_scope(gov_moe_client):
    """Finding I：持 STUDENTS_IEP_APPROVE 但 SPECIAL_NEEDS_WRITE:own_class 的自訂角色
    不得核定 scope 外的 IEP——approve/close 須走 _scoped_query（與 list/update 一致）。

    本案 approver 不擔任任何班級導師 → accessible_classroom_ids 為空 → scope=[]，
    故對任一 IEP 的 approve 都應 404（查無此通知或無權），不得 200 直接核定。
    """
    from models.employee import Employee

    client, sf = gov_moe_client
    admin_tok = _login_admin(client, sf)
    sid, _cls = _seed_student_and_classroom(sf)

    with sf() as s:
        emp = Employee(name="無班核定師", employee_id="T900", is_active=True)
        s.add(emp)
        s.commit()
        s.refresh(emp)
        u = User(
            username="scoped_appr",
            password_hash=hash_password("ScopedPass1"),
            role="teacher",
            permission_names=[
                "STUDENTS_IEP_APPROVE",
                "STUDENTS_SPECIAL_NEEDS_WRITE:own_class",
            ],
            is_active=True,
            employee_id=emp.id,
        )
        s.add(u)
        s.commit()

    auth_admin = {"Authorization": f"Bearer {admin_tok}"}
    iep = client.post(
        "/api/gov-moe/iep",
        json={"student_id": sid, "school_year": 2026, "semester": 1},
        headers=auth_admin,
    ).json()
    client.put(f"/api/gov-moe/iep/{iep['id']}/submit", headers=auth_admin)

    resp = client.post(
        "/api/auth/login", json={"username": "scoped_appr", "password": "ScopedPass1"}
    )
    tok = resp.json().get("access_token") or resp.cookies.get("access_token")
    auth = {"Authorization": f"Bearer {tok}"}

    r = client.put(f"/api/gov-moe/iep/{iep['id']}/approve", headers=auth)
    assert r.status_code == 404, f"scope 外核定應 404，得 {r.status_code}: {r.text}"


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


# ---------------------------------------------------------------------------
# A3 Tests: PDF export
# ---------------------------------------------------------------------------


def test_iep_pdf_export(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    sid, _ = _seed_student_and_classroom(sf)
    auth = {"Authorization": f"Bearer {tok}"}
    iep = client.post(
        "/api/gov-moe/iep",
        json={
            "student_id": sid,
            "school_year": 2026,
            "semester": 1,
            "current_status": "x",
            "long_term_goals": "y",
            "short_term_goals": [
                {
                    "goal": "a",
                    "criteria": "b",
                    "due_date": "2026-12-01",
                    "status": "active",
                }
            ],
        },
        headers=auth,
    ).json()
    r = client.get(f"/api/gov-moe/iep/{iep['id']}/export", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# A4 Tests: Audit pattern
# ---------------------------------------------------------------------------


def test_audit_pattern_registered_for_iep():
    from utils.audit import ENTITY_PATTERNS, ENTITY_LABELS

    assert any("iep_record" in (et or "") for _, et in ENTITY_PATTERNS)
    assert ENTITY_LABELS.get("iep_record") == "IEP 個別化教育計畫"
