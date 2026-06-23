"""config GET 端點補 response_model 的契約 + 特徵化測試。

兩類測試：
1. 特徵化（characterization）：鎖住 GET /titles、/grade-targets、/position-salary
   的回傳「形狀」（key 集合、空值行為）。在『加 response_model 前後都必須 GREEN』，
   證明 response_model 是忠實 superset、沒有靜默丟欄/改形狀。
2. 契約（OpenAPI）：斷言這三個端點的 200 response schema 不再是空 `{}`（無型別），
   而是具名 typed schema。加 response_model 前為 RED、後為 GREEN。

只覆蓋前端真正消費、且無 `{}` empty-sentinel 風險的 3 個端點：
  - GET /titles          → list[JobTitleOut]        （空表回 [] 安全）
  - GET /grade-targets   → dict[str, GradeTargetOut]（空回 {} 由 Dict 型別保留）
  - GET /position-salary → PositionSalaryOut        （無資料回完整預設物件，非 {}）

刻意不含 GET /bonus / /attendance-policy / /insurance-rates：前兩者前端零消費，
/bonus 雖 9 caller 但空回 `{}` 且前端 Object.assign 會被 {全 null} 覆蓋預設值，
需前端協同改動，另案處理。
"""

import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.config import _clear_cache, init_config_services
from api.config import router as config_router
from models.database import (
    Base,
    BonusConfig as DBBonusConfig,
    GradeTarget,
    JobTitle,
    PositionSalaryConfig,
    User,
)
from services.salary.engine import SalaryEngine
from utils.auth import hash_password


@pytest.fixture
def cfg_client(tmp_path):
    db_path = tmp_path / "cfg.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    # GET 端點不觸發 engine，但 init 需要非 None 才不報錯；用真實 engine（load_from_db=False）
    init_config_services(SalaryEngine(load_from_db=False), MagicMock())
    # 清 config namespace cache，避免 module-global 快取跨測試污染
    _clear_cache()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(config_router)

    with TestClient(app) as client:
        yield client, session_factory, app

    _ip_attempts.clear()
    _account_failures.clear()
    _clear_cache()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login(client, session_factory, username="cfg_admin"):
    with session_factory() as session:
        session.add(
            User(
                username=username,
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["SETTINGS_READ", "SETTINGS_WRITE"],
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login",
        json={"username": username, "password": "TempPass123"},
    )
    assert res.status_code == 200


# ======================= 特徵化：GET /titles =======================

_TITLE_KEYS = {"id", "name", "bonus_grade"}


def test_titles_empty_returns_empty_list(cfg_client):
    client, session_factory, _ = cfg_client
    _login(client, session_factory)
    res = client.get("/api/config/titles")
    assert res.status_code == 200
    assert res.json() == []


def test_titles_shape_preserved(cfg_client):
    client, session_factory, _ = cfg_client
    with session_factory() as session:
        session.add_all(
            [
                JobTitle(name="主任", is_active=True, sort_order=1, bonus_grade="A"),
                JobTitle(name="助教", is_active=True, sort_order=2, bonus_grade=None),
            ]
        )
        session.commit()
    _login(client, session_factory)
    res = client.get("/api/config/titles")
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, list) and len(body) == 2
    for item in body:
        assert set(item.keys()) == _TITLE_KEYS
    by_name = {i["name"]: i for i in body}
    assert by_name["主任"]["bonus_grade"] == "A"
    assert by_name["助教"]["bonus_grade"] is None


# ==================== 特徵化：GET /grade-targets ====================

_GRADE_TARGET_KEYS = {
    "id",
    "festival_two_teachers",
    "festival_one_teacher",
    "festival_shared",
    "overtime_two_teachers",
    "overtime_one_teacher",
    "overtime_shared",
}


def test_grade_targets_empty_returns_empty_dict(cfg_client):
    client, session_factory, _ = cfg_client
    _login(client, session_factory)
    res = client.get("/api/config/grade-targets")
    assert res.status_code == 200
    assert res.json() == {}


def test_grade_targets_shape_preserved(cfg_client):
    client, session_factory, _ = cfg_client
    with session_factory() as session:
        bonus = DBBonusConfig(is_active=True, config_year=2026, version=1)
        session.add(bonus)
        session.flush()
        session.add(
            GradeTarget(
                config_year=2026,
                grade_name="大班",
                bonus_config_id=bonus.id,
                festival_two_teachers=10,
                festival_one_teacher=8,
                festival_shared=5,
                overtime_two_teachers=4,
                overtime_one_teacher=3,
                overtime_shared=2,
            )
        )
        session.commit()
    _login(client, session_factory)
    res = client.get("/api/config/grade-targets")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"大班"}
    assert set(body["大班"].keys()) == _GRADE_TARGET_KEYS
    assert body["大班"]["festival_two_teachers"] == 10
    assert body["大班"]["overtime_shared"] == 2


# =================== 特徵化：GET /position-salary ===================

_POSITION_SALARY_KEYS = {
    "id",
    "head_teacher_a",
    "head_teacher_b",
    "head_teacher_c",
    "assistant_teacher_a",
    "assistant_teacher_b",
    "assistant_teacher_c",
    "admin_staff",
    "english_teacher",
    "art_teacher",
    "designer",
    "nurse",
    "driver",
    "kitchen_staff",
    "director",
    "principal",
    "version",
    "changed_by",
}


def test_position_salary_no_config_returns_full_default(cfg_client):
    """無資料時回完整預設物件（非 {}），key 集合固定、id/version 為預設。"""
    client, session_factory, _ = cfg_client
    _login(client, session_factory)
    res = client.get("/api/config/position-salary")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == _POSITION_SALARY_KEYS
    assert body["id"] is None
    assert body["version"] == 0
    assert body["head_teacher_a"] == 39240
    assert body["director"] is None


def test_position_salary_with_config_shape_preserved(cfg_client):
    client, session_factory, _ = cfg_client
    with session_factory() as session:
        session.add(
            PositionSalaryConfig(
                config_year=2026,
                version=3,
                changed_by="tester",
                head_teacher_a=40000,
                head_teacher_b=38000,
                head_teacher_c=34000,
                assistant_teacher_a=36000,
                assistant_teacher_b=33000,
                assistant_teacher_c=30000,
            )
        )
        session.commit()
    _login(client, session_factory)
    res = client.get("/api/config/position-salary")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == _POSITION_SALARY_KEYS
    assert body["version"] == 3
    assert body["changed_by"] == "tester"
    assert body["head_teacher_a"] == 40000


# ==================== 契約：OpenAPI typed response ====================
# 加 response_model 前：FastAPI 對無 response_model 的端點輸出 schema == {}（無型別）。
# 加 response_model 後：輸出具名 typed schema（array/object/$ref），!= {}。


def _resp_schema(app, path):
    return app.openapi()["paths"][path]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]


def test_titles_has_typed_response_schema(cfg_client):
    _, _, app = cfg_client
    schema = _resp_schema(app, "/api/config/titles")
    assert (
        schema != {}
    ), "GET /titles 應有具名 typed response schema（list[JobTitleOut]）"
    assert schema.get("type") == "array"


def test_grade_targets_has_typed_response_schema(cfg_client):
    _, _, app = cfg_client
    schema = _resp_schema(app, "/api/config/grade-targets")
    assert schema != {}, "GET /grade-targets 應有具名 typed response schema"
    assert schema.get("type") == "object"


def test_position_salary_has_typed_response_schema(cfg_client):
    _, _, app = cfg_client
    schema = _resp_schema(app, "/api/config/position-salary")
    assert schema != {}, "GET /position-salary 應有具名 typed response schema（$ref）"
    assert "$ref" in schema or schema.get("type") == "object"
