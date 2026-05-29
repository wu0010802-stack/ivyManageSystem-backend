"""Ops kill switch 設定（維護模式 / 唯讀模式）。

env-only：避開事故時 DB 可能掛；zeabur dashboard 直接 flip env 即生效。
搭配 utils/kill_switch.py KillSwitchMiddleware 在請求進入時短路 503。

維運手冊：docs/sop/dr-runbook.md 啟用維護/唯讀模式章節。

注意：此 sub-Settings 與 OpsAlertSettings（慢請求告警，env_prefix `OPS_ALERT_`）
是不同 domain，兩者不要混用。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    maintenance_mode: bool = Field(
        default=False,
        validation_alias="MAINTENANCE_MODE",
    )
    read_only_mode: bool = Field(
        default=False,
        validation_alias="READ_ONLY_MODE",
    )
    maintenance_message: str = Field(
        default="系統維護中，請稍後再試",
        validation_alias="MAINTENANCE_MESSAGE",
    )
