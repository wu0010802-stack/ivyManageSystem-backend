"""Sentry error tracking settings. DSN empty = no-op (整套 Sentry 自動關閉)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SentrySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SENTRY_", extra="ignore", case_sensitive=False
    )

    dsn: str | None = Field(default=None, repr=False)
    environment: str = "production"
    release: str | None = None
    traces_sample_rate: float = 0.1

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)
