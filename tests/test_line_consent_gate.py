"""
Spec E P0 #6：LINE 推播跨境合規 consent gate 測試

涵蓋：
1. _check_line_push_consent：3 個 DB-level case + 1 DB error case
2. build_*_message 去識別化：2 個代表性 case
3. 家長分支改查 ParentConsentLog（Task 5）
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from services.line_service import (
    _check_line_push_consent,
    build_activity_waitlist_promoted_message,
    build_dismissal_message,
)
from utils.cache_layer import reset_cache_for_testing

# ── Cache isolation（consent_check 有 60s TTL cache，測試間必須重置）────────────


@pytest.fixture(autouse=True)
def _reset_consent_cache():
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


# ── _check_line_push_consent ──────────────────────────────────────────────────


def test_check_line_push_consent_user_not_bound_returns_false(test_db_session):
    """LINE user_id 未綁定任何 User → 回傳 False（fail-closed）。"""
    result = _check_line_push_consent("U_nonexistent_line_id")
    assert result is False


def test_check_line_push_consent_consent_false_returns_false(test_db_session):
    """家長已綁定 LINE 但 line_push_consent=False → 回傳 False（家長須 explicit opt-in）。"""
    from models.auth import User

    user = User(
        username="test_parent_no_consent",
        password_hash="hashed",
        role="parent",
        line_user_id="U_consent_false_001",
        line_push_consent=False,
    )
    test_db_session.add(user)
    test_db_session.commit()

    result = _check_line_push_consent("U_consent_false_001")
    assert result is False


def test_check_line_push_consent_consent_true_returns_true(test_db_session):
    """家長已綁定 LINE 且 ParentConsentLog 最新一筆 consented=True → 回傳 True。

    Task 5 後數據源改為 ParentConsentLog；user.line_push_consent 已退役（不再讀取）。
    """
    from models.auth import User
    from models.consent import ParentConsentLog, PolicyVersion, CONSENT_SCOPE_LINE_PUSH
    from utils.taipei_time import now_taipei_naive

    pv = PolicyVersion(
        version="2026.gate.t1",
        effective_at=now_taipei_naive(),
        document_path="/policies/gate-t1.pdf",
    )
    test_db_session.add(pv)
    test_db_session.flush()

    user = User(
        username="test_parent_with_consent",
        password_hash="hashed",
        role="parent",
        line_user_id="U_consent_true_001",
        line_push_consent=False,  # 舊欄位不影響結果
    )
    test_db_session.add(user)
    test_db_session.flush()

    log = ParentConsentLog(
        user_id=user.id,
        policy_version_id=pv.id,
        scope=CONSENT_SCOPE_LINE_PUSH,
        consented=True,
        consented_at=now_taipei_naive(),
    )
    test_db_session.add(log)
    test_db_session.commit()

    result = _check_line_push_consent("U_consent_true_001")
    assert result is True


def test_check_line_push_consent_db_error_returns_false():
    """session_scope 拋出例外 → fail-closed 回傳 False（不 re-raise）。"""
    with patch("models.base.session_scope") as mock_scope:
        mock_scope.side_effect = RuntimeError("DB connection lost")
        result = _check_line_push_consent("U_any_user_id")
    assert result is False


def test_check_line_push_consent_staff_exempt_returns_true(test_db_session):
    """員工（role != 'parent'）不受家長跨境 consent gate 約束 → 一律放行（True），
    即使 line_push_consent=False。

    理由：Spec E 的跨境同意僅針對「家長推播含學生（第三方未成年）PII」；員工 LINE
    通知傳的是員工本人工作資訊（請假/加班/薪資），非第三方 PII，且員工無 opt-in
    途徑（Spec E 僅建家長 LIFF opt-in UI）。員工被 gate 拍掉是實作副作用，非設計意圖。
    """
    from models.auth import User

    user = User(
        username="staff_no_consent",
        password_hash="hashed",
        role="teacher",
        line_user_id="U_staff_001",
        line_push_consent=False,
    )
    test_db_session.add(user)
    test_db_session.commit()

    result = _check_line_push_consent("U_staff_001")
    assert result is True


def test_check_line_push_consent_dual_role_parent_still_gated(test_db_session):
    """teacher-parent（role='parent' 但同時是員工，employee_id 非空）的家長身分推播
    含學生 PII，仍須受 gate（consent=False → False）。

    回歸防護：確保 discriminator 用 role（而非 employee_id）。若誤用 employee_id 判定，
    此 user 因 employee_id 非空會被當員工放行 → 在未同意下外洩學生 PII。
    """
    from models.auth import User

    user = User(
        username="teacher_parent_dual",
        password_hash="hashed",
        role="parent",
        employee_id=12345,
        line_user_id="U_dual_role_001",
        line_push_consent=False,
    )
    test_db_session.add(user)
    test_db_session.commit()

    result = _check_line_push_consent("U_dual_role_001")
    assert result is False


# ── Task 5：家長分支改查 ParentConsentLog（單一數據源）────────────────────────


def test_check_line_push_consent_parent_uses_consent_log_consented(test_db_session):
    """家長在 ParentConsentLog 有 consented=True → 回 True，
    即使 user.line_push_consent=False（證明數據源已從 User 欄位換成 ParentConsentLog）。
    """
    from models.auth import User
    from models.consent import ParentConsentLog, PolicyVersion, CONSENT_SCOPE_LINE_PUSH
    from utils.taipei_time import now_taipei_naive

    # seed PolicyVersion
    pv = PolicyVersion(
        version="2026.task5.t1",
        effective_at=now_taipei_naive(),
        document_path="/policies/task5-t1.pdf",
    )
    test_db_session.add(pv)
    test_db_session.flush()

    # seed 家長（line_push_consent=False 舊欄位故意設 False）
    user = User(
        username="parent_consent_log_true",
        password_hash="hashed",
        role="parent",
        line_user_id="U_consent_log_true_001",
        line_push_consent=False,
    )
    test_db_session.add(user)
    test_db_session.flush()

    # seed ParentConsentLog：最新一筆 consented=True
    log = ParentConsentLog(
        user_id=user.id,
        policy_version_id=pv.id,
        scope=CONSENT_SCOPE_LINE_PUSH,
        consented=True,
        consented_at=now_taipei_naive(),
    )
    test_db_session.add(log)
    test_db_session.commit()

    result = _check_line_push_consent("U_consent_log_true_001")
    assert result is True, (
        "家長 ParentConsentLog 最新一筆 consented=True，"
        "應回 True（即使 user.line_push_consent=False）"
    )


def test_check_line_push_consent_parent_uses_consent_log_revoked(test_db_session):
    """家長在 ParentConsentLog 最新一筆 consented=False（撤回）→ 回 False。
    即使 user.line_push_consent=True（舊欄位已退役）。
    """
    from models.auth import User
    from models.consent import ParentConsentLog, PolicyVersion, CONSENT_SCOPE_LINE_PUSH
    from utils.taipei_time import now_taipei_naive
    import time

    # seed PolicyVersion
    pv = PolicyVersion(
        version="2026.task5.t2",
        effective_at=now_taipei_naive(),
        document_path="/policies/task5-t2.pdf",
    )
    test_db_session.add(pv)
    test_db_session.flush()

    # seed 家長（line_push_consent=True 舊欄位故意設 True）
    user = User(
        username="parent_consent_log_revoked",
        password_hash="hashed",
        role="parent",
        line_user_id="U_consent_log_revoked_001",
        line_push_consent=True,
    )
    test_db_session.add(user)
    test_db_session.flush()

    t_base = now_taipei_naive()
    # 先同意
    log_grant = ParentConsentLog(
        user_id=user.id,
        policy_version_id=pv.id,
        scope=CONSENT_SCOPE_LINE_PUSH,
        consented=True,
        consented_at=t_base,
    )
    # 後撤回（consented_at 較晚 → 最新一筆 wins）
    from datetime import timedelta

    log_revoke = ParentConsentLog(
        user_id=user.id,
        policy_version_id=pv.id,
        scope=CONSENT_SCOPE_LINE_PUSH,
        consented=False,
        consented_at=t_base + timedelta(seconds=1),
    )
    test_db_session.add(log_grant)
    test_db_session.add(log_revoke)
    test_db_session.commit()

    result = _check_line_push_consent("U_consent_log_revoked_001")
    assert result is False, (
        "家長 ParentConsentLog 最新一筆 consented=False（撤回），"
        "應回 False（即使 user.line_push_consent=True）"
    )


# ── build_*_message 去識別化 ──────────────────────────────────────────────────


def test_build_activity_waitlist_promoted_message_no_student_name():
    """build_activity_waitlist_promoted_message 不再 inline student_name。

    原簽章保留（backward-compat），但輸出訊息改用「您的孩子」，不含傳入的名字。
    """
    msg = build_activity_waitlist_promoted_message(
        student_name="王小明",
        course_name="兒童鋼琴班",
        deadline=datetime(2026, 6, 1, 12, 0),
    )
    assert "王小明" not in msg, "student_name 不應出現在訊息中（去識別化）"
    assert "您的孩子" in msg
    assert "兒童鋼琴班" in msg


def test_build_dismissal_message_no_classroom_name():
    """build_dismissal_message 不再 inline student_name / classroom_name。

    原簽章保留（backward-compat），但輸出改用「您的孩子已可接送」，不含名字或班級。
    """
    msg = build_dismissal_message(
        student_name="陳小花",
        classroom_name="大班A",
        note="請走正門",
    )
    assert "陳小花" not in msg, "student_name 不應出現在訊息中（去識別化）"
    assert "大班A" not in msg, "classroom_name 不應出現在訊息中（去識別化）"
    assert "您的孩子" in msg
    assert "請走正門" in msg  # note 是非 PII，應保留
