"""高風險稽核事件主動 LINE 告警：_maybe_alert_high_risk_audit + per-risk_kind cooldown
+ _write_audit_sync 寫入後掛勾。

原本高風險事件（硬刪 / 提權-角色變更 / 越權嘗試）只靠前端每 60s 輪詢紅點（分頁隱藏還跳過），
下班/假日無人知曉。稽核寫入成功後對高風險事件主動推 LINE（best-effort、不阻擋寫入）。
"""

from unittest.mock import patch

import pytest

import utils.audit as audit_mod


@pytest.fixture(autouse=True)
def _reset_throttle():
    audit_mod._reset_high_risk_alert_throttle()
    yield
    audit_mod._reset_high_risk_alert_throttle()


def _payload(action, summary, entity_type, username="admin"):
    return {
        "action": action,
        "summary": summary,
        "entity_type": entity_type,
        "username": username,
    }


def test_alerts_on_high_risk_hard_delete():
    with patch("services.ops_alert.notify_high_risk_audit") as notify:
        audit_mod._maybe_alert_high_risk_audit(
            _payload("DELETE", "刪除管理員 (不可復原)", "user")
        )
    notify.assert_called_once()
    kw = notify.call_args.kwargs
    assert kw["risk_kind"] == "hard_delete"
    assert kw["action"] == "DELETE"
    assert kw["username"] == "admin"


def test_no_alert_on_normal_event():
    with patch("services.ops_alert.notify_high_risk_audit") as notify:
        audit_mod._maybe_alert_high_risk_audit(
            _payload("UPDATE", "修改員工資料", "employee")
        )
    notify.assert_not_called()


def test_throttle_suppresses_same_kind_within_cooldown():
    with patch("services.ops_alert.notify_high_risk_audit") as notify:
        audit_mod._maybe_alert_high_risk_audit(
            _payload("DELETE", "a (不可復原)", "user")
        )
        audit_mod._maybe_alert_high_risk_audit(
            _payload("DELETE", "b (不可復原)", "user")
        )
    assert notify.call_count == 1  # 第二筆同 risk_kind 被 cooldown 抑制


def test_different_kinds_each_alert():
    with patch("services.ops_alert.notify_high_risk_audit") as notify:
        audit_mod._maybe_alert_high_risk_audit(
            _payload("DELETE", "x (不可復原)", "user")
        )
        audit_mod._maybe_alert_high_risk_audit(
            _payload("BLOCKED_DELETE", "拒絕", "user")
        )
    assert notify.call_count == 2  # hard_delete + blocked 各自 cooldown


def test_cooldown_resets_allow_realert():
    with patch("services.ops_alert.notify_high_risk_audit") as notify:
        audit_mod._maybe_alert_high_risk_audit(
            _payload("DELETE", "x (不可復原)", "user")
        )
        audit_mod._reset_high_risk_alert_throttle()
        audit_mod._maybe_alert_high_risk_audit(
            _payload("DELETE", "y (不可復原)", "user")
        )
    assert notify.call_count == 2


def test_hook_swallows_notify_errors():
    with patch(
        "services.ops_alert.notify_high_risk_audit", side_effect=RuntimeError("boom")
    ):
        # 不應拋出（best-effort，不可影響稽核寫入）
        audit_mod._maybe_alert_high_risk_audit(
            _payload("DELETE", "x (不可復原)", "user")
        )


def test_write_audit_sync_triggers_alert_after_commit(test_db_session):
    """整合：_write_audit_sync 寫入高風險 payload 成功 commit 後呼叫告警。"""
    with patch("services.ops_alert.notify_high_risk_audit") as notify:
        audit_mod._write_audit_sync(
            {
                "action": "DELETE",
                "entity_type": "user",
                "summary": "刪除管理員 admin (不可復原)",
                "username": "admin",
            }
        )
    notify.assert_called_once()


def test_write_audit_sync_no_alert_for_normal(test_db_session):
    with patch("services.ops_alert.notify_high_risk_audit") as notify:
        audit_mod._write_audit_sync(
            {
                "action": "UPDATE",
                "entity_type": "employee",
                "summary": "修改員工資料",
                "username": "hr",
            }
        )
    notify.assert_not_called()
