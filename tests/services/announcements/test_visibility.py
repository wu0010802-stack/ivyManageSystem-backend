"""Tests for services.announcements.visibility helpers."""

from datetime import datetime, timedelta

import pytest

from models.database import Announcement
from services.announcements.visibility import (
    derive_status,
    visibility_time_predicate,
)


def test_visibility_predicate_returns_sqlalchemy_clause():
    """Predicate must compose with SQLAlchemy filter()."""
    now = datetime(2026, 5, 29, 8, 0, 0)
    pred = visibility_time_predicate(now)
    # Smoke: 能編譯成 SQL，包含 publish_at 與 expires_at 條件
    compiled = str(pred.compile(compile_kwargs={"literal_binds": True}))
    assert "publish_at" in compiled
    assert "expires_at" in compiled
    assert "2026-05-29" in compiled


@pytest.mark.parametrize(
    "publish_at_delta,expires_at_delta,expected",
    [
        (None, None, "active"),
        (-timedelta(hours=1), None, "active"),
        (timedelta(hours=1), None, "scheduled"),
        (None, timedelta(hours=1), "active"),
        (None, -timedelta(hours=1), "expired"),
        (-timedelta(hours=2), -timedelta(hours=1), "expired"),
        (timedelta(hours=1), timedelta(hours=2), "scheduled"),
    ],
)
def test_derive_status_combinations(publish_at_delta, expires_at_delta, expected):
    now = datetime(2026, 5, 29, 8, 0, 0)
    ann = Announcement(
        title="T",
        content="C",
        created_by=1,
        publish_at=(now + publish_at_delta) if publish_at_delta is not None else None,
        expires_at=(now + expires_at_delta) if expires_at_delta is not None else None,
    )
    assert derive_status(ann, now) == expected
