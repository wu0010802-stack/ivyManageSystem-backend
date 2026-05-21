"""Network-related settings: CORS, hosts, proxy, CSP, cookies, rate limit."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import CsvList


class NetworkSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    cors_origins: CsvList = []
    allowed_hosts: CsvList = []
    trusted_proxy_ips: str = "*"
    csp_script_hashes: CsvList = []
    # 注意 default "strict" 對齊 utils/cookie.py 原始安全預設（CSRF 最強防護）。
    # 型別保留 str（不用 Literal），讓 utils/cookie.py 自己處理 invalid value warn+fallback。
    cookie_samesite: str = "strict"
    school_wifi_ips: CsvList = []
    rate_limit_backend: str = "memory"
