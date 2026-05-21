"""Miscellaneous settings that don't fit other domains."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv


class MiscSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    anthropic_api_key: str | None = Field(
        default=None, validation_alias="ANTHROPIC_API_KEY", repr=False
    )
    pos_cash_deposit_warning_threshold: int = Field(
        default=5000, validation_alias="POS_CASH_DEPOSIT_WARNING_THRESHOLD"
    )
    enable_leave_ot_offset: BoolEnv = Field(
        default=False, validation_alias="ENABLE_LEAVE_OT_OFFSET"
    )
    activity_query_token_ttl_days: int = Field(
        default=30, validation_alias="ACTIVITY_QUERY_TOKEN_TTL_DAYS"
    )
    ivy_mcp_username: str | None = Field(
        default=None, validation_alias="IVY_MCP_USERNAME"
    )
    ivy_mcp_password: str | None = Field(
        default=None, validation_alias="IVY_MCP_PASSWORD", repr=False
    )
