"""Centralized application settings (Phase 1)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from .base import Settings

__all__ = ["Settings", "settings", "get_settings", "reset_for_tests"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide Settings singleton. Use this in lazy contexts."""
    return Settings()


def reset_for_tests() -> None:
    """Clear lru_cache so next get_settings() call re-reads env. Test use only."""
    get_settings.cache_clear()


class _SettingsProxy:
    """Lazy proxy: every attribute access re-resolves via get_settings().

    Why a proxy instead of a module-level alias `settings = get_settings()`:

    Callers commonly do `from config import settings` once at import time and then
    read `settings.<domain>.<field>` throughout the module. If `settings` were a
    plain alias to the lru_cache result, tests that call `monkeypatch.setenv` +
    `reset_for_tests()` would still see the stale Settings instance because the
    importing module holds a reference to it.

    A proxy fixes this: each `settings.X` access goes through `get_settings()`,
    which re-instantiates Settings whenever the lru_cache is cleared. Prod cost
    is negligible (one attribute hop per access; lru_cache returns the same
    instance until explicitly cleared).
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)

    def __repr__(self) -> str:
        return f"<SettingsProxy {get_settings()!r}>"


settings: Settings = _SettingsProxy()  # type: ignore[assignment]
"""Module-level Settings proxy. `from config import settings` always sees current env."""
