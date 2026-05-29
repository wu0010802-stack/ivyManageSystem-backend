"""Medical fields encryption settings (P0d).

Encryption key 從 env `MEDICAL_FIELD_ENCRYPTION_KEY`（base64 32 bytes Fernet key）。
未設定時 helper 會 raise RuntimeError，但 prod 起動前必先 set。
dev/test 各自 generate 進 .env（不 commit）。

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §5.1
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class MedicalSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEDICAL_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    field_encryption_key: str | None = None
