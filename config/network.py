"""Network-related settings: CORS, hosts, proxy, CSP, cookies, rate limit."""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import CsvList


class NetworkSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    cors_origins: CsvList = []
    allowed_hosts: CsvList = []
    trusted_proxy_ips: str = "*"
    csp_script_hashes: CsvList = []
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    school_wifi_ips: CsvList = []
    rate_limit_backend: str = "memory"
