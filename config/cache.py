"""config/cache.py — cache layer settings.

PR1 only ships memory backend；PR2 才會接 Redis。本檔先把欄位定起來，
即便 PR1 不會讀 redis_url，PR2 也能 in-place 補。
"""

from __future__ import annotations

from typing import Literal

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
