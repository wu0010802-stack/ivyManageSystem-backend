"""Phase 2 P1 resilience：retry scheduler tick unit + integration test."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
import pytest


class TestTickLineRetry:
    def test_picks_pending_row_and_succeeds(self, test_db_session, monkeypatch):
        from services.notification.retry_scheduler import tick_line_retry
        from models.database import NotificationLog, User

        user = User(
            username="p",
            password_hash="x",
            line_user_id="U1",
            is_active=True,
            line_follow_confirmed_at=datetime.now(),
        )
        test_db_session.add(user)
        test_db_session.commit()

        row = NotificationLog(
            recipient_user_id=user.id,
            event_type="parent.fee_due",
            title="t",
            body="b",
            payload_json={
                "student_name": "X",
                "item_name": "I",
                "amount": 100,
                "due_date": "2026-06-01",
            },
            channels_attempted=["line"],
            channels_succeeded=[],
            channels_failed=[{"channel": "line", "error": "X"}],
            line_next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            line_retry_count=0,
            is_inbox_visible=False,
        )
        test_db_session.add(row)
        test_db_session.commit()

        monkeypatch.setattr(
            "services.notification.retry_scheduler._get_line_adapter",
            lambda: MagicMock(send=MagicMock(return_value=None)),
        )

        result = tick_line_retry()
        test_db_session.refresh(row)
        assert result["attempted"] == 1
        assert result["succeeded"] == 1
        assert row.line_next_retry_at is None
        assert "line(retry)" in row.channels_succeeded

    def test_third_failure_marks_final(self, test_db_session, monkeypatch):
        from services.notification.retry_scheduler import tick_line_retry
        from models.database import NotificationLog, User

        user = User(
            username="p2",
            password_hash="x",
            line_user_id="U2",
            is_active=True,
            line_follow_confirmed_at=datetime.now(),
        )
        test_db_session.add(user)
        test_db_session.commit()

        row = NotificationLog(
            recipient_user_id=user.id,
            event_type="parent.fee_due",
            title="t",
            body="b",
            payload_json={
                "student_name": "X",
                "item_name": "I",
                "amount": 1,
                "due_date": "2026-06-01",
            },
            channels_attempted=["line"],
            channels_succeeded=[],
            channels_failed=[{"channel": "line", "error": "X"}],
            line_next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            line_retry_count=2,  # 已試 2 次（首發+1 tick）；本 tick 後達 3
            is_inbox_visible=False,
        )
        test_db_session.add(row)
        test_db_session.commit()

        # adapter 仍失敗
        class Boom:
            def send(self, *a, **k):
                raise ConnectionError("still down")

        monkeypatch.setattr(
            "services.notification.retry_scheduler._get_line_adapter", lambda: Boom()
        )

        result = tick_line_retry()
        test_db_session.refresh(row)
        assert result["final_failed"] == 1
        assert row.line_retry_count == 3
        assert row.line_next_retry_at is None
        assert any(f.get("final") is True for f in row.channels_failed)

    def test_unreachable_user_marks_final_immediately(
        self, test_db_session, monkeypatch
    ):
        """user inactive / no line_user_id → mark final，不再 retry."""
        from services.notification.retry_scheduler import tick_line_retry
        from models.database import NotificationLog, User

        user = User(username="p3", password_hash="x", is_active=False)  # inactive
        test_db_session.add(user)
        test_db_session.commit()

        row = NotificationLog(
            recipient_user_id=user.id,
            event_type="parent.fee_due",
            title="t",
            body="b",
            payload_json={
                "student_name": "X",
                "item_name": "I",
                "amount": 1,
                "due_date": "2026-06-01",
            },
            channels_attempted=["line"],
            channels_succeeded=[],
            channels_failed=[{"channel": "line", "error": "X"}],
            line_next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            line_retry_count=0,
            is_inbox_visible=False,
        )
        test_db_session.add(row)
        test_db_session.commit()

        result = tick_line_retry()
        test_db_session.refresh(row)
        # _retry_line_push 偵測 user unreachable → mark final
        assert row.line_retry_count == 3
        assert row.line_next_retry_at is None
