"""tests/consent/test_consent_checker.py — consent_check 純函式測試。

覆蓋：
- 同一 user+scope 先同意後撤回 → False（最新一筆 wins）
- 只有一筆 consented=True → True
- 無任何記錄 → False
- per-student：主要聯絡人不同意、次要同意 → False（以主要為準）
- 快取：set 後 get 命中；invalidate 後 miss
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from models.auth import User
from models.classroom import Student
from models.consent import (
    CONSENT_SCOPE_PHOTO_PUBLISH,
    CONSENT_SCOPE_LINE_PUSH,
    ParentConsentLog,
    PolicyVersion,
)
from models.guardian import Guardian
from utils.taipei_time import now_taipei_naive
from utils.cache_layer import get_cache, reset_cache_for_testing

# ── Cache isolation ────────────────────────────────────────────
# 每個 test 使用獨立 SQLite DB（test_db_session），但 cache singleton 跨 test
# 共用。reset_cache_for_testing() 確保每個 test 從乾淨 cache 開始，
# 避免 user_id=1 在不同 test DB 間的 cache key 碰撞。


@pytest.fixture(autouse=True)
def _reset_consent_cache():
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


# ── Seed helpers ──────────────────────────────────────────────────────────────


def _make_policy(session) -> PolicyVersion:
    pv = PolicyVersion(
        version="2026.test",
        effective_at=now_taipei_naive(),
        document_path="/policies/2026-test.pdf",
    )
    session.add(pv)
    session.flush()
    return pv


def _make_parent_user(session, username: str = "parent_test") -> User:
    user = User(
        username=username,
        password_hash="hashed",
        role="parent",
    )
    session.add(user)
    session.flush()
    return user


def _make_consent_log(
    session,
    user: User,
    policy: PolicyVersion,
    scope: str,
    consented: bool,
    consented_at=None,
) -> ParentConsentLog:
    log = ParentConsentLog(
        user_id=user.id,
        policy_version_id=policy.id,
        scope=scope,
        consented=consented,
        consented_at=consented_at if consented_at is not None else now_taipei_naive(),
    )
    session.add(log)
    session.flush()
    return log


def _make_student(session) -> Student:
    student = Student(
        student_id="S_TEST_CONSENT",
        name="測試學生",
        lifecycle_status="active",
    )
    session.add(student)
    session.flush()
    return student


def _make_guardian(
    session,
    student: Student,
    user: User,
    is_primary: bool = False,
) -> Guardian:
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name=user.username,
        is_primary=is_primary,
    )
    session.add(guardian)
    session.flush()
    return guardian


# ── 核心 consent_check 測試 ───────────────────────────────────────────────────


def test_consent_check_revoked_after_consent_returns_false(test_db_session):
    """同一 user+scope：先同意，後撤回（consented_at 較晚）→ False（最新一筆 wins）。"""
    from services.consent.checker import consent_check

    session = test_db_session
    pv = _make_policy(session)
    user = _make_parent_user(session, "parent_revoke_test")
    scope = CONSENT_SCOPE_PHOTO_PUBLISH

    t0 = now_taipei_naive()
    t1 = t0 + timedelta(hours=1)

    # 第一筆：同意（時間早）
    _make_consent_log(session, user, pv, scope, consented=True, consented_at=t0)
    # 第二筆：撤回（時間晚）
    _make_consent_log(session, user, pv, scope, consented=False, consented_at=t1)

    result = consent_check(session, user.id, scope)
    assert result is False, "最新一筆是撤回，consent_check 應回 False"


def test_consent_check_single_consented_returns_true(test_db_session):
    """只有一筆 consented=True → 回 True。"""
    from services.consent.checker import consent_check

    session = test_db_session
    pv = _make_policy(session)
    user = _make_parent_user(session, "parent_single_true")
    scope = CONSENT_SCOPE_PHOTO_PUBLISH

    _make_consent_log(session, user, pv, scope, consented=True)

    result = consent_check(session, user.id, scope)
    assert result is True, "唯一記錄 consented=True 應回 True"


def test_consent_check_no_record_returns_false(test_db_session):
    """無任何記錄 → 回 False。"""
    from services.consent.checker import consent_check

    session = test_db_session
    user = _make_parent_user(session, "parent_no_record")
    scope = CONSENT_SCOPE_PHOTO_PUBLISH

    result = consent_check(session, user.id, scope)
    assert result is False, "無記錄應回 False"


# ── per-student 測試 ──────────────────────────────────────────────────────────


def test_consent_check_student_scope_primary_wins(test_db_session):
    """主要聯絡人不同意、次要聯絡人同意 → consent_check_student_scope 回 False（以主要為準）。"""
    from services.consent.checker import consent_check_student_scope

    session = test_db_session
    pv = _make_policy(session)
    scope = CONSENT_SCOPE_LINE_PUSH

    student = _make_student(session)
    primary_user = _make_parent_user(session, "parent_primary")
    secondary_user = _make_parent_user(session, "parent_secondary")

    # 主要聯絡人：不同意
    _make_consent_log(session, primary_user, pv, scope, consented=False)
    # 次要聯絡人：同意
    _make_consent_log(session, secondary_user, pv, scope, consented=True)

    _make_guardian(session, student, primary_user, is_primary=True)
    _make_guardian(session, student, secondary_user, is_primary=False)

    result = consent_check_student_scope(session, student.id, scope)
    assert result is False, "主要聯絡人不同意，即使次要同意也應回 False"


# ── 快取測試 ──────────────────────────────────────────────────────────────────


def test_consent_cache_set_and_get(test_db_session):
    """set 後 get 命中（快取 hit）。"""
    reset_cache_for_testing()
    cache = get_cache()

    cache.set("consent", "user_1:photo_publish", True, ttl=60)
    val = cache.get("consent", "user_1:photo_publish")
    assert val is True, "set 後應能 get 到同值"


def test_invalidate_consent_cache_clears_entry(test_db_session):
    """invalidate 後 miss（快取 miss）。

    key 格式與 consent_check 一致：f"{user_id}:{scope}"（不含 "user_" 前綴）。
    """
    from services.consent.checker import invalidate_consent_cache

    reset_cache_for_testing()
    cache = get_cache()

    # key 格式："{user_id}:{scope}"，與 checker.py _CACHE_NS / key 一致
    cache_key = "2:line_push"
    cache.set("consent", cache_key, True, ttl=60)
    assert cache.get("consent", cache_key) is True, "set 前置確認"

    invalidate_consent_cache(2, "line_push")
    val = cache.get("consent", cache_key)
    assert val is None, "invalidate 後 cache 應 miss（None）"
