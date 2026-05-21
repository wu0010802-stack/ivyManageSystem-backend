"""Parent portal RLS-isolated DB credentials + RLS feature flags."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv


class ParentDBSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    user: str | None = Field(
        default=None, validation_alias="PARENT_DB_USER", repr=False
    )
    password: str | None = Field(
        default=None, validation_alias="PARENT_DB_PASSWORD", repr=False
    )
    rls_guard_enabled: BoolEnv = Field(
        default=False, validation_alias="PARENT_RLS_GUARD_ENABLED"
    )
    rls_metrics_disabled: BoolEnv = Field(
        default=False, validation_alias="PARENT_RLS_METRICS_DISABLED"
    )
