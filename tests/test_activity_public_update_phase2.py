"""tests/test_activity_public_update_phase2.py

Phase 2 強化（2026-05-01）：
- /public/query 回 updated_at 樂觀鎖 token
- /public/update 接 if_unmodified_since（不符 → 409 STALE）
- /public/update 直接回傳完整 registration（含 field_state、courses、supplies、updated_at）
- /public/update 寫 RegistrationChange 業務軌跡（與管理端同層）
- /public/update audit_changes diff 不洩漏家長手機號全文
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
    ActivitySupply,
    Base,
    Classroom,
    RegistrationChange,
    Student,
)


@pytest.fixture
def phase2_client(tmp_path):
    db_path = tmp_path / "phase2.sqlite"
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
    # 清 query limiter 避免跨檔測試殘留 timestamps 把後續測試打到 429
    _public_query_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    # 跨檔 teardown 也清乾淨，避免本檔的 query/register 呼叫殘留 timestamp
    # 把後續測試（特別是同樣會對公開端點打 query 的）打到 429。
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed(session, *, with_student=True):
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
        ActivityCourse(
            name="畫畫", price=800, school_year=sy, semester=sem, is_active=True
        )
    )
    session.add(
        ActivitySupply(
            name="畫具", price=200, school_year=sy, semester=sem, is_active=True
        )
    )
    if with_student:
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
    session.commit()
    return classroom.id


def _register(client, *, name="王小明", phone="0912345678", class_name="海豚班"):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": name,
            "birthday": "2020-05-10",
            "parent_phone": phone,
            "class": class_name,
            "courses": [{"name": "圍棋", "price": "1000"}],
            "supplies": [],
        },
    )


def _query(client, *, name="王小明", phone="0912345678"):
    return client.get(
        "/api/activity/public/query",
        params={"name": name, "birthday": "2020-05-10", "parent_phone": phone},
    )


class TestOptimisticLockToken:
    def test_query_returns_updated_at_token(self, phase2_client):
        client, sf = phase2_client
        with sf() as s:
            _seed(s)
        r1 = _register(client)
        assert r1.status_code == 201

        res = _query(client)
        assert res.status_code == 200
        body = res.json()
        assert "updated_at" in body
        assert isinstance(body["updated_at"], str)
        assert len(body["updated_at"]) > 10  # ISO 字串至少含日期

    def test_update_with_matching_token_succeeds(self, phase2_client):
        client, sf = phase2_client
        with sf() as s:
            _seed(s)
        _register(client)
        q = _query(client).json()

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": q["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q["class_name"],
                "courses": [
                    {"name": "圍棋", "price": "1000"},
                    {"name": "畫畫", "price": "800"},
                ],
                "supplies": [{"name": "畫具", "price": "200"}],
                "if_unmodified_since": q["updated_at"],
            },
        )
        assert res.status_code == 200, res.text

    def test_update_with_stale_token_returns_409(self, phase2_client):
        client, sf = phase2_client
        with sf() as s:
            _seed(s)
        _register(client)
        q1 = _query(client).json()

        # 先用第一份 token 改一次（成功會 bump updated_at）
        client.post(
            "/api/activity/public/update",
            json={
                "id": q1["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q1["class_name"],
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [{"name": "畫具", "price": "200"}],
                "if_unmodified_since": q1["updated_at"],
            },
        )

        # 再用同一份舊 token 嘗試覆寫 → 應 409 STALE
        res = client.post(
            "/api/activity/public/update",
            json={
                "id": q1["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q1["class_name"],
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [],
                "if_unmodified_since": q1["updated_at"],
            },
        )
        assert res.status_code == 409
        assert "資料已被校方更新" in res.json()["detail"]

    def test_update_without_token_still_succeeds(self, phase2_client):
        """向後相容：if_unmodified_since 是選填，未帶仍可儲存。"""
        client, sf = phase2_client
        with sf() as s:
            _seed(s)
        _register(client)
        q = _query(client).json()

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": q["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q["class_name"],
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [],
            },
        )
        assert res.status_code == 200


class TestUpdateReturnsFullPayload:
    def test_update_response_contains_full_registration(self, phase2_client):
        client, sf = phase2_client
        with sf() as s:
            _seed(s)
        _register(client)
        q = _query(client).json()

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": q["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q["class_name"],
                "courses": [
                    {"name": "圍棋", "price": "1000"},
                    {"name": "畫畫", "price": "800"},
                ],
                "supplies": [{"name": "畫具", "price": "200"}],
                "if_unmodified_since": q["updated_at"],
            },
        )
        assert res.status_code == 200
        body = res.json()

        # 與 /public/query 同 schema
        for key in (
            "id",
            "name",
            "birthday",
            "class_name",
            "courses",
            "supplies",
            "field_state",
            "updated_at",
            "total_amount",
            "paid_amount",
            "message",
        ):
            assert key in body, f"update response missing {key}"

        # courses 應已套用新選擇
        course_names = {c["name"] for c in body["courses"]}
        assert course_names == {"圍棋", "畫畫"}

        # updated_at 應推進（與舊 token 不同）
        assert body["updated_at"] != q["updated_at"]

    def test_update_response_does_not_leak_match_internals(self, phase2_client):
        client, sf = phase2_client
        with sf() as s:
            _seed(s)
        _register(client)
        q = _query(client).json()

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": q["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q["class_name"],
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [],
            },
        )
        body = res.json()
        for forbidden in (
            "match_status",
            "pending_review",
            "student_id",
            "classroom_id",
        ):
            assert forbidden not in body


class TestRegistrationChangeBusinessTrail:
    def test_update_writes_registration_change_log(self, phase2_client):
        client, sf = phase2_client
        with sf() as s:
            _seed(s)
        _register(client)
        q = _query(client).json()

        client.post(
            "/api/activity/public/update",
            json={
                "id": q["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q["class_name"],
                "courses": [
                    {"name": "圍棋", "price": "1000"},
                    {"name": "畫畫", "price": "800"},
                ],
                "supplies": [],
            },
        )

        with sf() as s:
            entries = (
                s.query(RegistrationChange)
                .filter(RegistrationChange.registration_id == q["id"])
                .order_by(RegistrationChange.id.desc())
                .all()
            )
        assert any(
            e.change_type == "家長公開頁修改" and e.changed_by == "家長（公開頁）"
            for e in entries
        )

    def test_no_change_no_business_log(self, phase2_client):
        """若儲存時無實際欄位變動，不應寫業務軌跡。"""
        client, sf = phase2_client
        with sf() as s:
            _seed(s)
        _register(client)
        q = _query(client).json()

        # 用相同內容再儲存一次
        client.post(
            "/api/activity/public/update",
            json={
                "id": q["id"],
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": q["class_name"],
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [],
            },
        )

        with sf() as s:
            count = (
                s.query(RegistrationChange)
                .filter(
                    RegistrationChange.registration_id == q["id"],
                    RegistrationChange.change_type == "家長公開頁修改",
                )
                .count()
            )
        assert count == 0
