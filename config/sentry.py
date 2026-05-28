"""Sentry error tracking settings. DSN empty = no-op (整套 Sentry 自動關閉)."""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_TRACES_DEFAULT = 0.1


class SentrySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SENTRY_", extra="ignore", case_sensitive=False
    )

    dsn: str | None = Field(default=None, repr=False)
    environment: str = "production"
    release: str | None = None
    # invalid float fallback 預設，對齊原 utils/sentry_init.py 容錯行為。
    traces_sample_rate: float = _TRACES_DEFAULT
    tag_external_failures: bool = True  # Phase 1 P1 resilience: 外呼站點 tagged_capture 總開關

    @field_validator("traces_sample_rate", mode="before")
    @classmethod
    def _coerce_traces_rate(cls, v: object) -> float:
        """無法解析的字串 → fallback 預設 0.1，對齊原 service try/except 行為。"""
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _TRACES_DEFAULT

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)
