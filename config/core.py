"""Core application settings: env, database, JWT, admin init."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv

_DEV_ROUTER_ENVS = frozenset({"development", "dev", "local", "test"})


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    env: str = "development"
    # 部署形態（設計審查 2026-06-25 LONG-1 scale-out 協調 gate）：single=單 uvicorn
    # worker（當前 prod 形態）；multi=多 worker / 多 pod。multi 時 startup gate 會強制
    # 所有跨 worker 必須共享的 backend（cache/broadcast/rate-limit）非 in-process memory，
    # 否則拒啟動——把「scale-out 前要翻好幾個 env flag」的隱性合約變成單一 fail-fast 守衛。
    deployment_mode: Literal["single", "multi"] = Field(
        default="single", validation_alias="DEPLOYMENT_MODE"
    )
    database_url: str | None = None
    jwt_secret_key: str | None = Field(default=None, repr=False)
    # JWT rotation 用，accept-only 舊 secrets 的 JSON list 字串
    # 設計：docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md
    jwt_secret_keys_olds: str = Field(default="[]", repr=False)
    jwt_absolute_lifetime_hours: int = 8
    enable_api_docs: BoolEnv = False
    admin_init_username: str | None = None
    admin_init_password: str | None = Field(default=None, repr=False)

    # 連線池參數（10 base + 10 overflow = 20/pod）。
    # 原為 5+5=10 對 Supabase Session Mode（硬上限 15 clients）保守設定；prod 已遷
    # Zeabur PostgreSQL，該上限不再適用，~1472 個同步路由共搶 10 條會在並發 >10 時
    # 排隊逾 pool_timeout 後 500。單 worker 下 20 條對 PG（預設 max_connections=100）
    # 安全。⚠ 調更大前須確認 Zeabur PG 實際 max_connections（部分託管 PG / pgbouncer
    # 預設僅 25-50），可經 env DB_POOL_SIZE / DB_POOL_MAX_OVERFLOW 覆寫
    # （系統設計審查 2026-06-14，top#2）。
    db_pool_size: int = Field(default=10, validation_alias="DB_POOL_SIZE")
    db_pool_max_overflow: int = Field(
        default=10, validation_alias="DB_POOL_MAX_OVERFLOW"
    )
    db_pool_timeout: int = Field(default=15, validation_alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=1800, validation_alias="DB_POOL_RECYCLE")
    # AnyIO threadpool token 數對齊：同步 def 路由跑在此 threadpool，每個多半需一條
    # DB 連線。把 token 上限對齊到「pool 容量 + headroom」，避免 threadpool 准入遠超
    # pool 能服務的量（否則並發 > pool 時請求搶到 thread 卻卡在 pool checkout 逾時
    # 500）。實際 token = db_pool_size + db_pool_max_overflow + headroom；headroom 給
    # 排程器 / WS / 純 CPU 工作留餘裕。設 0 = 沿用 anyio 預設（不調整）。
    thread_pool_headroom: int = Field(
        default=8, validation_alias="THREAD_POOL_HEADROOM"
    )

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in ("production", "prod")

    @property
    def docs_enabled(self) -> bool:
        """是否掛載 /docs /redoc /openapi.json。

        Fail-closed：僅顯式 ENABLE_API_DOCS=true 才開放（預設 False）。
        舊邏輯 `enable_api_docs or not is_production` 為 fail-open——ENV 拼錯/
        漏設（任何非 production 字面，如 'staging'/typo/空字串）即自動開放，
        把完整 router/schema/權限欄位地圖洩漏給未認證者（資安掃描 2026-06-16 C30）。
        dev 需看 docs 顯式設 ENABLE_API_DOCS=true。
        """
        return self.enable_api_docs

    @property
    def dev_router_enabled(self) -> bool:
        return self.env.strip().lower() in _DEV_ROUTER_ENVS

    @property
    def dev_router_should_mount(self) -> bool:
        """嚴格判斷：ENV 必須顯式設為 dev 值才 mount dev router。

        未設 ENV（model_fields_set 不含 env，default 'development' fallback）視為「未配置 dev」，
        回 False。對齊原 main.py:_should_mount_dev_router 的安全保守邏輯
        （unset ENV → 不 mount dev router）。
        """
        if "env" not in self.model_fields_set:
            return False
        return self.dev_router_enabled
