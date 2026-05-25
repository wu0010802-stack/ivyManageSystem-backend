"""dispatch._fan_out 整合測試：log row + channels_* + adapter 呼叫順序。"""

import pytest
from unittest.mock import patch, MagicMock

from services.notification import dispatch
from services.notification.dispatch import PendingEvent


def _pevt(event_type, channels, **kwargs):
    return PendingEvent(
        event_type=event_type,
        recipient_user_id=kwargs.get("recipient_user_id", 42),
        context=kwargs.get(
            "context",
            {
                "reviewer_name": "X",
                "leave_type": "事假",
                "start": "2026-06-01",
                "end": "2026-06-02",
                "leave_id": 1,
            },
        ),
        sender_id=kwargs.get("sender_id"),
        source_entity_type=kwargs.get("source_entity_type"),
        source_entity_id=kwargs.get("source_entity_id"),
        channels=channels,
    )


def test_fan_out_writes_log_row_with_rendered_fields(test_db_session):
    from models.database import NotificationLog

    with (
        patch("services.notification.dispatch._inbox_ws_push"),
        patch("services.notification.dispatch._get_line_adapter") as mock_get_la,
    ):
        mock_la = MagicMock()
        mock_get_la.return_value = mock_la
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))

    rows = test_db_session.query(NotificationLog).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "leave.approved"
    assert row.recipient_user_id == 42
    assert "X" in row.title
    assert row.deep_link == "/portal/leaves/1"
    assert "in_app" in row.channels_attempted
    assert "in_app" in row.channels_succeeded


def test_fan_out_in_app_path_calls_inbox_ws_push(test_db_session):
    with (
        patch("services.notification.dispatch._inbox_ws_push") as mock_ipush,
        patch("services.notification.dispatch._get_line_adapter"),
    ):
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))
    mock_ipush.assert_called_once()


def test_fan_out_inbox_ws_push_failure_does_not_mark_in_app_failed(test_db_session):
    """inbox WS 失敗只 warning，in_app 仍算 succeeded（log row 已寫）。"""
    from models.database import NotificationLog

    with (
        patch(
            "services.notification.dispatch._inbox_ws_push",
            side_effect=RuntimeError("hub down"),
        ),
        patch("services.notification.dispatch._get_line_adapter"),
    ):
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))

    row = test_db_session.query(NotificationLog).first()
    assert "in_app" in row.channels_succeeded
    assert all(f.get("channel") != "in_app" for f in row.channels_failed)


def test_fan_out_line_adapter_failure_marks_channels_failed(test_db_session):
    from models.database import NotificationLog

    mock_la = MagicMock()
    mock_la.send.side_effect = RuntimeError("LINE 5xx")
    with (
        patch("services.notification.dispatch._inbox_ws_push"),
        patch("services.notification.dispatch._get_line_adapter", return_value=mock_la),
    ):
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))

    row = test_db_session.query(NotificationLog).first()
    assert any(f.get("channel") == "line" for f in row.channels_failed)
    assert "line" not in row.channels_succeeded


def test_fan_out_parent_event_skips_in_app(test_db_session):
    """家長域 v1 不寫 in_app（matrix 沒給 in_app）。"""
    from models.database import NotificationLog

    mock_ws = MagicMock()
    mock_la = MagicMock()
    with (
        patch("services.notification.dispatch._get_ws_adapter", return_value=mock_ws),
        patch("services.notification.dispatch._get_line_adapter", return_value=mock_la),
    ):
        dispatch._fan_out(
            _pevt(
                "parent.announcement",
                ("line",),
                context={"title": "x", "preview": "y", "announcement_id": 1},
            )
        )

    rows = test_db_session.query(NotificationLog).all()
    assert rows == []
    mock_la.send.assert_called_once()


def test_fan_out_preference_disabled_skips_line(test_db_session):
    """user 關閉 line preference 應跳過 LINE adapter（in_app 仍寫）。"""
    from models.database import NotificationPreference

    test_db_session.add(
        NotificationPreference(
            user_id=42,
            event_type="leave.approved",
            channel="line",
            enabled=False,
        )
    )
    test_db_session.commit()

    mock_la = MagicMock()
    with (
        patch("services.notification.dispatch._inbox_ws_push"),
        patch("services.notification.dispatch._get_line_adapter", return_value=mock_la),
    ):
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))

    mock_la.send.assert_not_called()
    from models.database import NotificationLog

    row = test_db_session.query(NotificationLog).first()
    assert "in_app" in row.channels_succeeded
    assert "line" not in row.channels_succeeded
    assert "line" not in row.channels_attempted
