"""模擬（impersonation）進行中須即時檢查 impersonator(admin) 的 token_version。

qa-loop #4（2026-06-23）：#5（2026-06-17）已加 impersonator is_active 即時檢查，但
對稱的 token_version 失效窗口未關——admin 被 reset_password / update_user 改角色權限 /
logout-all（皆 bump token_version、is_active 維持 True）後，先前簽發的模擬 access_token
仍以 target 權限通過認證並寫入直到自然過期（≤15min、不可刷新）。情境：憑證疑似外洩 →
另一 admin 對其 reset_password 期望全域踢出，但其進行中的模擬 session（或被竊模擬 cookie）
仍可繼續代操作。

修法：模擬 token 簽發時帶入 admin 的 token_version（impersonator_token_version claim），
_resolve_user_auth_fields 在模擬期間比對 impersonator 現行 token_version，不符即 401。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from models.database import User
from utils.auth import _resolve_user_auth_fields, hash_password


def _mk_user(session, username, role, token_version=0):
    u = User(
        username=username,
        password_hash=hash_password("p"),
        role=role,
        is_active=True,
        token_version=token_version,
        must_change_password=False,
    )
    session.add(u)
    session.commit()
    return u


def test_blocks_when_impersonator_token_version_bumped(test_db_session):
    """admin token_version 在模擬飛行中被 bump（reset_password/改權/logout-all）→ 401。"""
    session = test_db_session
    admin = _mk_user(session, "adm", "admin", token_version=5)  # DB 已 bump 到 5
    target = _mk_user(session, "tgt", "teacher")

    # 模擬 token 簽發於 bump 前，帶 admin token_version=4（與現行 DB=5 不符）
    with pytest.raises(HTTPException) as exc:
        _resolve_user_auth_fields(
            target.id,
            0,
            "/api/x",
            impersonated_by=admin.id,
            impersonator_token_version=4,
        )
    assert exc.value.status_code == 401


def test_allows_when_impersonator_token_version_matches(test_db_session):
    """admin token_version 未變（claim 與現行 DB 一致）→ 模擬照常通過。"""
    session = test_db_session
    admin = _mk_user(session, "adm", "admin", token_version=3)
    target = _mk_user(session, "tgt", "teacher")

    must_change, username = _resolve_user_auth_fields(
        target.id,
        0,
        "/api/x",
        impersonated_by=admin.id,
        impersonator_token_version=3,
    )
    assert username == "tgt"
    assert must_change is False


def test_non_impersonation_unaffected(test_db_session):
    """一般請求（impersonated_by=None）行為不變，不因新參數受影響。"""
    session = test_db_session
    target = _mk_user(session, "tgt", "teacher")
    must_change, username = _resolve_user_auth_fields(target.id, 0, "/api/x")
    assert username == "tgt"
    assert must_change is False
