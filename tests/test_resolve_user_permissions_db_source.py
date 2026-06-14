"""回歸：resolve_user_permissions 對 NULL-perm 帳號以 DB 角色為單一事實來源。

系統設計審查 2026-06-14, top#5：原本 NULL-perm（未明確設權限）帳號一律走 in-code
ROLE_TEMPLATES，與 DB 角色 scope 漂移時靜默提權（2026-06-04 滲透測試 #2）。修補後
傳 session 時改以 DB roles 為準；無 session / DB 未 seed 時 fallback in-code（不
lockout）。
"""

from __future__ import annotations

from models.database import User
from models.permission_models import Role
from utils.permissions import ROLE_TEMPLATES, resolve_user_permissions


def _seed_role(session, code: str, permissions: list[str]) -> None:
    session.add(
        Role(
            code=code, label=f"角色-{code}", description="test", permissions=permissions
        )
    )
    session.commit()


def _null_perm_user(session, role: str) -> User:
    u = User(
        username=f"u_{role}",
        password_hash="x",
        role=role,
        is_active=True,
        permission_names=None,  # NULL → 走 role 預設
    )
    session.add(u)
    session.commit()
    return u


def test_null_perm_reads_db_role_when_session_given(test_db_session):
    """DB 角色 perms 與 in-code 不同時，傳 session → 以 DB 為準（消除漂移提權）。"""
    s = test_db_session
    db_perms = ["DB_ONLY_PERM_A", "DB_ONLY_PERM_B"]
    _seed_role(s, "hr", db_perms)
    user = _null_perm_user(s, "hr")

    resolved = resolve_user_permissions(user, s)

    assert resolved == db_perms, f"應以 DB 角色為準，實得 {resolved}"
    # 確認 DB 值確實與 in-code 不同（否則測試 vacuous）
    assert set(db_perms) != set(ROLE_TEMPLATES.get("hr", []))


def test_null_perm_falls_back_to_in_code_when_no_session(test_db_session):
    """不傳 session → fallback in-code ROLE_TEMPLATES（向下相容）。"""
    s = test_db_session
    _seed_role(s, "hr", ["DB_ONLY_PERM_A"])
    user = _null_perm_user(s, "hr")

    resolved = resolve_user_permissions(user)  # 無 session

    assert resolved == list(ROLE_TEMPLATES.get("hr", []))


def test_null_perm_falls_back_when_role_absent_in_db(test_db_session):
    """DB roles 表沒有該 role（未 seed）→ fallback in-code，不 lockout。"""
    s = test_db_session
    # 不 seed 任何 role
    user = _null_perm_user(s, "supervisor")

    resolved = resolve_user_permissions(user, s)

    assert resolved == list(ROLE_TEMPLATES.get("supervisor", []))
    assert resolved, "fallback 不應為空（避免 NULL-perm 帳號被鎖死）"


def test_explicit_permission_names_unchanged_regardless_of_session(test_db_session):
    """有顯式 permission_names 的帳號 → 原樣回傳，與 session / DB 角色無關。"""
    s = test_db_session
    _seed_role(s, "hr", ["DB_ONLY_PERM_A"])
    u = User(
        username="explicit",
        password_hash="x",
        role="hr",
        is_active=True,
        permission_names=["EXPLICIT_X", "EXPLICIT_Y"],
    )
    s.add(u)
    s.commit()

    assert resolve_user_permissions(u, s) == ["EXPLICIT_X", "EXPLICIT_Y"]
    assert resolve_user_permissions(u) == ["EXPLICIT_X", "EXPLICIT_Y"]


def test_null_perm_empty_db_role_falls_back(test_db_session):
    """DB 角色存在但 permissions 為空 → fallback in-code（空表不視為有效來源）。"""
    s = test_db_session
    _seed_role(s, "hr", [])
    user = _null_perm_user(s, "hr")

    resolved = resolve_user_permissions(user, s)

    assert resolved == list(ROLE_TEMPLATES.get("hr", []))
