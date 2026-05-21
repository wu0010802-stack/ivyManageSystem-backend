"""Reusable Pydantic validators for env-derived fields."""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import NoDecode


def parse_bool_env(v: str | bool | None) -> bool:
    """Accept '1' / 'true' / 'yes' (case-insensitive). Everything else → False."""
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes")


def parse_csv_list(v: str | list[str] | None) -> list[str]:
    """Parse comma-separated string into list of trimmed non-empty strings."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [s.strip() for s in str(v).split(",") if s.strip()]


BoolEnv = Annotated[bool, BeforeValidator(parse_bool_env)]
CsvList = Annotated[list[str], NoDecode, BeforeValidator(parse_csv_list)]
