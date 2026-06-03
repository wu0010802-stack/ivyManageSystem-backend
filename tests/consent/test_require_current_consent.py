"""tests/consent/test_require_current_consent.py — require_current_consent gate 整合測試。

覆蓋：
1. flag on + 未簽當期 policy → 打掛了 gate 的讀端點 → 403 + X-Consent-Required header
2. flag on + 已簽當期 policy → 通過（非 403）
3. flag on + 簽的是舊版（再 seed 新 PolicyVersion）→ 403（重簽偵測）
4. flag off → 不擋（即使沒簽）
5. 寫端點 + DB error（mock session_scope 拋例外）→ 503
6. 讀端點 + DB error → degraded 通過（非 503/403）

代表端點：
- 讀端點：GET /api/parent/home/summary（掛 require_current_consent(write=False)）
- 寫端點：POST /api/parent/messages/threads/{id}/messages（掛 require_current_consent(write=True)）
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from api.parent_portal import parent_router as parent_portal_router
from models.auth import User
from models.consent import (
    CONSENT_SCOPE_SERVICE_ESSENTIAL,
    ParentConsentLog,
    PolicyVersion,
)
from models.database import Base
from utils.auth import create_access_token
from utils.cache_layer import get_cache, reset_cache_for_testing
from utils.taipei_time import now_taipei_naive

# ── Cache isolation ───────────────────────────────────────────────────────────
# consent_check 有 60s TTL cache；parent_home_summary 也有 60s TTL cache。
# 兩者均用 user_id 作 key，跨 test SQLite DB 重用 id=1 → 必須每個 test 清除。


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture
def gate_client(tmp_path):
    """建 FastAPI + SQLite + TestClient，包含 exception_handlers（確保自訂 header 正確傳遞）。"""
    db_path = tmp_path / "consent_gate.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_portal_router)

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(sf)

    with TestClient(app) as client:
        yield client, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


# ── Seed helpers ──────────────────────────────────────────────────────────────


def _make_parent(session, username: str = "parent_gate_test") -> User:
    user = User(
        username=username,
        password_hash="!",
        role="parent",
        permission_names=[],
        is_active=True,
        token_version=0,
    )
    session.add(user)
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


def _make_policy(
    session,
    *,
    version: str = "2026.gate_test",
    seconds_ago: int = 10,
) -> PolicyVersion:
    """seed PolicyVersion，effective_at = now - seconds_ago（確保 <= now）。

    使用 seconds_ago 而非 offset_seconds，讓呼叫方直接指定「幾秒前」生效，
    避免不小心算出未來時間點（有未來時間的 PolicyVersion 不會被選為「當期政策」）。
    """
    pv = PolicyVersion(
        version=version,
        effective_at=now_taipei_naive() - timedelta(seconds=seconds_ago),
        document_path=f"/policies/{version}.pdf",
    )
    session.add(pv)
    session.flush()
    return pv


def _make_consent_log(
    session,
    user: User,
    policy: PolicyVersion,
    *,
    consented: bool = True,
) -> ParentConsentLog:
    log = ParentConsentLog(
        user_id=user.id,
        policy_version_id=policy.id,
        scope=CONSENT_SCOPE_SERVICE_ESSENTIAL,
        consented=consented,
        consented_at=now_taipei_naive(),
    )
    session.add(log)
    session.flush()
    return log


def _hit_home_summary(client: TestClient, token: str):
    """打 GET /api/parent/home/summary — 掛 write=False gate 的代表讀端點。"""
    return client.get(
        "/api/parent/home/summary",
        cookies={"access_token": token},
    )


# ── Case 1：flag on + 未簽當期 policy → 403 + X-Consent-Required header ────────


def test_unsigned_policy_returns_403_with_consent_required_header(
    gate_client, monkeypatch
):
    """flag on + 未簽當期 policy → 403 + X-Consent-Required: service_essential。

    「未簽」情境：DB 已有 PolicyVersion（不觸發 dark-period），但無對應 consent log。
    若缺 PolicyVersion seed，has_signed_current_policy 回 True（dark 期放行），
    此 test 便變成錯誤 green——PolicyVersion seed 是必要的。
    """
    from config import reset_for_tests

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        user = _make_parent(session, "parent_unsigned")
        _make_policy(session)  # 必須 seed，否則 dark 期放行
        session.commit()
        token = _parent_token(user)

    resp = _hit_home_summary(client, token)

    assert resp.status_code == 403, f"預期 403，實際 {resp.status_code}: {resp.text}"
    assert resp.headers.get("X-Consent-Required") == "service_essential", (
        f"回應應含 X-Consent-Required: service_essential header；"
        f"實際 headers: {dict(resp.headers)}"
    )


# ── Case 2：flag on + 已簽當期 policy → 通過（200）──────────────────────────────


def test_signed_current_policy_passes_gate(gate_client, monkeypatch):
    """flag on + 已簽當期 policy → gate 放行（非 403）。"""
    from config import reset_for_tests

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        user = _make_parent(session, "parent_signed")
        pv = _make_policy(session)
        _make_consent_log(session, user, pv, consented=True)
        session.commit()
        token = _parent_token(user)

    resp = _hit_home_summary(client, token)

    assert (
        resp.status_code != 403
    ), f"已簽當期 policy 不應被擋，實際 {resp.status_code}: {resp.text}"


# ── Case 3：flag on + 簽舊版 → 403（重簽偵測）────────────────────────────────────


def test_signed_old_policy_returns_403_when_new_policy_effective(
    gate_client, monkeypatch
):
    """flag on + 簽的是舊版（有更新 PolicyVersion effective）→ 403。

    兩個 PolicyVersion 都需 effective_at <= now（用 seconds_ago 確保在過去）；
    新版 effective_at 較晚（seconds_ago 較小），has_signed_current_policy 取最新版，
    log 指向舊版 → mismatch → 403。
    """
    from config import reset_for_tests

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        user = _make_parent(session, "parent_old_signed")
        # 舊版：20 秒前生效；新版：5 秒前生效 → 兩者均 <= now，新版 effective_at 較晚
        old_pv = _make_policy(session, version="2026.old", seconds_ago=20)
        new_pv = _make_policy(session, version="2026.new", seconds_ago=5)  # noqa: F841
        # 家長只簽了舊版
        _make_consent_log(session, user, old_pv, consented=True)
        session.commit()
        token = _parent_token(user)

    resp = _hit_home_summary(client, token)

    assert (
        resp.status_code == 403
    ), f"簽舊版 policy 應被 403，實際 {resp.status_code}: {resp.text}"
    assert resp.headers.get("X-Consent-Required") == "service_essential"


# ── Case 4：flag off → 不擋（即使沒簽）──────────────────────────────────────────


def test_flag_off_allows_without_consent(gate_client, monkeypatch):
    """flag off（CONSENT_ENFORCEMENT_ENABLED=false）→ gate no-op，即使沒任何 consent log。"""
    from config import reset_for_tests

    monkeypatch.delenv("CONSENT_ENFORCEMENT_ENABLED", raising=False)
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        user = _make_parent(session, "parent_flag_off")
        _make_policy(session)  # 有 policy 但沒 consent log
        session.commit()
        token = _parent_token(user)

    resp = _hit_home_summary(client, token)

    assert (
        resp.status_code != 403
    ), f"flag off 不應擋 consent，實際 {resp.status_code}: {resp.text}"


# ── Case 5：寫端點 + DB error → 503（fail-closed）────────────────────────────────


def test_write_endpoint_db_error_returns_503(gate_client, monkeypatch):
    """POST 寫端點 + session_scope 拋 DB error + write=True → 503（fail-closed）。

    503 由 gate dependency 在 has_signed_current_policy 執行前就拋，
    故不需要 seed thread（端點 body 根本不會被執行）。

    Patch 目標必須是 api.parent_portal._consent_gate.session_scope（綁在 gate 模組的名稱空間），
    而非 models.base.session_scope（不同名稱空間，patch 不穿透）。
    """
    from config import reset_for_tests

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        user = _make_parent(session, "parent_write_db_err")
        session.commit()
        token = _parent_token(user)

    with patch(
        "api.parent_portal._consent_gate.session_scope",
        side_effect=RuntimeError("DB connection lost"),
    ):
        resp = client.post(
            "/api/parent/messages/threads/999/messages",
            cookies={"access_token": token},
            json={"body": "測試訊息"},
        )

    assert (
        resp.status_code == 503
    ), f"寫端點 DB error 應 fail-closed 回 503，實際 {resp.status_code}: {resp.text}"


# ── Case 6：讀端點 + DB error → degraded 通過（非 503/403）──────────────────────


def test_read_endpoint_db_error_passes_degraded(gate_client, monkeypatch):
    """GET 讀端點 + session_scope 拋 DB error + write=False → degraded fail-open（非 503/403）。

    讀路徑 DB 查詢失敗時，gate 記 WARNING 並回 current_user（降級放行），
    避免 DB 短暫異常讓所有家長無法查看自己的資料。
    """
    from config import reset_for_tests

    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    reset_for_tests()

    client, sf = gate_client
    with sf() as session:
        user = _make_parent(session, "parent_read_db_err")
        session.commit()
        token = _parent_token(user)

    with patch(
        "api.parent_portal._consent_gate.session_scope",
        side_effect=RuntimeError("DB connection lost"),
    ):
        resp = _hit_home_summary(client, token)

    assert resp.status_code not in (
        403,
        503,
    ), f"讀端點 DB error 應 degraded 放行（非 403/503），實際 {resp.status_code}: {resp.text}"
