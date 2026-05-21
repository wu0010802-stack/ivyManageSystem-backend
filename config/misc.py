"""Miscellaneous settings that don't fit other domains."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv

_TTL_DEFAULT = 180
_POS_CASH_DEFAULT = 30_000


class MiscSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    anthropic_api_key: str | None = Field(
        default=None, validation_alias="ANTHROPIC_API_KEY", repr=False
    )
    # default 對齊 api/activity/pos.py 原碼 fallback（30_000，非 plan 寫的 5000）
    # invalid 值（非整數）fallback 預設，對齊原 service 行為。
    pos_cash_deposit_warning_threshold: int = Field(
        default=_POS_CASH_DEFAULT, validation_alias="POS_CASH_DEPOSIT_WARNING_THRESHOLD"
    )

    @field_validator("pos_cash_deposit_warning_threshold", mode="before")
    @classmethod
    def _coerce_pos_cash(cls, v: object) -> int:
        """無法解析的字串 → fallback 預設 30000，對齊原 service try/except 行為。"""
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _POS_CASH_DEFAULT

    enable_leave_ot_offset: BoolEnv = Field(
        default=False, validation_alias="ENABLE_LEAVE_OT_OFFSET"
    )
    # default 對齊 services/activity_query_token.py 原碼（180 天，非 plan 寫的 30）
    # invalid 值（非正整數）fallback 預設值，對齊原 service 行為。
    activity_query_token_ttl_days: int = Field(
        default=_TTL_DEFAULT, validation_alias="ACTIVITY_QUERY_TOKEN_TTL_DAYS"
    )

    @field_validator("activity_query_token_ttl_days", mode="before")
    @classmethod
    def _clamp_ttl(cls, v: object) -> int:
        """非正整數 / 無法解析 → fallback 預設 180，對齊原 service 行為。"""
        try:
            parsed = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _TTL_DEFAULT
        return parsed if parsed > 0 else _TTL_DEFAULT

    ivy_mcp_username: str | None = Field(
        default=None, validation_alias="IVY_MCP_USERNAME"
    )
    ivy_mcp_password: str | None = Field(
        default=None, validation_alias="IVY_MCP_PASSWORD", repr=False
    )
    # MCP client 連回後端 API base URL（給 ivy-activity-crud MCP server 用）
    ivy_api_base_url: str = Field(
        default="http://localhost:8088", validation_alias="IVY_API_BASE_URL"
    )
