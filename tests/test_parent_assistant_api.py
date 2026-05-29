"""GET /api/parent/assistant/faq 整合測試。

驗證：
- 已認證家長可拿到 FAQ 資料（version / items / categories）
- 未帶 token 回 401
- 回應帶 Cache-Control: private, max-age=300

Fixture 模式參考 tests/test_parent_calendar_v2.py：
sqlite 暫存 DB + JWT + FastAPI TestClient。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router
from models.database import Base, Guardian, Student, User
from utils.auth import create_access_token


@pytest.fixture
def parent_client(tmp_path, monkeypatch):
    db_path = tmp_path / "assistant.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(db_engine)

    # 建一位家長（含對應 Guardian 紀錄）
    with sf() as s:
        parent = User(
            username="parent_assistant_test",
            password_hash="!",
            role="parent",
            permission_names=[],
            is_active=True,
            token_version=0,
        )
        s.add(parent)
        s.flush()
        student = Student(
            student_id="ST_ASSIST",
            name="小測",
            is_active=True,
        )
        s.add(student)
        s.flush()
        guardian = Guardian(
            student_id=student.id,
            user_id=parent.id,
            name="家長",
            relation="父親",
            is_primary=True,
            can_pickup=True,
        )
        s.add(guardian)
        s.commit()

        token = create_access_token(
            {
                "user_id": parent.id,
                "employee_id": None,
                "role": "parent",
                "name": parent.username,
                "permission_names": [],
                "token_version": 0,
            }
        )

    # 重定向 FAQ 檔到 tmp，避免依賴正式檔
    from services import parent_assistant_service as svc

    faq_path = tmp_path / "parent_faq.json"
    faq_path.write_text(
        json.dumps(
            {
                "version": "9.9.9",
                "updated_at": "2026-05-16",
                "categories": [
                    {"id": "leave", "label": "請假", "icon": "x", "color": "#000"}
                ],
                "items": [
                    {
                        "id": "x",
                        "category": "leave",
                        "question": "Q?",
                        "keywords": [],
                        "answer": "A",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    svc.ParentAssistantService._cache = None
    svc.ParentAssistantService._cached_mtime = None
    monkeypatch.setattr(svc.ParentAssistantService, "_path", faq_path)

    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_router)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    yield client

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    db_engine.dispose()


def test_get_faq_returns_data(parent_client):
    r = parent_client.get("/api/parent/assistant/faq")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "9.9.9"
    assert body["items"][0]["id"] == "x"
    assert body["categories"][0]["label"] == "請假"


def test_get_faq_requires_auth():
    """未帶 token 應 401。"""
    app = FastAPI()
    app.include_router(parent_router)
    client = TestClient(app)
    r = client.get("/api/parent/assistant/faq")
    assert r.status_code == 401


def test_get_faq_sets_cache_header(parent_client):
    r = parent_client.get("/api/parent/assistant/faq")
    assert "private" in r.headers.get("Cache-Control", "")
    assert "max-age=300" in r.headers.get("Cache-Control", "")
