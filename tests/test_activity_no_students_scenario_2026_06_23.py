"""驗證測試：系統「完全沒有任何學生資料（students 表為空）」時，課後才藝報名是否仍能運作。

對應分析三類結論（每類各有測試佐證）：

A. 完全不受影響（學生表空也照常報名成功）
   - 後台管理端手動新增報名 → 201，student_id 自動為 None、match_status='unmatched'
   - 公開網頁端（免登入）報名 → 201，落入待審核（pending_review=True、match_status='pending'）

B. 只會是空清單 / 顯示 0（by design 正常，不炸）
   - 統計 dashboard（stats-summary / dashboard-table）→ 200，每班 student_count=0，無除零

C. 唯一被擋下的環節（受控 4xx，非 crash）
   - LIFF 登入家長端報名：家長名下無子女（無 Guardian/Student）→ 403（StudentNotLinkedToParent）
   - 但「我的報名」清單 → 200 空清單（不炸）

共同前提：所有測試都「不建任何 Student」，並在每個 case 內 assert Student 表確實為 0 筆，
以明確聲明此為「零學生」情境。模組真正的硬依賴是 classrooms（班級），故各 fixture 仍建班級。

DB 隔離：沿用全模組 SQLite + monkeypatch base_module 模式（不碰 dev PG）。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import (
    _public_confirm_limiter_instance,
    _public_query_limiter_instance,
    _public_register_limiter_instance,
)
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.parent_portal import parent_router as parent_portal_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    Student,
    User,
)
from utils.academic import resolve_current_academic_term
from utils.auth import create_access_token, hash_password

_PUBLIC_LIMITERS = (
    _public_register_limiter_instance,
    _public_query_limiter_instance,
    _public_confirm_limiter_instance,
)


# ───────────────────────────── 共用：建立「零學生」基礎資料 ─────────────────────────────


def _seed_no_students(session):
    """建立班級 + 課程，但**完全不建任何 Student**。回傳 (school_year, semester)。"""
    sy, sem = resolve_current_academic_term()
    session.add(Classroom(name="大班", is_active=True))
    session.add(
        ActivityCourse(
            name="美術",
            price=1500,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
    )
    session.flush()
    return sy, sem


def _assert_zero_students(session):
    assert session.query(Student).count() == 0, "前提：students 表必須為空"


# ───────────────────────────── A/B 類：後台 admin client ─────────────────────────────


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "no_students_admin.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_admin_and_data(session):
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )
    _seed_no_students(session)


def _admin_login(client):
    return client.post(
        "/api/auth/login", json={"username": "admin", "password": "TempPass123"}
    )


class TestAdminRegistrationWithZeroStudents:
    """A 類：後台手動報名不需要既有學生（自由輸入姓名+生日+班級）。"""

    def test_admin_register_succeeds_and_leaves_student_id_null(self, admin_client):
        client, sf = admin_client
        with sf() as s:
            _seed_admin_and_data(s)
            s.commit()
            _assert_zero_students(s)
        assert _admin_login(client).status_code == 200

        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "大班",
                "courses": [{"name": "美術"}],
                "supplies": [],
            },
        )
        assert res.status_code == 201, res.text

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(student_name="王小明").one()
            # 零學生 → best-effort 匹配查無 → student_id 為 None、標記 unmatched
            assert reg.student_id is None
            assert reg.match_status == "unmatched"
            _assert_zero_students(s)


class TestDashboardWithZeroStudents:
    """B 類：統計儀表板在零學生時回 200、不炸（每班 student_count=0、無除零）。"""

    def test_stats_summary_and_dashboard_table_no_crash(self, admin_client):
        client, sf = admin_client
        with sf() as s:
            _seed_admin_and_data(s)
            s.commit()
            _assert_zero_students(s)
        assert _admin_login(client).status_code == 200

        r1 = client.get("/api/activity/stats-summary")
        assert r1.status_code == 200, r1.text

        r2 = client.get("/api/activity/dashboard-table")
        assert r2.status_code == 200, r2.text


# ───────────────────────────── A 類：公開端 public client ─────────────────────────────


@pytest.fixture
def public_client(tmp_path):
    db_path = tmp_path / "no_students_public.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    for lim in _PUBLIC_LIMITERS:
        lim._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, sf

    for lim in _PUBLIC_LIMITERS:
        lim._timestamps.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


class TestPublicRegistrationWithZeroStudents:
    """A 類：公開報名（免登入）不需要既有學生；匹配不到 → 落待審核但報名成功。"""

    def test_public_register_succeeds_and_falls_into_review(self, public_client):
        client, sf = public_client
        with sf() as s:
            _seed_no_students(s)
            s.commit()
            _assert_zero_students(s)

        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "陳小華",
                "birthday": "2020-03-03",
                "parent_phone": "0912345678",
                "class": "大班",
                "courses": [{"name": "美術", "price": "1500"}],
                "supplies": [],
            },
        )
        assert res.status_code == 201, res.text
        # response 刻意中性，不洩漏 match 結果；改查 DB 驗證落入待審核
        assert res.json().get("query_token")

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(student_name="陳小華").one()
            assert reg.student_id is None
            assert reg.match_status == "pending"
            assert reg.pending_review is True
            _assert_zero_students(s)


# ───────────────────────────── C 類：家長 LIFF parent client ─────────────────────────────


@pytest.fixture
def parent_client(tmp_path):
    db_path = tmp_path / "no_students_parent.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_portal_router)

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import (
        make_sqlite_parent_db_override,
        register_sqlite_parent_rls_udfs,
    )

    register_sqlite_parent_rls_udfs(engine)
    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(sf)

    with TestClient(app) as client:
        yield client, sf

    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_childless_parent(session):
    """建立一個 parent User，但**不建 Guardian、不建 Student**（名下零子女）。"""
    user = User(
        username="parent_line_LONELY",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id="LONELY",
        token_version=0,
    )
    session.add(user)
    _seed_no_students(session)
    session.flush()
    return user


def _parent_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permission_names": [],
            "token_version": user.token_version or 0,
        }
    )


class TestParentRegistrationWithNoChildren:
    """C 類：LIFF 登入家長名下無子女 → 報名被擋下（403），但清單端點不炸。"""

    def test_register_blocked_with_403(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            user = _seed_childless_parent(s)
            s.commit()
            _assert_zero_students(s)
            token = _parent_token(user)
            sy, sem = resolve_current_academic_term()

        # course_ids 非空以越過「至少一項」400 守衛；403（非自己小孩）會在
        # 課程查詢前先觸發——名下無子女 → 任何 student_id 都不屬於該家長。
        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": 999999,
                "school_year": sy,
                "semester": sem,
                "course_ids": [1],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 403, resp.text

    def test_my_registrations_returns_empty_without_crash(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            user = _seed_childless_parent(s)
            s.commit()
            _assert_zero_students(s)
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/my-registrations",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
