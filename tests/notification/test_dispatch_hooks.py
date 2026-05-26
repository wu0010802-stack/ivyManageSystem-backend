"""after_commit / after_rollback hook 行為測試。

注意：test_db_session fixture 必須在 swap factory 後 reinstall hooks（Task 7 conftest 改動），
否則 hook 綁在 production factory，test factory commit 不觸發。
"""

import pytest
from unittest.mock import patch

from services.notification import dispatch


def test_after_commit_drains_queue_and_calls_fan_out(test_db_session):
    with patch.object(dispatch, "_fan_out") as mock_fan_out:
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
        assert dispatch._QUEUE_KEY in test_db_session.info
        test_db_session.commit()

    # commit 後 queue 應被 pop
    assert dispatch._QUEUE_KEY not in test_db_session.info
    assert mock_fan_out.call_count == 1


def test_after_rollback_clears_queue_without_fan_out(test_db_session):
    with patch.object(dispatch, "_fan_out") as mock_fan_out:
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

    assert dispatch._QUEUE_KEY not in test_db_session.info
    assert mock_fan_out.call_count == 0


def test_after_commit_with_empty_queue_is_no_op(test_db_session):
    """commit 但沒 enqueue 不應炸。"""
    with patch.object(dispatch, "_fan_out") as mock_fan_out:
        test_db_session.commit()
    mock_fan_out.assert_not_called()


def test_after_commit_one_fan_out_failure_does_not_block_others(test_db_session):
    """一筆 _fan_out 拋例外，後面的還是會被 call。"""
    call_count = [0]

    def fake_fan_out(evt):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("first fails")

    with patch.object(dispatch, "_fan_out", side_effect=fake_fan_out):
        for i in range(3):
            dispatch.enqueue(
                test_db_session,
                event_type="leave.approved",
                recipient_user_id=i,
                context={
                    "reviewer_name": "X",
                    "leave_type": "事假",
                    "start": "2026-06-01",
                    "end": "2026-06-02",
                    "leave_id": 1,
                },
            )
        # commit 不應 re-raise
        test_db_session.commit()

    assert call_count[0] == 3


def test_install_session_hooks_idempotent():
    """重複呼叫 install 不應綁多次 hook。"""
    from models.base import get_session_factory

    factory = get_session_factory()
    dispatch.install_session_hooks(factory)
    dispatch.install_session_hooks(factory)
    assert factory in dispatch._HOOKS_INSTALLED
