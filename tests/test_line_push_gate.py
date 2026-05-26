"""DEPRECATED: 原本測 LineService.should_push_to_parent gate 與
notify_parent_message_received 行為。Phase 4 Section 4 (2026-05-26) 落地後
should_push_to_parent 與 _notify_* 全部從 line_service.py 移除：

- LINE 推送 gate (active + line_user_id + line_follow_confirmed_at) →
  dispatch._resolve_line_user_id（測試於 tests/notification/
  test_resolve_line_user_id.py，6 case）
- 通知偏好 gate (NotificationPreference) → dispatch._pref_enabled
  （測試於 tests/notification/test_dispatch_fan_out.py）
- Per-event LINE 推送 → dispatch._channels.line.LINE_HANDLERS
  （測試於 tests/notification/test_channels_line.py）

本檔保留為 phase-4 退役歷史 placeholder，下個 minor version 可移除。
"""


def test_should_push_to_parent_retired_in_phase4_section4():
    """Sanity check: should_push_to_parent 已從 LineService class 移除。"""
    from services.line_service import LineService

    assert not hasattr(LineService, "should_push_to_parent"), (
        "should_push_to_parent 應已退役 (Phase 4 Section 4)；"
        "gate 邏輯由 dispatch._resolve_line_user_id + dispatch._pref_enabled 取代。"
    )


def test_notify_parent_message_received_retired_in_phase4_section1():
    """Sanity check: _notify_parent_message_received 已退役。"""
    from services.line_service import LineService

    assert not hasattr(LineService, "_notify_parent_message_received"), (
        "_notify_* method 應已退役 (Phase 4 Section 4)；"
        "推送邏輯由 LINE_HANDLERS['parent.message_received'] handler 取代。"
    )
