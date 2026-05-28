"""Phase 1 整合 smoke：完整 enqueue → commit → fan-out → log row。

這套測試與 Task 11 (test_dispatch_fan_out) 的主要差異是：
- Task 11 直接呼叫 _fan_out()（單元測試）
- 本檔測試 enqueue() + commit()（整合，觸發 after_commit hook）
"""

import pytest
from unittest.mock import patch, MagicMock

from services.notification import dispatch
from models.database import NotificationLog


def test_full_lifecycle_employee_event_writes_log_row_and_calls_adapters(
    test_db_session,
):
    """員工域：enqueue → commit → 應寫 log row + line/ws adapter 被呼叫。"""
    from datetime import datetime

    from models.database import User

    # _fan_out 的 _resolve_line_user_id 需要有 line_user_id + follow confirmed 的 active user
    user = User(
        username="emp_u42",
        password_hash="x",
        line_user_id="Uemp42",
        line_follow_confirmed_at=datetime.now(),
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    recipient_id = user.id

    with (
        patch("services.notification.dispatch._inbox_ws_push") as mock_ipush,
        patch("services.notification.dispatch._get_line_adapter") as mock_get_la,
    ):
        mock_la = MagicMock()
        mock_get_la.return_value = mock_la

        dispatch.enqueue(
            test_db_session,
            event_type="leave.approved",
            recipient_user_id=recipient_id,
            context={
                "reviewer_name": "張主任",
                "leave_type": "事假",
                "start": "2026-06-01",
                "end": "2026-06-02",
                "leave_id": 1,
            },
            sender_id=7,
            source_entity_type="leave_request",
            source_entity_id=1,
        )
        test_db_session.commit()

    rows = test_db_session.query(NotificationLog).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.recipient_user_id == recipient_id
    assert row.sender_id == 7
    assert row.event_type == "leave.approved"
    assert row.source_entity_id == 1
    assert "張主任" in row.title
    assert "in_app" in row.channels_succeeded

    mock_la.send.assert_called_once()
    mock_ipush.assert_called_once()


def test_full_lifecycle_parent_event_no_log_row_still_calls_adapters(test_db_session):
    """家長域：無 in_app channel → 不寫 log row，但 adapter 仍呼叫。"""
    from datetime import datetime

    from models.database import User

    # _resolve_line_user_id 需 active user with line_user_id + follow confirmed
    user = User(
        username="parent_u99",
        password_hash="x",
        line_user_id="Uparen99",
        line_follow_confirmed_at=datetime.now(),
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    recipient_id = user.id

    with (
        patch("services.notification.dispatch._get_line_adapter") as mock_get_la,
        patch("services.notification.dispatch._get_ws_adapter") as mock_get_ws,
    ):
        mock_la = MagicMock()
        mock_ws = MagicMock()
        mock_get_la.return_value = mock_la
        mock_get_ws.return_value = mock_ws

        dispatch.enqueue(
            test_db_session,
            event_type="parent.message_received",
            recipient_user_id=recipient_id,
            context={
                "teacher_name": "王老師",
                "student_name": "小明",
                "body_preview": "今天很乖",
                "thread_id": 7,
            },
        )
        test_db_session.commit()

    rows = test_db_session.query(NotificationLog).all()
    # Phase 2: log row IS written now for LINE/WS events (is_inbox_visible=False for parent events)
    assert len(rows) == 1, "Phase 2: LINE/WS 事件也寫 log row"
    assert rows[0].is_inbox_visible is False, "家長域事件 is_inbox_visible=False"

    mock_la.send.assert_called_once()  # LINE
    mock_ws.send.assert_called_once()  # parent WS


def test_full_lifecycle_rollback_does_not_send(test_db_session):
    """rollback 後 queue 清空，無 adapter 呼叫、無 log row。"""
    with patch("services.notification.dispatch._get_line_adapter") as mock_get_la:
        mock_la = MagicMock()
        mock_get_la.return_value = mock_la

        dispatch.enqueue(
            test_db_session,
            event_type="leave.approved",
            recipient_user_id=42,
            context={
                "reviewer_name": "X",
                "leave_type": "事假",
                "start": "2026-06-01",
                "end": "2026-06-02",
                "leave_id": 1,
            },
        )
        test_db_session.rollback()

    mock_la.send.assert_not_called()
    rows = test_db_session.query(NotificationLog).all()
    assert rows == []
