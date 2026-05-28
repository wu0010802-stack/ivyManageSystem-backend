"""File storage settings: local FS / Supabase Storage / growth reports."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # 型別保留 str（不用 Literal），讓 utils/storage.py 自己驗證並 raise
    # ValueError("未知的 STORAGE_BACKEND: ...")（對齊原 get_backend() 行為）。
    backend: str = Field(default="local", validation_alias="STORAGE_BACKEND")
    # default None：未設 env 時由 utils/storage.py 決定 fallback root（保留原本 <repo>/data/uploads 慣例）
    root: Path | None = Field(default=None, validation_alias="STORAGE_ROOT")
    supabase_url: str | None = Field(default=None, validation_alias="SUPABASE_URL")
    supabase_service_role_key: str | None = Field(
        default=None, validation_alias="SUPABASE_SERVICE_ROLE_KEY", repr=False
    )
    supabase_signed_url_ttl: int = Field(
        default=300,  # 3600→300（5 分鐘）。Prod 若 UX 有問題，env override 可暫 revert
        validation_alias="SUPABASE_STORAGE_SIGNED_URL_TTL",
    )
    growth_report_root: Path = Field(
        default=Path("./growth_reports"), validation_alias="GROWTH_REPORT_ROOT"
    )
    growth_report_max_bytes: int = Field(
        default=5_242_880, validation_alias="GROWTH_REPORT_MAX_BYTES"
    )
    # Phase 4 P1 resilience：Supabase 失敗時本機暫存開關
    local_fallback_enabled: bool = Field(
        default=True, validation_alias="STORAGE_LOCAL_FALLBACK_ENABLED"
    )
    local_fallback_max_mb: int = Field(
        default=5000, validation_alias="STORAGE_LOCAL_FALLBACK_MAX_MB"
    )
