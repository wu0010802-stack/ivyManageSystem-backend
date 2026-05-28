"""LINE Login + LIFF settings. Bot messaging token 仍走 DB line_configs，不在此檔。"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LineSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    login_channel_id: str | None = Field(
        default=None, validation_alias="LINE_LOGIN_CHANNEL_ID"
    )
    login_channel_secret: str | None = Field(
        default=None, validation_alias="LINE_LOGIN_CHANNEL_SECRET", repr=False
    )
    liff_id: str | None = Field(default=None, validation_alias="LIFF_ID")
    channel_access_token: str | None = Field(
        default=None, validation_alias="LINE_CHANNEL_ACCESS_TOKEN", repr=False
    )
    vite_liff_id: str | None = Field(default=None, validation_alias="VITE_LIFF_ID")
    # Phase 4 P1 resilience：每日 token health ping 整點 (Asia/Taipei)
    token_health_ping_hour_taipei: int = Field(
        default=8, validation_alias="LINE_TOKEN_HEALTH_PING_HOUR"
    )
