"""tests/test_line_token_health_scheduler.py — Phase 4 P1 resilience unit tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestTickLineTokenHealth:
    def test_tick_200_marks_healthy(self, test_db_session, monkeypatch):
        """200 OK → singleton row healthy=True，consecutive_failures reset 0。"""
        from models.integration_health import LineTokenHealth
        from services.line_token_health_scheduler import tick_line_token_health

        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "fake-token")
        # reload settings to pick up env
        from config import settings as _s
        _s.line.__init__()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("services.line_token_health_scheduler.requests.get", return_value=mock_resp):
            result = tick_line_token_health()

        assert result["healthy"] is True
        assert result.get("error") is None

        row = test_db_session.query(LineTokenHealth).filter(LineTokenHealth.id == 1).first()
        assert row is not None
        assert row.healthy is True
        assert row.consecutive_failures == 0

    def test_tick_401_marks_unhealthy_and_milestone_alert(
        self, test_db_session, monkeypatch
    ):
        """401 → healthy=False，consecutive_failures=1，tagged_capture 呼叫一次。"""
        from models.integration_health import LineTokenHealth
        from services.line_token_health_scheduler import tick_line_token_health

        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "fake-token")
        from config import settings as _s
        _s.line.__init__()

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("services.line_token_health_scheduler.requests.get", return_value=mock_resp):
            with patch("services.line_token_health_scheduler.tagged_capture") as mock_cap:
                result = tick_line_token_health()

        assert result["healthy"] is False
        assert result["error"] == "http_401"

        row = test_db_session.query(LineTokenHealth).filter(LineTokenHealth.id == 1).first()
        assert row is not None
        assert row.healthy is False
        assert row.consecutive_failures == 1
        # milestone 1 → alert once
        mock_cap.assert_called_once()

    def test_tick_connection_error_marks_unhealthy_warning(
        self, test_db_session, monkeypatch
    ):
        """ConnectionError → healthy=False，tagged_capture level=warning。"""
        from models.integration_health import LineTokenHealth
        from services.line_token_health_scheduler import tick_line_token_health

        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "fake-token")
        from config import settings as _s
        _s.line.__init__()

        with patch(
            "services.line_token_health_scheduler.requests.get",
            side_effect=ConnectionError("unreachable"),
        ):
            with patch("services.line_token_health_scheduler.tagged_capture") as mock_cap:
                result = tick_line_token_health()

        assert result["healthy"] is False
        assert result["error"] == "ConnectionError"

        mock_cap.assert_called_once()
        call_kwargs = mock_cap.call_args.kwargs
        assert call_kwargs.get("level") == "warning"

    def test_dedup_only_milestones_alert(self, test_db_session, monkeypatch):
        """consecutive_failures=2（非里程碑）→ tagged_capture 不呼叫。"""
        from models.integration_health import LineTokenHealth
        from services.line_token_health_scheduler import tick_line_token_health

        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "fake-token")
        from config import settings as _s
        _s.line.__init__()

        # Pre-seed row with consecutive_failures=1 (already alerted for milestone 1)
        from datetime import datetime, timezone
        row = LineTokenHealth(
            id=1,
            last_check_at=datetime.now(timezone.utc),
            healthy=False,
            last_error="http_401",
            consecutive_failures=1,
        )
        test_db_session.add(row)
        test_db_session.commit()

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("services.line_token_health_scheduler.requests.get", return_value=mock_resp):
            with patch("services.line_token_health_scheduler.tagged_capture") as mock_cap:
                result = tick_line_token_health()

        # consecutive_failures goes to 2 (not in milestones [1,8,30]) → no alert
        test_db_session.refresh(row)
        assert row.consecutive_failures == 2
        mock_cap.assert_not_called()

    def test_disabled_skips(self, monkeypatch):
        """LINE_CHANNEL_ACCESS_TOKEN 未設 → 直接 return skipped。"""
        from services.line_token_health_scheduler import tick_line_token_health

        monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
        from config import settings as _s
        _s.line.__init__()

        result = tick_line_token_health()
        assert result.get("skipped") == "no_token"
