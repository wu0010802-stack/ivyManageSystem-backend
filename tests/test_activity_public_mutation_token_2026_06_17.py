"""#5 資安稽核 follow-up（2026-06-17）：公開才藝破壞性 mutation 強制 query_token。

Option A（業主裁示）：
- 有 query_token_hash 的報名：/public/update、confirm-promotion、decline-promotion
  必須帶「有效未過期 query_token + phone」。PII 三欄（姓名+生日+手機）不再足夠。
- 無 token 的舊報名（query_token_hash IS NULL）：沿用三欄（向後相容，無 token 可驗）。
- 查詢 response 多帶 query_token_required，供前端在「三欄載入（無 token）」時將
  token-bearing 報名顯示為唯讀。

DB 隔離：沿用 phase2/phase3 的 SQLite + monkeypatch base_module 模式（不碰 dev PG）。
"""

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity._shared import _hash_query_token
from api.activity.public import (
    _public_confirm_limiter_instance,
    _public_query_limiter_instance,
    _public_register_limiter_instance,
)
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationCourse,
    Student,
)
from utils.taipei_time import now_taipei_naive

_LIMITERS = (
    _public_register_limiter_instance,
    _public_query_limiter_instance,
    _public_confirm_limiter_instance,
)


@pytest.fixture
def client_sf(tmp_path):
    db_path = tmp_path / "mut_token.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    for lim in _LIMITERS:
        lim._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, sf

    for lim in _LIMITERS:
        lim._timestamps.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed(session):
    sy, sem = _term()
    c = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
    session.add(c)
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
        Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 5, 10),
            classroom_id=c.id,
            parent_phone="0912345678",
            is_active=True,
        )
    )
    session.commit()
    return c.id


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


def _query3(client):
    return client.post(
        "/api/activity/public/query",
        json={"name": "王小明", "birthday": "2020-05-10", "parent_phone": "0912345678"},
    )


def _update_payload(reg_id, **over):
    p = {
        "id": reg_id,
        "name": "王小明",
        "birthday": "2020-05-10",
        "parent_phone": "0912345678",
        "class": "海豚班",
        "courses": [{"name": "圍棋", "price": "1000"}],
        "supplies": [],
    }
    p.update(over)
    return p


def _insert_reg(session, *, token_plain=None, issued_offset_days=0):
    """直接插入一筆報名（控制 token 狀態）。token_plain=None → 無 token 舊報名。"""
    sy, sem = _term()
    reg = ActivityRegistration(
        student_name="王小明",
        birthday="2020-05-10",
        class_name="海豚班",
        school_year=sy,
        semester=sem,
        parent_phone="0912345678",
        is_active=True,
        paid_amount=0,
        query_token_hash=_hash_query_token(token_plain) if token_plain else None,
        query_token_issued_at=(
            now_taipei_naive() - timedelta(days=issued_offset_days)
            if token_plain
            else None
        ),
    )
    session.add(reg)
    session.flush()
    return reg.id


def _add_promoted_pending(session, reg_id, course_id):
    session.add(
        RegistrationCourse(
            registration_id=reg_id,
            course_id=course_id,
            price_snapshot=1000,
            status="promoted_pending",
            confirm_deadline=now_taipei_naive() + timedelta(hours=24),
        )
    )
    session.commit()


# ─────────────────────────── /public/update ───────────────────────────


class TestPublicUpdateTokenEnforcement:
    def test_token_bearing_update_without_token_rejected(self, client_sf):
        client, sf = client_sf
        with sf() as s:
            _seed(s)
        _register(client)  # 真實報名 → token-bearing
        q = _query3(client).json()
        res = client.post("/api/activity/public/update", json=_update_payload(q["id"]))
        assert res.status_code == 403, res.text

    def test_token_bearing_update_with_valid_token_succeeds(self, client_sf):
        client, sf = client_sf
        with sf() as s:
            _seed(s)
        token = _register(client).json()["query_token"]
        q = _query3(client).json()
        res = client.post(
            "/api/activity/public/update",
            json=_update_payload(
                q["id"],
                query_token=token,
                courses=[
                    {"name": "圍棋", "price": "1000"},
                    {"name": "畫畫", "price": "800"},
                ],
            ),
        )
        assert res.status_code == 200, res.text

    def test_token_bearing_update_with_wrong_token_rejected(self, client_sf):
        client, sf = client_sf
        with sf() as s:
            _seed(s)
        _register(client)
        q = _query3(client).json()
        res = client.post(
            "/api/activity/public/update",
            json=_update_payload(q["id"], query_token="totally-wrong-token-xxxxxxxx"),
        )
        assert res.status_code == 403, res.text

    def test_token_bearing_update_with_expired_token_rejected(self, client_sf):
        client, sf = client_sf
        plain = "expired-known-token-abcdef123456"
        with sf() as s:
            _seed(s)
            rid = _insert_reg(s, token_plain=plain, issued_offset_days=10000)
            s.commit()
        res = client.post(
            "/api/activity/public/update",
            json=_update_payload(rid, query_token=plain),
        )
        assert res.status_code == 403, res.text

    def test_legacy_no_token_update_with_pii_succeeds(self, client_sf):
        client, sf = client_sf
        with sf() as s:
            _seed(s)
            rid = _insert_reg(s, token_plain=None)
            s.commit()
        res = client.post("/api/activity/public/update", json=_update_payload(rid))
        assert res.status_code == 200, res.text


# ──────────────────── query_token_required flag ────────────────────


class TestQueryTokenRequiredFlag:
    def test_token_bearing_query_sets_flag_true(self, client_sf):
        client, sf = client_sf
        with sf() as s:
            _seed(s)
        _register(client)
        body = _query3(client).json()
        assert body["query_token_required"] is True

    def test_legacy_query_sets_flag_false(self, client_sf):
        client, sf = client_sf
        with sf() as s:
            _seed(s)
            _insert_reg(s, token_plain=None)
            s.commit()
        body = _query3(client).json()
        assert body["query_token_required"] is False


# ──────────────── confirm / decline promotion ────────────────


class TestPromotionTokenEnforcement:
    def test_confirm_promotion_without_token_rejected(self, client_sf):
        client, sf = client_sf
        token = "promo-token-known-123456"
        with sf() as s:
            _seed(s)
            cid = s.query(ActivityCourse).filter_by(name="圍棋").first().id
            rid = _insert_reg(s, token_plain=token)
            _add_promoted_pending(s, rid, cid)
        res = client.post(
            f"/api/activity/public/registrations/{rid}/courses/{cid}/confirm-promotion",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert res.status_code == 404, res.text
        with sf() as s:
            rc = (
                s.query(RegistrationCourse)
                .filter_by(registration_id=rid, course_id=cid)
                .first()
            )
            assert rc.status == "promoted_pending"  # 未被改動

    def test_confirm_promotion_with_valid_token_succeeds(self, client_sf):
        client, sf = client_sf
        token = "promo-token-known-123456"
        with sf() as s:
            _seed(s)
            cid = s.query(ActivityCourse).filter_by(name="圍棋").first().id
            rid = _insert_reg(s, token_plain=token)
            _add_promoted_pending(s, rid, cid)
        res = client.post(
            f"/api/activity/public/registrations/{rid}/courses/{cid}/confirm-promotion",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "query_token": token,
            },
        )
        assert res.status_code == 200, res.text
        with sf() as s:
            rc = (
                s.query(RegistrationCourse)
                .filter_by(registration_id=rid, course_id=cid)
                .first()
            )
            assert rc.status == "enrolled"

    def test_decline_promotion_without_token_rejected(self, client_sf):
        client, sf = client_sf
        token = "promo-token-known-123456"
        with sf() as s:
            _seed(s)
            cid = s.query(ActivityCourse).filter_by(name="圍棋").first().id
            rid = _insert_reg(s, token_plain=token)
            _add_promoted_pending(s, rid, cid)
        res = client.post(
            f"/api/activity/public/registrations/{rid}/courses/{cid}/decline-promotion",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert res.status_code == 404, res.text
        with sf() as s:
            rc = (
                s.query(RegistrationCourse)
                .filter_by(registration_id=rid, course_id=cid)
                .first()
            )
            assert rc is not None and rc.status == "promoted_pending"

    def test_legacy_confirm_promotion_with_pii_succeeds(self, client_sf):
        client, sf = client_sf
        with sf() as s:
            _seed(s)
            cid = s.query(ActivityCourse).filter_by(name="圍棋").first().id
            rid = _insert_reg(s, token_plain=None)
            _add_promoted_pending(s, rid, cid)
        res = client.post(
            f"/api/activity/public/registrations/{rid}/courses/{cid}/confirm-promotion",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert res.status_code == 200, res.text
