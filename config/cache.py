"""config/cache.py — cache + broadcast Redis settings.

PR1 only ships memory backend；PR2 才會接 Redis cache。
本 PR 接 Redis 給 WS broadcast 用，cache 用法是 follow-up。
"""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CacheSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CACHE_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None
    key_prefix: str = "ivy"
    pubsub_timeout_seconds: float = 5.0
    publish_payload_max_bytes: int = 8192

    @model_validator(mode="after")
    def _validate_redis_url(self) -> "CacheSettings":
        if self.backend == "redis" and not self.redis_url:
            raise ValueError("CACHE_REDIS_URL is required when CACHE_BACKEND=redis")
        return self
