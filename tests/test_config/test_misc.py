import pytest
from config.misc import MiscSettings


def test_defaults(monkeypatch):
    for var in (
        "ANTHROPIC_API_KEY",
        "POS_CASH_DEPOSIT_WARNING_THRESHOLD",
        "ENABLE_LEAVE_OT_OFFSET",
        "ACTIVITY_QUERY_TOKEN_TTL_DAYS",
        "IVY_MCP_USERNAME",
        "IVY_MCP_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    s = MiscSettings()
    assert s.anthropic_api_key is None
    assert s.pos_cash_deposit_warning_threshold == 30_000
    assert s.enable_leave_ot_offset is False
    assert s.activity_query_token_ttl_days == 180
    assert s.ivy_mcp_username is None
    assert s.ivy_mcp_password is None


def test_env_reads(monkeypatch):
    monkeypatch.setenv("ENABLE_LEAVE_OT_OFFSET", "true")
    monkeypatch.setenv("POS_CASH_DEPOSIT_WARNING_THRESHOLD", "10000")
    monkeypatch.setenv("ACTIVITY_QUERY_TOKEN_TTL_DAYS", "60")
    s = MiscSettings()
    assert s.enable_leave_ot_offset is True
    assert s.pos_cash_deposit_warning_threshold == 10000
    assert s.activity_query_token_ttl_days == 60
