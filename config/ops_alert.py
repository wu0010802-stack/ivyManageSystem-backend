"""Ops 告警設定（慢請求突發 → LINE group push）。

group_id 為 None 時 alerter 仍會計數但僅 log warn（避免無聲掉訊）。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsAlertSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPS_ALERT_", extra="ignore", case_sensitive=False
    )

    line_group_id: str | None = Field(default=None)
    slow_request_alert_window_seconds: int = 60
    slow_request_alert_threshold: int = 10
    slow_request_alert_cooldown_seconds: int = 300

    # UptimeRobot webhook 驗證 token（free tier 不支援 custom header，走 query param）。
    # validation_alias 跳過 env_prefix（OPS_ALERT_），用全域 UPTIME_ROBOT_WEBHOOK_TOKEN。
    uptime_robot_webhook_token: str | None = Field(
        default=None, validation_alias="UPTIME_ROBOT_WEBHOOK_TOKEN", repr=False
    )

    @property
    def enabled(self) -> bool:
        return self.slow_request_alert_window_seconds > 0
