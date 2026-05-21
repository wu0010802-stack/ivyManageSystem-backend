"""Recruitment-related settings: IVYKIDS sync, campus geo, TGOS fallback."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv


class RecruitmentSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # IVYKIDS sync
    ivykids_username: str | None = Field(
        default=None, validation_alias="IVYKIDS_USERNAME"
    )
    ivykids_password: str | None = Field(
        default=None, validation_alias="IVYKIDS_PASSWORD", repr=False
    )
    ivykids_login_url: str = Field(
        default="https://www.ivykids.tw/manage/", validation_alias="IVYKIDS_LOGIN_URL"
    )
    ivykids_data_url: str = Field(
        default="https://www.ivykids.tw/manage/make_an_appointment/",
        validation_alias="IVYKIDS_DATA_URL",
    )
    ivykids_sync_enabled: BoolEnv = Field(
        default=False, validation_alias="IVYKIDS_SYNC_ENABLED"
    )
    ivykids_sync_interval_minutes: int = Field(
        default=10, validation_alias="IVYKIDS_SYNC_INTERVAL_MINUTES"
    )

    # Campus geo (RECRUITMENT_CAMPUS_* prefix)
    campus_name: str | None = Field(
        default=None, validation_alias="RECRUITMENT_CAMPUS_NAME"
    )
    campus_address: str | None = Field(
        default=None, validation_alias="RECRUITMENT_CAMPUS_ADDRESS"
    )
    campus_lat: float | None = Field(
        default=None, validation_alias="RECRUITMENT_CAMPUS_LAT"
    )
    campus_lng: float | None = Field(
        default=None, validation_alias="RECRUITMENT_CAMPUS_LNG"
    )
    campus_travel_mode: str = Field(
        default="driving", validation_alias="RECRUITMENT_CAMPUS_TRAVEL_MODE"
    )

    # TGOS fallback
    tgos_app_id: str | None = Field(default=None, validation_alias="TGOS_APP_ID")
    tgos_api_key: str | None = Field(
        default=None, validation_alias="TGOS_API_KEY", repr=False
    )

    # Market intelligence
    market_timeout_seconds: int = Field(
        default=8, validation_alias="RECRUITMENT_MARKET_TIMEOUT_SECONDS"
    )
