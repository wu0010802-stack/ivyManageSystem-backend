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

    # 全域 request body 大小上限（bytes）。Starlette/uvicorn 預設不限 body 大小，單一
    # 超大 body（數百 MB JSON）會在驗證前先被 uvicorn 收進記憶體 → 單 worker 記憶體飆升。
    # 預設 64MB：高於最大合法上傳（檔案 50MB，見 utils/file_upload），純擋「數百 MB」攻擊。
    # 可經 env MAX_REQUEST_BODY_BYTES 調整（改 Zeabur Service Variables + 重啟即生效）。
    max_request_body_bytes: int = 64 * 1024 * 1024

    # 家長公開報名端 per-IP 限流（max_calls / window_seconds）。
    # 可經 env 在上線尖峰「免改碼」調整（改 Zeabur Service Variables + 重啟即生效），
    # 不必 push 後端——配合報名視窗內的部署凍結紀律。
    # 預設值針對「校園 / 社區 / 電信 CGNAT 共用同一出口公網 IP」放寬：原 register 5/min
    # 在多位家長共用一個 NAT 出口時會互相擠掉額度（穩定度稽核 2026-06-23 P2）。
    # ⚠ 真正防超賣靠 register 的 with_for_update 行鎖 + IntegrityError，放寬限流不損正確性。
    # register 與 public_update 共用此額度。
    activity_register_rate_max: int = 20  # env: ACTIVITY_REGISTER_RATE_MAX（原 5）
    activity_register_rate_window: int = 60  # env: ACTIVITY_REGISTER_RATE_WINDOW
    activity_query_rate_max: int = 30  # env: ACTIVITY_QUERY_RATE_MAX（原 10）
    activity_query_rate_window: int = 60  # env: ACTIVITY_QUERY_RATE_WINDOW
    activity_inquiry_rate_max: int = 10  # env: ACTIVITY_INQUIRY_RATE_MAX（原 3）
    activity_inquiry_rate_window: int = 60  # env: ACTIVITY_INQUIRY_RATE_WINDOW
