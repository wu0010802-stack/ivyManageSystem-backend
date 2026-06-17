"""ops_alert.notify_slow_request_burst 行為測試。

驗證：line_group_id 缺 / LineService 未注入 / push 異常 三種失敗路徑
皆 no-op + log，不 propagate exception。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from config import settings
from services import ops_alert


@pytest.fixture(autouse=True)
def _reset_ops_alert():
    ops_alert.reset_for_tests()
    yield
    ops_alert.reset_for_tests()


@pytest.fixture
def _restore_group_id():
    original = settings.ops_alert.line_group_id
    yield
    settings.ops_alert.line_group_id = original


def test_no_group_id_skips_silently(caplog, _restore_group_id):
    """OPS_ALERT_LINE_GROUP_ID 缺 → 不 push，只 log warning。"""
    settings.ops_alert.line_group_id = None
    ops_alert.init_ops_alert_service(MagicMock())

    with caplog.at_level("WARNING"):
        ops_alert.notify_slow_request_burst(
            path="/api/foo",
            count=10,
            window_seconds=60,
            sample_elapsed_ms=3000.0,
            sample_status=200,
        )
    assert any("OPS_ALERT_LINE_GROUP_ID 未設" in r.message for r in caplog.records)


def test_line_service_not_injected_skips(caplog, _restore_group_id):
    """LineService 未注入 → log warning，不炸。"""
    settings.ops_alert.line_group_id = "Cabcdef"
    # 不呼叫 init_ops_alert_service
    with caplog.at_level("WARNING"):
        ops_alert.notify_slow_request_burst(
            path="/api/foo",
            count=10,
            window_seconds=60,
            sample_elapsed_ms=3000.0,
            sample_status=200,
        )
    assert any("LineService 未注入" in r.message for r in caplog.records)


def test_push_exception_is_swallowed(caplog, _restore_group_id):
    """LineService.push_text_to_group 拋 exception → log error，不 propagate。"""
    settings.ops_alert.line_group_id = "Cabcdef"
    bad_line = MagicMock()
    bad_line.push_text_to_group.side_effect = RuntimeError("LINE API down")
    ops_alert.init_ops_alert_service(bad_line)

    with caplog.at_level("ERROR"):
        ops_alert.notify_slow_request_burst(
            path="/api/foo",
            count=10,
            window_seconds=60,
            sample_elapsed_ms=3000.0,
            sample_status=200,
        )
    assert any("Slow request alert push 失敗" in r.message for r in caplog.records)
    bad_line.push_text_to_group.assert_called_once()


def test_happy_path_pushes_to_group(_restore_group_id):
    """成功路徑：line_group_id 設了 + LineService 注入了 → push 一次，內容含關鍵欄位。"""
    settings.ops_alert.line_group_id = "Cabcdef"
    line = MagicMock()
    ops_alert.init_ops_alert_service(line)

    ops_alert.notify_slow_request_burst(
        path="/api/students/42",
        count=15,
        window_seconds=60,
        sample_elapsed_ms=3500.0,
        sample_status=200,
    )

    line.push_text_to_group.assert_called_once()
    args = line.push_text_to_group.call_args
    assert args[0][0] == "Cabcdef"
    text = args[0][1]
    assert "/api/students/42" in text
    assert "15 次" in text
    assert "3500ms" in text


# ============== notify_high_risk_audit ==============


def test_high_risk_no_group_id_skips(caplog, _restore_group_id):
    settings.ops_alert.line_group_id = None
    ops_alert.init_ops_alert_service(MagicMock())
    with caplog.at_level("WARNING"):
        ops_alert.notify_high_risk_audit(
            risk_kind="hard_delete",
            action="DELETE",
            entity_type="user",
            summary="刪除管理員 (不可復原)",
        )
    assert any("OPS_ALERT_LINE_GROUP_ID 未設" in r.message for r in caplog.records)


def test_high_risk_service_not_injected_skips(caplog, _restore_group_id):
    settings.ops_alert.line_group_id = "Cabcdef"
    with caplog.at_level("WARNING"):
        ops_alert.notify_high_risk_audit(
            risk_kind="permission_change",
            action="UPDATE",
            entity_type="user",
            summary="提權",
        )
    assert any("LineService 未注入" in r.message for r in caplog.records)


def test_high_risk_push_exception_swallowed(caplog, _restore_group_id):
    settings.ops_alert.line_group_id = "Cabcdef"
    bad = MagicMock()
    bad.push_text_to_group.side_effect = RuntimeError("LINE down")
    ops_alert.init_ops_alert_service(bad)
    with caplog.at_level("ERROR"):
        ops_alert.notify_high_risk_audit(
            risk_kind="blocked",
            action="BLOCKED_DELETE",
            entity_type="employee",
            summary="拒絕",
        )
    assert any("High-risk audit alert push 失敗" in r.message for r in caplog.records)
    bad.push_text_to_group.assert_called_once()


def test_high_risk_happy_path_pushes(_restore_group_id):
    settings.ops_alert.line_group_id = "Cabcdef"
    line = MagicMock()
    ops_alert.init_ops_alert_service(line)
    ops_alert.notify_high_risk_audit(
        risk_kind="hard_delete",
        action="DELETE",
        entity_type="user",
        summary="刪除管理員 admin (不可復原)",
        username="boss",
    )
    line.push_text_to_group.assert_called_once()
    args = line.push_text_to_group.call_args
    assert args[0][0] == "Cabcdef"
    text = args[0][1]
    assert "硬刪除" in text  # risk_kind label
    assert "DELETE / user" in text
    assert "boss" in text
