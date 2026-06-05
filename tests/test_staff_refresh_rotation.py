"""Spec F: staff refresh rotation pytest — 9 tests。

涵蓋：
1. issue_refresh_token 寫 DB
2. rotate_refresh_token 回新 token
3. rotate 標舊 token used_at
4. reuse 超 race window → 整 family revoked + token_version bump
5. 過期 token 拒絕
6. revoke_family 把整 family 標 revoked
7. revoke_all_for_user 撤全部 + bump token_version
8. 無效 token 字串 → 401
9. 已撤銷 token → 401

Spec F §3.5 / §4
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from models.auth import User
from models.staff_refresh_token import StaffRefreshToken
from services.staff_refresh import (
    issue_refresh_token,
    revoke_all_for_user,
    revoke_family,
    rotate_refresh_token,
)

# ── fixture ──────────────────────────────────────────────────────────────────


@pytest.fixture
def staff_user(test_db_session):
    """建立一個測試用員工 User。"""
    from utils.auth import hash_password

    user = User(
        username="staff_rotation_tester",
        password_hash=hash_password("pw"),
        role="teacher",
        is_active=True,
        token_version=0,
        permission_names=[],
    )
    test_db_session.add(user)
    test_db_session.commit()
    return user


# ── 1. issue token 寫 DB ──────────────────────────────────────────────────────


def test_issue_refresh_token_writes_db(test_db_session, staff_user):
    """issue_refresh_token 應回傳非空 raw token + 寫入 DB。"""
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl/8.0", ip="1.1.1.1")
    assert len(raw) > 32
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    assert rt is not None
    assert rt.user_id == staff_user.id
    assert rt.user_agent == "curl/8.0"
    assert rt.ip == "1.1.1.1"
    assert rt.expires_at > datetime.now()
    assert rt.used_at is None
    assert rt.revoked_at is None


# ── 2. rotate 回新 token ──────────────────────────────────────────────────────


def test_rotate_refresh_returns_new_token(test_db_session, staff_user):
    """rotate_refresh_token 應回傳與原 token 不同的新 token 及正確 user_id。"""
    raw, _ = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    new_raw, uid = rotate_refresh_token(raw, "curl", "1.1.1.1")
    assert new_raw != raw
    assert len(new_raw) > 32
    assert uid == staff_user.id


# ── 3. rotate 標舊 token used_at ─────────────────────────────────────────────


def test_rotate_marks_old_token_used(test_db_session, staff_user):
    """rotate 後舊 token 的 used_at 應不為 None。"""
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rotate_refresh_token(raw, "curl", "1.1.1.1")
    test_db_session.expire_all()
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    assert rt.used_at is not None


# ── 4. reuse 超 race window → family revoke + token_version bump ─────────────


def test_rotate_reuse_revokes_family(test_db_session, staff_user):
    """重複用 used token（超出 5s race tolerance）→ 整 family revoked + 401 + bump token_version。"""
    raw, _ = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rotate_refresh_token(raw, "curl", "1.1.1.1")  # 第一次 rotate OK

    # mock now_taipei_naive 讓 elapsed = 10s > RACE_TOLERANCE_SECONDS(5s)
    import services.staff_refresh as sr_mod

    future_now = datetime.now() + timedelta(seconds=10)
    with patch.object(sr_mod, "now_taipei_naive", return_value=future_now):
        with pytest.raises(HTTPException) as exc_info:
            rotate_refresh_token(raw, "curl", "1.1.1.1")
    assert exc_info.value.status_code == 401
    assert "重用" in exc_info.value.detail

    test_db_session.expire_all()
    user = test_db_session.get(User, staff_user.id)
    assert user.token_version >= 1

    # 同 family 所有 token 應全部 revoked
    tokens = (
        test_db_session.query(StaffRefreshToken).filter_by(user_id=staff_user.id).all()
    )
    assert all(t.revoked_at is not None for t in tokens)


# ── 5. 過期 token 拒絕 ────────────────────────────────────────────────────────


def test_rotate_expired_token_rejected(test_db_session, staff_user):
    """過期的 token 應被拒絕（401）。"""
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    rt.expires_at = datetime.now() - timedelta(days=1)
    test_db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        rotate_refresh_token(raw, "curl", "1.1.1.1")
    assert exc_info.value.status_code == 401
    assert "過期" in exc_info.value.detail


# ── 6. revoke_family 把整 family 標 revoked ──────────────────────────────────


def test_revoke_family_marks_all_revoked(test_db_session, staff_user):
    """revoke_family 應把指定 family 所有未撤 token 標為 revoked。"""
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    family_id = rt.family_id

    n = revoke_family(staff_user.id, family_id)
    assert n == 1

    test_db_session.expire_all()
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    assert rt.revoked_at is not None


# ── 7. revoke_all_for_user 撤全部 + bump token_version ───────────────────────


def test_revoke_all_bumps_token_version(test_db_session, staff_user):
    """revoke_all_for_user 應撤銷所有 family + bump token_version。"""
    issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    issue_refresh_token(staff_user.id, user_agent="firefox", ip="2.2.2.2")

    revoke_all_for_user(staff_user.id)

    test_db_session.expire_all()
    user = test_db_session.get(User, staff_user.id)
    assert user.token_version >= 1

    tokens = (
        test_db_session.query(StaffRefreshToken).filter_by(user_id=staff_user.id).all()
    )
    assert len(tokens) == 2
    assert all(t.revoked_at is not None for t in tokens)


# ── 8. 無效 token 字串 → 401 ─────────────────────────────────────────────────


def test_invalid_refresh_token_rejected(test_db_session):
    """不存在的 token → 401。"""
    with pytest.raises(HTTPException) as exc_info:
        rotate_refresh_token("this_token_was_never_issued_by_server", "curl", "1.1.1.1")
    assert exc_info.value.status_code == 401
    assert "不存在" in exc_info.value.detail


# ── 9. 已撤銷 token → 401 ────────────────────────────────────────────────────


def test_revoked_token_rejected(test_db_session, staff_user):
    """已被 revoke 的 token 再送來 → 401。"""
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    rt.revoked_at = datetime.now()
    test_db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        rotate_refresh_token(raw, "curl", "1.1.1.1")
    assert exc_info.value.status_code == 401
    assert "撤銷" in exc_info.value.detail


# ── Finding F1：family absolute session lifetime ──


def test_rotate_rejected_when_family_exceeds_absolute_lifetime(
    test_db_session, staff_user
):
    """family 從首次登入起算超過 absolute lifetime → rotation 須 401 + 撤整 family，
    封死「失竊/棄置 refresh cookie 無限期 rotate 延續登入」。"""
    raw, rt_id = issue_refresh_token(staff_user.id, user_agent="curl", ip="1.1.1.1")
    rt = test_db_session.get(StaffRefreshToken, rt_id)
    # family 誕生在很久以前（遠超 absolute lifetime），但 token 本身尚未過期
    rt.created_at = datetime.now() - timedelta(hours=100000)
    test_db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        rotate_refresh_token(raw, "curl", "1.1.1.1")
    assert exc_info.value.status_code == 401

    test_db_session.expire_all()
    tokens = (
        test_db_session.query(StaffRefreshToken).filter_by(user_id=staff_user.id).all()
    )
    assert all(
        t.revoked_at is not None for t in tokens
    ), "超過 absolute lifetime 的 family 須全撤銷"
