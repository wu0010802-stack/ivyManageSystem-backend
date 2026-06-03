"""個資法 Consent 強制執行設定（dark-launch flag）。

預設 false：所有 consent 檢查為 no-op，方便分批灰度上線。
設為 true 後，缺同意書的請求將收到 403（由各 router 的 consent_guard 處理）。

啟用方式：zeabur dashboard 或 .env 設 CONSENT_ENFORCEMENT_ENABLED=true。
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConsentSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    enforcement_enabled: bool = Field(
        default=False,
        validation_alias="CONSENT_ENFORCEMENT_ENABLED",
        description="個資法 consent 強制總開關；false=全 no-op（dark-launch）",
    )
