"""dispatch.enqueue 入口契約測試。"""

import pytest
from services.notification import dispatch


def test_enqueue_unknown_event_type_raises(test_db_session):
    with pytest.raises(ValueError, match="未知 event_type"):
        dispatch.enqueue(
            test_db_session,
            event_type="bogus.event",
            recipient_user_id=1,
            context={},
        )


def test_enqueue_stores_pending_event_on_session_info(test_db_session):
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
    queue = test_db_session.info[dispatch._QUEUE_KEY]
    assert len(queue) == 1
    evt = queue[0]
    assert evt.event_type == "leave.approved"
    assert evt.recipient_user_id == 42
    assert evt.channels == ("in_app", "line")


def test_enqueue_copies_context_dict(test_db_session):
    """context 應被拷貝，caller 後續改 dict 不影響 pending event。"""
    ctx = {"a": 1}
    dispatch.enqueue(
        test_db_session,
        event_type="leave.approved",
        recipient_user_id=1,
        context=ctx,
    )
    ctx["a"] = 999
    queue = test_db_session.info[dispatch._QUEUE_KEY]
    assert queue[0].context == {"a": 1}


def test_enqueue_with_channels_override(test_db_session):
    dispatch.enqueue(
        test_db_session,
        event_type="leave.approved",
        recipient_user_id=1,
        context={},
        channels_override=("in_app",),
    )
    queue = test_db_session.info[dispatch._QUEUE_KEY]
    assert queue[0].channels == ("in_app",)


def test_enqueue_appends_multiple_events(test_db_session):
    for i in range(3):
        dispatch.enqueue(
            test_db_session,
            event_type="leave.approved",
            recipient_user_id=i,
            context={},
        )
    queue = test_db_session.info[dispatch._QUEUE_KEY]
    assert len(queue) == 3
    assert [e.recipient_user_id for e in queue] == [0, 1, 2]


def test_enqueue_includes_source_entity_fields(test_db_session):
    dispatch.enqueue(
        test_db_session,
        event_type="leave.approved",
        recipient_user_id=1,
        context={},
        sender_id=7,
        source_entity_type="leave_request",
        source_entity_id=99,
    )
    evt = test_db_session.info[dispatch._QUEUE_KEY][0]
    assert evt.sender_id == 7
    assert evt.source_entity_type == "leave_request"
    assert evt.source_entity_id == 99
