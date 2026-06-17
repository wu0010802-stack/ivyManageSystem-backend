"""模擬（impersonation）進行中須即時檢查 impersonator(admin) 本人 is_active。

#5（qa-loop 全掃 2026-06-17，業主裁示加 is_active 即時檢查）：
get_current_user 在模擬期間只驗 target（被模擬者）帳號，從不查 impersonated_by(admin)
本人狀態 → admin 被停用後，既有模擬 access_token 仍可正常通過認證並寫入直到過期
（≤15min、不可刷新）。修法：_resolve_user_auth_fields 在 impersonated_by 非 None 時
額外查 impersonator，is_active=False 即 401 終止模擬。非模擬請求（impersonated_by=None）
不增加任何查詢，熱路徑不受影響。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from models.database import User
from utils.auth import _resolve_user_auth_fields, hash_password


def _mk_user(session, username, role, is_active):
    u = User(
        username=username,
        password_hash=hash_password("p"),
        role=role,
        is_active=is_active,
        token_version=0,
        must_change_password=False,
    )
    session.add(u)
    session.commit()
    return u


def test_blocks_when_impersonator_admin_disabled(test_db_session):
    session = test_db_session
    admin = _mk_user(session, "adm", "admin", is_active=False)  # 模擬期間被停用
    target = _mk_user(session, "tgt", "teacher", is_active=True)

    with pytest.raises(HTTPException) as exc:
        _resolve_user_auth_fields(target.id, 0, "/api/x", impersonated_by=admin.id)
    assert exc.value.status_code == 401


def test_allows_when_impersonator_admin_active(test_db_session):
    session = test_db_session
    admin = _mk_user(session, "adm", "admin", is_active=True)
    target = _mk_user(session, "tgt", "teacher", is_active=True)

    must_change, username = _resolve_user_auth_fields(
        target.id, 0, "/api/x", impersonated_by=admin.id
    )
    assert username == "tgt"
    assert must_change is False


def test_non_impersonation_unaffected(test_db_session):
    """impersonated_by=None（一般請求）行為不變。"""
    session = test_db_session
    target = _mk_user(session, "tgt", "teacher", is_active=True)
    must_change, username = _resolve_user_auth_fields(target.id, 0, "/api/x")
    assert username == "tgt"
