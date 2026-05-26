"""tests/test_dr_storage_sync.py

Test pure functions in dr_storage_sync: _decide_action, _r2_key.
Note: Mocks boto3 before importing the module since boto3 is not in dev dependencies.
"""

import sys
from unittest.mock import MagicMock

# Mock boto3 modules before any import of dr_storage_sync
if "boto3" not in sys.modules:
    sys.modules["boto3"] = MagicMock()
    sys.modules["boto3.s3"] = MagicMock()
    sys.modules["boto3.s3.transfer"] = MagicMock()

import pytest


def test_decide_action_new_file():
    """New file should be uploaded."""
    from scripts.dr_storage_sync import _decide_action

    src = {"name": "a.pdf", "updated_at": "2026-05-20T10:00:00Z", "size": 100}
    assert _decide_action(src, None) == "upload"


def test_decide_action_target_up_to_date():
    """Target with matching timestamp should be skipped."""
    from scripts.dr_storage_sync import _decide_action

    src = {"name": "a.pdf", "updated_at": "2026-05-20T10:00:00Z", "size": 100}
    dst = {
        "user_metadata": {"x-source-updated-at": "2026-05-20T10:00:00Z"},
        "size": 100,
    }
    assert _decide_action(src, dst) == "skip"


def test_decide_action_source_newer():
    """Source newer than target should be uploaded."""
    from scripts.dr_storage_sync import _decide_action

    src = {"name": "a.pdf", "updated_at": "2026-05-21T10:00:00Z", "size": 100}
    dst = {
        "user_metadata": {"x-source-updated-at": "2026-05-20T10:00:00Z"},
        "size": 100,
    }
    assert _decide_action(src, dst) == "upload"


def test_decide_action_target_newer_still_skip():
    """Target newer than source should be skipped (safe default)."""
    from scripts.dr_storage_sync import _decide_action

    src = {"name": "a.pdf", "updated_at": "2026-05-19T10:00:00Z", "size": 100}
    dst = {
        "user_metadata": {"x-source-updated-at": "2026-05-20T10:00:00Z"},
        "size": 100,
    }
    assert _decide_action(src, dst) == "skip"


def test_r2_key_layout():
    """R2 key should follow target_prefix / src_bucket / src_name layout."""
    from scripts.dr_storage_sync import _r2_key

    assert (
        _r2_key("storage/", "leave-attachments", "2026/01/abc.pdf")
        == "storage/leave-attachments/2026/01/abc.pdf"
    )


def test_r2_key_trailing_slash_ignored():
    """Trailing slash in prefix should be stripped."""
    from scripts.dr_storage_sync import _r2_key

    assert (
        _r2_key("storage", "growth-reports", "students/1/42.pdf")
        == "storage/growth-reports/students/1/42.pdf"
    )
