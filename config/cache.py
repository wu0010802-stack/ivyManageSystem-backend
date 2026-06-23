"""config/cache.py — cache + broadcast Redis settings.

`CACHE_BACKEND` controls the shared application cache driver.
`BROADCAST_BACKEND` can override only the websocket broadcast backend; when it is
unset, broadcast follows `CACHE_BACKEND` for backward compatibility.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CacheSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CACHE_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    backend: Literal["memory", "redis"] = "memory"
    broadcast_backend: Literal["memory", "redis"] | None = Field(
        default=None,
        validation_alias="BROADCAST_BACKEND",
    )
    redis_url: str | None = None
    key_prefix: str = "ivy"
    pubsub_timeout_seconds: float = 5.0
    publish_payload_max_bytes: int = 8192

    @model_validator(mode="after")
    def _validate_redis_url(self) -> "CacheSettings":
        if (
            self.backend == "redis" or self.effective_broadcast_backend == "redis"
        ) and not self.redis_url:
            raise ValueError(
                "CACHE_REDIS_URL is required when CACHE_BACKEND or "
                "BROADCAST_BACKEND is redis"
            )
        return self

    @property
    def effective_broadcast_backend(self) -> Literal["memory", "redis"]:
        """Broadcast backend, defaulting to CACHE_BACKEND for backward compatibility."""
        return self.broadcast_backend or self.backend
