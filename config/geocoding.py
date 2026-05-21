"""Geocoding settings: Google Maps / Nominatim / TGOS fallback chain."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GeocodingSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    google_maps_api_key: str | None = Field(
        default=None, validation_alias="GOOGLE_MAPS_API_KEY", repr=False
    )
    provider: Literal["google", "nominatim", "tgos"] = Field(
        default="nominatim", validation_alias="GEOCODING_PROVIDER"
    )
    user_agent: str = Field(
        default="ivyManageSystem/1.0", validation_alias="GEOCODING_USER_AGENT"
    )
    contact_email: str | None = Field(
        default=None, validation_alias="GEOCODING_CONTACT_EMAIL"
    )
    timeout_seconds: int = Field(
        default=8, validation_alias="GEOCODING_TIMEOUT_SECONDS"
    )
    google_geocoding_url: str = Field(
        default="https://maps.googleapis.com/maps/api/geocode/json",
        validation_alias="GOOGLE_GEOCODING_URL",
    )
    nominatim_url: str = Field(
        default="https://nominatim.openstreetmap.org/search",
        validation_alias="GEOCODING_NOMINATIM_URL",
    )
