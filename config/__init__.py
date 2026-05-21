"""Centralized application settings (Phase 1)."""

from __future__ import annotations

from functools import lru_cache

from .base import Settings

__all__ = ["Settings", "settings", "get_settings", "reset_for_tests"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide Settings singleton. Use this in lazy contexts."""
    return Settings()


def reset_for_tests() -> None:
    """Clear lru_cache so next get_settings() call re-reads env. Test use only."""
    get_settings.cache_clear()


settings: Settings = get_settings()
"""Module-level Settings alias for eager imports."""
