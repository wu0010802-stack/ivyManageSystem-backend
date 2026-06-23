"""P1-1 回歸（2026-06-23 全系統資安掃描）：公開查詢【讀取側】身分驗證空對空
（NULL-phone）繞過。

威脅：後台建立的報名（registrations.py create 路徑）不寫 parent_phone（NULL）。
- /public/query（三欄查詢）比對迴圈 `_normalize_phone(candidate.parent_phone) == normalized_phone`，
  攻擊者送 parent_phone="--------"（8 個連字號，過 min_length=8）→ strip 後為空 → None；
  _normalize_phone(NULL) → None → `None == None → True` → 命中 → 回傳完整兒童 PII。
- /public/query-by-token 同理：reg.parent_phone 為 NULL 時 `None != None → False`，phone 第二
  因素失效。

對照：mutation 側 _parent_mutation_identity_ok（public.py）已於 2026-06-23 補空對空守衛
（norm_reg_phone is not None and ...），但讀取側兩支查詢端點漏補即此漏洞。

DB 隔離：沿用 SQLite + monkeypatch base_module（不碰 dev PG）。
"""

import os
import sys
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import _hash_query_token, _public_query_limiter_instance
from models.database import ActivityRegistration, Base
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def query_client(tmp_path):
    db_path = tmp_path / "query_null_phone.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    _public_query_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, sf

    _public_query_limiter_instance._timestamps.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _insert_null_phone_reg(session, *, token_hash=None):
    """後台建立報名的等價：parent_phone=NULL。token_hash 可選（query-by-token 防禦測試用）。"""
    sy, sem = _term()
    reg = ActivityRegistration(
        student_name="王小明",
        birthday="2020-05-10",
        class_name="海豚班",
        school_year=sy,
        semester=sem,
        parent_phone=None,  # ← 後台建立路徑不寫手機
        query_token_hash=token_hash,
        query_token_issued_at=now_taipei_naive() if token_hash else None,
        is_active=True,
        match_status="matched",
        paid_amount=0,
    )
    session.add(reg)
    session.commit()
    return reg.id


def _insert_phone_reg(session):
    """有有效手機的正常報名（sanity：修復不得誤傷正常查詢）。"""
    sy, sem = _term()
    reg = ActivityRegistration(
        student_name="陳小華",
        birthday="2019-03-03",
        class_name="海豚班",
        school_year=sy,
        semester=sem,
        parent_phone="0912345678",
        is_active=True,
        match_status="matched",
        paid_amount=0,
    )
    session.add(reg)
    session.commit()
    return reg.id


def test_public_query_null_phone_dash_bypass_blocked(query_client):
    """攻擊者僅知 name+birthday，送 parent_phone='--------'（normalize→None）
    不得讀取 NULL-phone 報名的 PII。"""
    client, sf = query_client
    with sf() as s:
        _insert_null_phone_reg(s)

    res = client.post(
        "/api/activity/public/query",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "--------",  # 8 dash 過 min_length=8；strip 後 None
        },
    )
    assert (
        res.status_code == 404
    ), f"空對空查詢不得回傳 PII，得到 {res.status_code}: {res.text}"


def test_public_query_by_token_null_phone_dash_bypass_blocked(query_client):
    """query-by-token：reg 有有效 token 但 parent_phone=NULL 時，
    phone 第二因素不得被空對空繞過。"""
    client, sf = query_client
    token = "TESTTOKEN1234567890"
    with sf() as s:
        _insert_null_phone_reg(s, token_hash=_hash_query_token(token))

    res = client.post(
        "/api/activity/public/query-by-token",
        json={"token": token, "parent_phone": "--------"},
    )
    assert (
        res.status_code == 404
    ), f"空對空 token 查詢不得回傳 PII，得到 {res.status_code}: {res.text}"


def test_public_query_normal_phone_still_matches(query_client):
    """sanity：有有效手機的正常報名，正確三欄仍可查（修復不得誤傷）。"""
    client, sf = query_client
    with sf() as s:
        _insert_phone_reg(s)

    res = client.post(
        "/api/activity/public/query",
        json={
            "name": "陳小華",
            "birthday": "2019-03-03",
            "parent_phone": "0912345678",
        },
    )
    assert res.status_code == 200, f"正常查詢應成功，得到 {res.status_code}: {res.text}"
    assert res.json()["name"] == "陳小華"
