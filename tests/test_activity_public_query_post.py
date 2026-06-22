"""tests/test_activity_public_query_post.py

A3：/public/query 從 GET（query params）改為 POST（JSON body）。

audit 發現原 GET 把姓名+生日+家長手機放進 URL query string → 進 access log /
瀏覽器歷史 / Referer，與同檔 query-by-token（已用 POST，docstring 明說要避免
進 log）自相矛盾。

測試要點：
- 舊 GET /api/activity/public/query（帶 query params）回 405（方法不允許）
- 新 POST /api/activity/public/query（body 帶 name/birthday/parent_phone）回 200 且資料正確
- 三欄不符仍回 404（隱私契約不變）
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
from api.activity.public import (
    _public_query_limiter_instance,
    _public_register_limiter_instance,
)
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    Student,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def query_post_client(tmp_path):
    db_path = tmp_path / "query_post.sqlite"
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
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed(session):
    sy, sem = _term()
    classroom = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
    session.add(classroom)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋", price=1000, school_year=sy, semester=sem, is_active=True
        )
    )
    session.add(
        Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 5, 10),
            classroom_id=classroom.id,
            parent_phone="0912345678",
            is_active=True,
        )
    )
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )
    session.commit()


def _register(client):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "0912345678",
            "class": "海豚班",
            "courses": [{"name": "圍棋", "price": "1000"}],
            "supplies": [],
        },
    )


class TestPublicQueryMethodChanged:
    def test_old_get_returns_405(self, query_post_client):
        """舊 GET /public/query（帶 query params）必須回 405 Method Not Allowed。"""
        client, sf = query_post_client
        with sf() as s:
            _seed(s)
        _register(client)

        res = client.get(
            "/api/activity/public/query",
            params={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert (
            res.status_code == 405
        ), f"GET /public/query 應回 405，實際得到 {res.status_code}"

    def test_new_post_with_correct_data_returns_200(self, query_post_client):
        """新 POST /public/query（body 帶三欄）三欄正確 → 200 + 資料正確。"""
        client, sf = query_post_client
        with sf() as s:
            _seed(s)
        _register(client)

        res = client.post(
            "/api/activity/public/query",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert (
            res.status_code == 200
        ), f"POST /public/query 應回 200，實際得到 {res.status_code}: {res.text}"
        body = res.json()
        # 必有核心欄位
        for key in ("id", "name", "birthday", "courses", "field_state", "updated_at"):
            assert key in body, f"回應缺少欄位: {key}"
        assert body["name"] == "王小明"

    def test_new_post_wrong_name_returns_404(self, query_post_client):
        """三欄任一不符 → 404（隱私契約不變）。"""
        client, sf = query_post_client
        with sf() as s:
            _seed(s)
        _register(client)

        res = client.post(
            "/api/activity/public/query",
            json={
                "name": "張三",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert res.status_code == 404

    def test_new_post_wrong_phone_returns_404(self, query_post_client):
        """家長電話不符 → 404。"""
        client, sf = query_post_client
        with sf() as s:
            _seed(s)
        _register(client)

        res = client.post(
            "/api/activity/public/query",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0900000000",
            },
        )
        assert res.status_code == 404

    def test_new_post_missing_field_returns_422(self, query_post_client):
        """body 缺必要欄位 → 422 Unprocessable Entity。"""
        client, sf = query_post_client
        with sf() as s:
            _seed(s)

        res = client.post(
            "/api/activity/public/query",
            json={"name": "王小明", "birthday": "2020-05-10"},
        )
        assert res.status_code == 422


class TestPublicQueryTermDeterministic:
    """#5：多筆跨學期 active 報名時，三欄查詢應 deterministic 取最新學期那筆。

    同一學生同家長手機可在不同學期各有一筆 active 報名（partial unique index
    per-term 允許）。原查詢無 order_by → 任意取 DB 預設順序第一筆（通常最舊）。
    """

    def test_multiple_active_terms_returns_newest(self, query_post_client):
        client, sf = query_post_client
        from utils.academic import resolve_current_academic_term

        sy, sem = resolve_current_academic_term()
        with sf() as s:
            # 舊學期先建（id 較小）
            old = ActivityRegistration(
                student_name="陳小華",
                birthday="2019-03-03",
                parent_phone="0987654321",
                is_active=True,
                school_year=sy - 1,
                semester=sem,
                match_status="matched",
                paid_amount=0,
            )
            s.add(old)
            s.flush()
            new = ActivityRegistration(
                student_name="陳小華",
                birthday="2019-03-03",
                parent_phone="0987654321",
                is_active=True,
                school_year=sy,
                semester=sem,
                match_status="matched",
                paid_amount=0,
            )
            s.add(new)
            s.flush()
            s.commit()
            old_id, new_id = old.id, new.id
        assert old_id < new_id  # 前置：舊學期 id 較小（驗證修前會取到它）

        res = client.post(
            "/api/activity/public/query",
            json={
                "name": "陳小華",
                "birthday": "2019-03-03",
                "parent_phone": "0987654321",
            },
        )
        assert res.status_code == 200, res.text
        assert (
            res.json()["id"] == new_id
        ), "多筆跨學期 active 報名應 deterministic 取最新學期（修前依 DB 預設順序取最舊）"
