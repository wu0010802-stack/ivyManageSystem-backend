"""Unit test：attendance.remark → source_leave_id 反查 helper。

驗證 _extract_source_leave_id 對家長申請前綴 `家長申請#<id>` 的解析正確性。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.student_attendance import _extract_source_leave_id


def test_remark_with_parent_leave_prefix_returns_id():
    assert _extract_source_leave_id("家長申請#42") == 42


def test_remark_with_prefix_and_whitespace_strips_correctly():
    assert _extract_source_leave_id("  家長申請#7  ") == 7


def test_remark_without_prefix_returns_none():
    assert _extract_source_leave_id("一般備註") is None


def test_remark_with_non_numeric_suffix_returns_none():
    assert _extract_source_leave_id("家長申請#abc") is None


def test_empty_remark_returns_none():
    assert _extract_source_leave_id("") is None
    assert _extract_source_leave_id(None) is None
