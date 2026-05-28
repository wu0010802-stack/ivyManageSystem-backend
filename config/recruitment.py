"""Recruitment-related settings: IVYKIDS sync, campus geo, TGOS fallback, market intelligence URLs."""

from __future__ import annotations

from pydantic import Field, field_validator
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
    # cutoff 字串給 sync helper 自己 parse
    ivykids_sync_created_at_cutoff: str = Field(
        default="2024-04-26 10:46:04",
        validation_alias="IVYKIDS_SYNC_CREATED_AT_CUTOFF",
    )

    # Campus geo (RECRUITMENT_CAMPUS_* prefix)
    # 注意：campus_name default 對齊 services/recruitment_market_intelligence.py 原 default "本園"
    campus_name: str = Field(default="本園", validation_alias="RECRUITMENT_CAMPUS_NAME")
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
    tgos_query_addr_url: str = Field(
        default="http://gis.tgos.tw/addrws/v30/QueryAddr.asmx/QueryAddr",
        validation_alias="TGOS_QUERY_ADDR_URL",
    )
    tgos_route_url: str = Field(
        default="http://gis.tgos.tw/TGRoute/TGRoute.aspx",
        validation_alias="TGOS_ROUTE_URL",
    )

    # Market intelligence (附近幼兒園/人口資料/Routes API)
    # default 對齊 services/recruitment_market_intelligence.py 原碼 (12 秒，非 plan 寫的 8 秒)
    market_timeout_seconds: int = Field(
        default=12, validation_alias="RECRUITMENT_MARKET_TIMEOUT_SECONDS"
    )
    google_routes_api_url: str = Field(
        default="https://routes.googleapis.com/directions/v2:computeRoutes",
        validation_alias="GOOGLE_ROUTES_API_URL",
    )
    google_places_text_api_url: str = Field(
        default="https://places.googleapis.com/v1/places:searchText",
        validation_alias="GOOGLE_PLACES_TEXT_API_URL",
    )
    nlsc_town_query_url: str = Field(
        default="https://api.nlsc.gov.tw/other/TownVillagePointQuery",
        validation_alias="NLSC_TOWN_QUERY_URL",
    )
    nlsc_land_use_url: str = Field(
        default="https://api.nlsc.gov.tw/other/LandUsePointQuery",
        validation_alias="NLSC_LAND_USE_URL",
    )
    population_density_url: str = Field(
        default="",
        validation_alias="RECRUITMENT_POPULATION_DENSITY_URL",
    )
    population_age_url: str = Field(
        default="",
        validation_alias="RECRUITMENT_POPULATION_AGE_URL",
    )

    # K-anonymity 抑制門檻（招生地址熱點 bucket 最小 visit 數，少於此值不 render marker）
    k_anonymity_threshold: int = Field(
        default=5,
        validation_alias="RECRUITMENT_K_ANONYMITY_THRESHOLD",
    )

    @field_validator("k_anonymity_threshold")
    @classmethod
    def _clamp_k_threshold(cls, v: int) -> int:
        return max(2, min(10, v))
