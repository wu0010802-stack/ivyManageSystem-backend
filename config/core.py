"""Core application settings: env, database, JWT, admin init."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv

_DEV_ROUTER_ENVS = frozenset({"development", "dev", "local", "test"})


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    env: str = "development"
    database_url: str | None = None
    jwt_secret_key: str | None = Field(default=None, repr=False)
    jwt_absolute_lifetime_hours: int = 8
    enable_api_docs: BoolEnv = False
    admin_init_username: str | None = None
    admin_init_password: str | None = Field(default=None, repr=False)

    # 連線池參數（5 base + 5 overflow = 10/pod 對 Supabase Session Mode 安全）
    db_pool_size: int = Field(default=5, validation_alias="DB_POOL_SIZE")
    db_pool_max_overflow: int = Field(
        default=5, validation_alias="DB_POOL_MAX_OVERFLOW"
    )
    db_pool_timeout: int = Field(default=15, validation_alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=1800, validation_alias="DB_POOL_RECYCLE")

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in ("production", "prod")

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
