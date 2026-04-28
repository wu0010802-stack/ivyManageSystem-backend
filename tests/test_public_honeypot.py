"""tests/test_public_honeypot.py — LOW-4 honeypot + 時序測試

驗證：
- _hp 非空 → 回 200 + 偽裝訊息，但 DB 沒新增報名/提問
- _ts 距離現在不到 3 秒 → 同上
- 正常 payload（無 _hp、_ts 為 5 秒前）→ 正常寫入
- should_silent_reject_bot helper 邊界
"""

import os
import sys
import time
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity._shared import should_silent_reject_bot
from models.database import (
    ActivityRegistration,
    ActivityRegistrationSettings,
    Base,
    Classroom,  # noqa: F401
    ParentInquiry,
)


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "hp.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    # 開啟報名（_check_registration_open 依賴）
    s = session_factory()
    try:
        cfg = ActivityRegistrationSettings(id=1, is_open=True)
        s.add(cfg)
        s.commit()
    finally:
        s.close()

    # 清空 limiter 計數
    from api.activity import public as public_mod

    for attr in dir(public_mod):
        obj = getattr(public_mod, attr)
        if hasattr(obj, "_timestamps"):
            obj._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


# ── helper unit tests ────────────────────────────────────────────────


def test_helper_hp_non_empty_rejects():
    assert should_silent_reject_bot("anything", None) is True
    assert should_silent_reject_bot(" ", None) is True


def test_helper_hp_empty_passes():
    assert should_silent_reject_bot("", None) is False


def test_helper_ts_too_fresh_rejects():
    now_ms = int(time.time() * 1000)
    assert should_silent_reject_bot("", now_ms - 1000) is True  # 1 秒前


def test_helper_ts_old_enough_passes():
    now_ms = int(time.time() * 1000)
    assert should_silent_reject_bot("", now_ms - 5000) is False  # 5 秒前


def test_helper_ts_negative_elapsed_passes():
    """前端時間漂移到未來 → 不該 reject。"""
    now_ms = int(time.time() * 1000)
    assert should_silent_reject_bot("", now_ms + 5000) is False


def test_helper_invalid_ts_passes_through():
    assert should_silent_reject_bot("", None) is False


# ── 整合測試 ─────────────────────────────────────────────────────────


def test_inquiry_with_honeypot_does_not_persist(client):
    c, session_factory = client
    payload = {
        "name": "bot",
        "phone": "0912345678",
        "question": "spam",
        "_hp": "filled-by-bot",
    }
    r = c.post("/api/activity/public/inquiries", json=payload)
    assert r.status_code == 201
    # 但 DB 沒有 ParentInquiry 記錄
    s = session_factory()
    try:
        assert s.query(ParentInquiry).count() == 0
    finally:
        s.close()


def test_inquiry_with_fresh_ts_does_not_persist(client):
    c, session_factory = client
    now_ms = int(time.time() * 1000)
    payload = {
        "name": "bot",
        "phone": "0912345678",
        "question": "fast spam",
        "_ts": now_ms - 500,  # 0.5 秒前
    }
    r = c.post("/api/activity/public/inquiries", json=payload)
    assert r.status_code == 201
    s = session_factory()
    try:
        assert s.query(ParentInquiry).count() == 0
    finally:
        s.close()


def test_inquiry_normal_passes_through(client):
    c, session_factory = client
    now_ms = int(time.time() * 1000)
    payload = {
        "name": "李家長",
        "phone": "0912345678",
        "question": "請問才藝報名截止日？",
        "_ts": now_ms - 10_000,  # 10 秒前，正常人類速度
    }
    r = c.post("/api/activity/public/inquiries", json=payload)
    assert r.status_code == 201
    s = session_factory()
    try:
        assert s.query(ParentInquiry).count() == 1
    finally:
        s.close()


def test_register_with_honeypot_does_not_persist(client):
    c, session_factory = client
    payload = {
        "name": "bot-student",
        "birthday": "2020-05-01",
        "class": "中班",
        "parent_phone": "0912345678",
        "courses": [],
        "supplies": [],
        "_hp": "robot",
    }
    r = c.post("/api/activity/public/register", json=payload)
    assert r.status_code == 201
    s = session_factory()
    try:
        assert s.query(ActivityRegistration).count() == 0
    finally:
        s.close()
