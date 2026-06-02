"""assert_all_scope helper 行為 + 全園彙總端點鎖 :all 的回歸測試。

搭配 test_scope_filter_guard.py（守衛靜態確保端點有套 filter）形成雙保險：
  - 守衛：端點「有呼叫」scope helper。
  - 本檔：helper「行為正確」（:all 通過 / :own_class 擋）+ 關鍵端點未來不被拿掉守衛。
"""

import inspect

import pytest
from fastapi import HTTPException

from utils.portfolio_access import assert_all_scope

# --------------------------------------------------------------------------
# assert_all_scope 純函式行為
# --------------------------------------------------------------------------


def test_wildcard_admin_passes():
    admin = {"role": "admin", "permission_names": ["*"]}
    assert_all_scope(admin, "STUDENTS_READ")  # 不 raise


def test_bare_permission_is_all_passes():
    """bare STUDENTS_READ 等價 :all（resolve_grant 規則）→ 通過，零行為變更。"""
    user = {"role": "hr", "permission_names": ["STUDENTS_READ"]}
    assert_all_scope(user, "STUDENTS_READ")  # 不 raise


def test_own_class_scope_is_blocked():
    """持 STUDENTS_READ:own_class 的自訂角色 → 403（堵全園 fail-open）。"""
    user = {"role": "teacher", "permission_names": ["STUDENTS_READ:own_class"]}
    with pytest.raises(HTTPException) as exc:
        assert_all_scope(user, "STUDENTS_READ")
    assert exc.value.status_code == 403


def test_missing_permission_is_blocked():
    user = {"role": "teacher", "permission_names": ["DASHBOARD"]}
    with pytest.raises(HTTPException) as exc:
        assert_all_scope(user, "STUDENTS_READ")
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# 關鍵端點守衛存在性（防止未來重構靜默移除 scope 守衛）
# --------------------------------------------------------------------------


def test_export_students_locks_all_scope():
    import api.exports as m

    assert "assert_all_scope" in inspect.getsource(m.export_students)


def test_enrollment_endpoints_lock_all_scope():
    import api.student_enrollment as m

    for fn in (
        m.get_enrollment_stats,
        m.get_enrollment_roster,
        m.get_enrollment_options,
    ):
        assert "assert_all_scope" in inspect.getsource(fn)


def test_approvals_attendance_summary_locks_all_scope():
    import api.approvals as m

    assert "assert_all_scope" in inspect.getsource(m.get_student_attendance_summary)


def test_attendance_overview_locks_all_scope():
    import api.student_attendance as m

    assert "assert_all_scope" in inspect.getsource(m.get_daily_attendance_overview)


def test_dismissal_admin_endpoints_lock_all_scope():
    import api.dismissal_calls as m

    for fn in (
        m.create_dismissal_call,
        m.list_dismissal_calls,
        m.cancel_dismissal_call,
    ):
        assert "assert_all_scope" in inspect.getsource(fn)


def test_student_detail_endpoints_assert_per_row_access():
    import api.students as m

    assert "assert_student_access" in inspect.getsource(m.get_student_profile)
    assert "assert_student_access" in inspect.getsource(
        m.get_student_lifecycle_overview
    )
