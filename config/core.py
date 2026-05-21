"""Core application settings: env, database, JWT, admin init."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .validators import BoolEnv

_DEV_ROUTER_ENVS = frozenset({"development", "dev", "test", "testing", ""})


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    env: str = "development"
    database_url: str = "postgresql://localhost:5432/ivymanagement"
    jwt_secret_key: str | None = None
    jwt_absolute_lifetime_hours: int = 8
    enable_api_docs: BoolEnv = False
    admin_init_username: str | None = None
    admin_init_password: str | None = None

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in ("production", "prod")

    @property
    def dev_router_enabled(self) -> bool:
        return self.env.strip().lower() in _DEV_ROUTER_ENVS
