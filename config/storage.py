"""File storage settings: local FS / Supabase Storage / growth reports."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    backend: Literal["local", "supabase"] = Field(
        default="local", validation_alias="STORAGE_BACKEND"
    )
    root: Path = Field(default=Path("./uploads"), validation_alias="STORAGE_ROOT")
    supabase_url: str | None = Field(default=None, validation_alias="SUPABASE_URL")
    supabase_service_role_key: str | None = Field(
        default=None, validation_alias="SUPABASE_SERVICE_ROLE_KEY", repr=False
    )
    supabase_signed_url_ttl: int = Field(
        default=3600, validation_alias="SUPABASE_STORAGE_SIGNED_URL_TTL"
    )
    growth_report_root: Path = Field(
        default=Path("./growth_reports"), validation_alias="GROWTH_REPORT_ROOT"
    )
    growth_report_max_bytes: int = Field(
        default=5_242_880, validation_alias="GROWTH_REPORT_MAX_BYTES"
    )
