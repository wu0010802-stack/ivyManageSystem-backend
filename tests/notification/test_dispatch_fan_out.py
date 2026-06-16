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
    """家長域 v1 不寫 in_app inbox（matrix 沒給 in_app）。

    Phase 2 更新：log row 仍寫（is_inbox_visible=False）供 retry audit；
    員工 inbox 不顯示此 row（is_inbox_visible=False）。
    """
    from datetime import datetime

    from models.database import NotificationLog, User

    # _fan_out 會呼叫 _resolve_line_user_id，需要有 User row 才能 resolve 成功
    user = User(
        username="parent_u42",
        password_hash="x",
        line_user_id="Uparen42",
        line_follow_confirmed_at=datetime.now(),
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()  # commit so log_session (new session in _fan_out) can see the row
    recipient_id = user.id

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
                recipient_user_id=recipient_id,
                context={"title": "x", "preview": "y", "announcement_id": 1},
            )
        )

    # Phase 2: log row IS written now (for retry audit), but is_inbox_visible=False
    rows = test_db_session.query(NotificationLog).all()
    assert len(rows) == 1, "Phase 2: LINE-only 事件也寫 log row"
    assert rows[0].is_inbox_visible is False, "LINE-only 事件 is_inbox_visible=False"
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


def test_fan_out_consent_denied_not_marked_succeeded(test_db_session):
    """bug #26 回歸：consent 被拒的家長 LINE 推播不可記為 channels_succeeded。

    家長未 opt-in 跨境 consent 時，LINE 不應送出，且稽核軌跡須能與「真的送達」
    區分——不計入 channels_succeeded，改記 channels_failed（error=consent_denied），
    且不真的呼叫 LINE adapter.send。
    """
    from datetime import datetime
    from models.database import NotificationLog, User

    user = User(
        username="parent_no_consent",
        password_hash="x",
        role="parent",
        line_user_id="UnoConsent",
        line_follow_confirmed_at=datetime.now(),
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    recipient_id = user.id

    mock_la = MagicMock()
    with (
        patch("services.notification.dispatch._get_line_adapter", return_value=mock_la),
        patch(
            "services.notification.dispatch._check_line_push_consent",
            return_value=False,
        ),
    ):
        dispatch._fan_out(
            _pevt(
                "parent.announcement",
                ("line",),
                recipient_user_id=recipient_id,
                context={"title": "x", "preview": "y", "announcement_id": 1},
            )
        )

    # consent 被拒 → 不真的送 LINE
    mock_la.send.assert_not_called()

    row = test_db_session.query(NotificationLog).first()
    assert row is not None
    assert "line" not in row.channels_succeeded, "consent 被拒不可誤記送達"
    assert any(
        f.get("channel") == "line" and f.get("error") == "consent_denied"
        for f in row.channels_failed
    ), "consent 被拒須記為 consent_denied（與真實 LINE 失敗可區分）"


def test_fan_out_consent_granted_still_sends(test_db_session):
    """consent 通過時維持原行為：LINE adapter 正常被呼叫並記 succeeded。"""
    from datetime import datetime
    from models.database import NotificationLog, User

    user = User(
        username="parent_consent",
        password_hash="x",
        role="parent",
        line_user_id="UwithConsent",
        line_follow_confirmed_at=datetime.now(),
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    recipient_id = user.id

    mock_la = MagicMock()
    with (
        patch("services.notification.dispatch._get_line_adapter", return_value=mock_la),
        patch(
            "services.notification.dispatch._check_line_push_consent",
            return_value=True,
        ),
    ):
        dispatch._fan_out(
            _pevt(
                "parent.announcement",
                ("line",),
                recipient_user_id=recipient_id,
                context={"title": "x", "preview": "y", "announcement_id": 1},
            )
        )

    mock_la.send.assert_called_once()
    row = test_db_session.query(NotificationLog).first()
    assert "line" in row.channels_succeeded


class TestPhase2LineRetry:
    """Phase 2 P1 resilience：log row 寫所有 line/ws 事件 + LINE 失敗 schedule retry."""

    def test_parent_line_only_event_writes_log_row(self, test_db_session, monkeypatch):
        """parent.fee_due (LINE-only) 也須寫 log row（之前只有 in_app 事件才寫）."""
        from services.notification import dispatch
        from models.database import User, NotificationLog
        from datetime import datetime

        # 建一個 parent user 含 line_user_id
        user = User(
            username="p1",
            password_hash="x",
            line_user_id="Uparent1",
            line_follow_confirmed_at=datetime.now(),
            is_active=True,
        )
        test_db_session.add(user)
        test_db_session.commit()  # commit so log_session (new session in _fan_out) can see the row

        # mock LINE adapter（避免真打 API）
        sent = []
        monkeypatch.setattr(
            "services.notification.dispatch._get_line_adapter",
            lambda: type(
                "A",
                (),
                {"send": lambda self, evt, r, log_id: sent.append(evt.event_type)},
            )(),
        )

        dispatch.enqueue(
            session=test_db_session,
            event_type="parent.fee_due",
            recipient_user_id=user.id,
            context={
                "student_name": "X",
                "item_name": "T",
                "amount": 100,
                "due_date": "2026-06-01",
            },
        )
        test_db_session.commit()

        rows = (
            test_db_session.query(NotificationLog)
            .filter_by(event_type="parent.fee_due")
            .all()
        )
        assert len(rows) == 1, "parent.fee_due 須寫 log row（即使無 in_app）"
        assert (
            rows[0].is_inbox_visible is False
        ), "LINE-only 事件 is_inbox_visible 須 False"

    def test_in_app_event_is_inbox_visible_true(self, test_db_session, monkeypatch):
        from services.notification import dispatch
        from models.database import User, NotificationLog

        user = User(username="emp1", password_hash="x", is_active=True)
        test_db_session.add(user)
        test_db_session.commit()

        monkeypatch.setattr(
            "services.notification.dispatch._get_line_adapter",
            lambda: type("A", (), {"send": lambda self, evt, r, log_id: None})(),
        )

        dispatch.enqueue(
            session=test_db_session,
            event_type="leave.submitted",
            recipient_user_id=user.id,
            context={
                "submitter_name": "X",
                "leave_type": "事假",
                "start": "2026-06-01",
                "end": "2026-06-01",
                "leave_hours": 4,
            },
        )
        test_db_session.commit()

        row = (
            test_db_session.query(NotificationLog)
            .filter_by(event_type="leave.submitted")
            .first()
        )
        assert row is not None
        assert row.is_inbox_visible is True

    def test_line_failure_schedules_retry(self, test_db_session, monkeypatch):
        from services.notification import dispatch
        from models.database import User, NotificationLog
        from datetime import datetime

        user = User(
            username="p2",
            password_hash="x",
            line_user_id="Uparent2",
            line_follow_confirmed_at=datetime.now(),
            is_active=True,
        )
        test_db_session.add(user)
        test_db_session.commit()  # commit so log_session can see the row

        # mock adapter throws → fan_out 寫 channels_failed + line_next_retry_at
        class Boom:
            def send(self, evt, r, log_id):
                raise ConnectionError("LINE down")

        monkeypatch.setattr(
            "services.notification.dispatch._get_line_adapter",
            lambda: Boom(),
        )

        dispatch.enqueue(
            session=test_db_session,
            event_type="parent.fee_due",
            recipient_user_id=user.id,
            context={
                "student_name": "X",
                "item_name": "T",
                "amount": 100,
                "due_date": "2026-06-01",
            },
        )
        test_db_session.commit()

        row = (
            test_db_session.query(NotificationLog)
            .filter_by(event_type="parent.fee_due")
            .first()
        )
        assert row is not None
        assert row.line_next_retry_at is not None, "LINE 失敗須 schedule retry"
        assert (
            row.line_retry_count == 0
        ), "首次失敗 count 仍 0（scheduler tick 才會 +1）"
        assert any(f.get("channel") == "line" for f in row.channels_failed)

    def test_business_rollback_no_retry_phantom(self, test_db_session, monkeypatch):
        """phantom retry on rollback guard：業務 tx rollback 後不應留 log row."""
        from services.notification import dispatch
        from models.database import User, NotificationLog

        user = User(username="p3", password_hash="x", is_active=True)
        test_db_session.add(user)
        test_db_session.commit()

        dispatch.enqueue(
            session=test_db_session,
            event_type="parent.fee_due",
            recipient_user_id=user.id,
            context={
                "student_name": "X",
                "item_name": "T",
                "amount": 100,
                "due_date": "2026-06-01",
            },
        )
        test_db_session.rollback()  # 業務 tx 滾回

        rows = (
            test_db_session.query(NotificationLog)
            .filter_by(event_type="parent.fee_due")
            .all()
        )
        assert (
            rows == []
        ), "業務 rollback 不應留 NotificationLog（log_session 也未 commit）"
