"""Centralized Settings combining all sub-Settings domains."""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .cache import CacheSettings
from .core import CoreSettings
from .geocoding import GeocodingSettings
from .line import LineSettings
from .misc import MiscSettings
from .network import NetworkSettings
from .ops import OpsSettings
from .ops_alert import OpsAlertSettings
from .parent_db import ParentDBSettings
from .recruitment import RecruitmentSettings
from .scheduler import SchedulerSettings
from .sentry import SentrySettings
from .storage import StorageSettings

_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "secret",
    "password",
    "token",
    "api_key",
    "dsn",
)

# Substring 匹配的副作用：含 token/secret 等字眼但實際無敏感資訊的欄位（如
# activity_query_token_ttl_days 是天數常數），需要透過 exact-name exempt 避免誤遮。
# 新增類似欄位時，請在此補上對應的 exact key name（不是 substring）。
_SENSITIVE_KEY_EXEMPT: frozenset[str] = frozenset({"activity_query_token_ttl_days"})


def _scrub(data: Any, denylist: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = _scrub(v, denylist)
        elif (
            isinstance(k, str)
            and k not in _SENSITIVE_KEY_EXEMPT
            and any(s in k.lower() for s in denylist)
            and v not in (None, "")
        ):
            out[k] = "***"
        else:
            out[k] = v
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    core: CoreSettings = Field(default_factory=CoreSettings)
    parent_db: ParentDBSettings = Field(default_factory=ParentDBSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    line: LineSettings = Field(default_factory=LineSettings)
    recruitment: RecruitmentSettings = Field(default_factory=RecruitmentSettings)
    geocoding: GeocodingSettings = Field(default_factory=GeocodingSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    misc: MiscSettings = Field(default_factory=MiscSettings)
    ops_alert: OpsAlertSettings = Field(default_factory=OpsAlertSettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)

    def model_dump_safe(self) -> dict[str, Any]:
        """Dump settings with sensitive fields redacted to '***'."""
        return _scrub(self.model_dump(), _SENSITIVE_KEY_SUBSTRINGS)
