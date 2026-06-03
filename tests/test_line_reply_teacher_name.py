"""line_reply_router 對家長顯示教師名稱不可外洩 username（稽核 2026-06-03 P2-d）。

handle_parent_postback 與 _list_unread_threads_for_parent 對家長顯示 teacher 名稱時
直接用 User.username（內部登入帳號 emp_xxx 形式）。messages.py _thread_summary_from_maps
明文規定不外洩 username：以 Employee.name 優先，退而求其次 User.display_name。
本測試鎖定共用 helper _resolve_teacher_display_name 的同套 precedence。
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.line_reply_router import _resolve_teacher_display_name


def _teacher(*, employee_id=None, display_name=None, username="emp_999"):
    t = MagicMock()
    t.employee_id = employee_id
    t.display_name = display_name
    t.username = username
    return t


def test_prefers_employee_name():
    """有 Employee 時用其正式姓名，非 username。"""
    teacher = _teacher(employee_id=5, username="emp_001")
    fake_emp = MagicMock()
    fake_emp.name = "王老師"
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = fake_emp
    assert _resolve_teacher_display_name(session, teacher) == "王老師"


def test_falls_back_to_display_name():
    """無 Employee 但有 display_name 時用 display_name。"""
    teacher = _teacher(employee_id=None, display_name="李老師", username="emp_002")
    session = MagicMock()
    assert _resolve_teacher_display_name(session, teacher) == "李老師"


def test_never_leaks_username():
    """無 Employee 且無 display_name 時退回『老師』，絕不外洩 username。"""
    teacher = _teacher(employee_id=None, display_name=None, username="emp_003")
    session = MagicMock()
    result = _resolve_teacher_display_name(session, teacher)
    assert result == "老師"
    assert "emp_003" not in result


def test_none_teacher_returns_default():
    assert _resolve_teacher_display_name(MagicMock(), None) == "老師"


def test_employee_without_name_falls_back():
    """Employee 存在但 name 為空時，不可掉回 username。"""
    teacher = _teacher(employee_id=7, display_name=None, username="emp_004")
    fake_emp = MagicMock()
    fake_emp.name = None
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = fake_emp
    result = _resolve_teacher_display_name(session, teacher)
    assert result == "老師"
    assert "emp_004" not in result
