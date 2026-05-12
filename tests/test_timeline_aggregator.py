"""Unit tests for timeline_aggregator pure functions."""

from __future__ import annotations

from datetime import date

import pytest

from services.timeline_aggregator import (
    decode_cursor,
    encode_cursor,
    measurement_to_timeline_item,
    milestone_to_timeline_item,
    observation_to_timeline_item,
    sort_and_paginate,
)


def test_encode_decode_cursor_roundtrip():
    cursor = encode_cursor(occurred_at="2026-05-10", type_="observation", id_=1234)
    decoded = decode_cursor(cursor)
    assert decoded == {
        "last_occurred_at": "2026-05-10",
        "last_type": "observation",
        "last_id": 1234,
    }


def test_decode_invalid_cursor_returns_none():
    assert decode_cursor("not-a-cursor") is None
    assert decode_cursor("") is None
    assert decode_cursor(None) is None


def test_milestone_to_timeline_item_minimal():
    class _M:
        id = 5
        milestone_type = "birthday"
        achieved_on = date(2026, 5, 10)
        title = "5 歲生日"
        description = None
        icon = "🎂"
        is_highlight = False

    item = milestone_to_timeline_item(_M())
    assert item["id"] == "milestone-5"
    assert item["type"] == "milestone"
    assert item["occurred_at"] == "2026-05-10"
    assert item["title"] == "5 歲生日"
    assert item["icon"] == "🎂"
    assert item["raw_ref"] == {"router": "milestones", "id": 5}


def test_sort_and_paginate_orders_desc():
    items = [
        {"occurred_at": "2026-05-10", "type": "a", "id": "a-1"},
        {"occurred_at": "2026-05-12", "type": "b", "id": "b-1"},
        {"occurred_at": "2026-05-11", "type": "c", "id": "c-1"},
    ]
    out = sort_and_paginate(items, limit=2)
    assert [i["occurred_at"] for i in out["items"]] == ["2026-05-12", "2026-05-11"]
    assert out["next_cursor"] is not None
    decoded = decode_cursor(out["next_cursor"])
    assert decoded["last_occurred_at"] == "2026-05-11"


def test_sort_and_paginate_no_next_cursor_when_fewer_than_limit():
    items = [
        {"occurred_at": "2026-05-10", "type": "a", "id": "a-1"},
    ]
    out = sort_and_paginate(items, limit=10)
    assert out["next_cursor"] is None
    assert len(out["items"]) == 1


def test_measurement_to_timeline_item():
    from datetime import date
    from services.timeline_aggregator import measurement_to_timeline_item

    class _M:
        id = 7
        measured_on = date(2026, 5, 1)
        height_cm = 110.5
        weight_kg = 18.2
        head_circumference_cm = None
        vision_left = None
        vision_right = None
        note = None

    item = measurement_to_timeline_item(_M())
    assert item["id"] == "measurement-7"
    assert item["type"] == "measurement"
    assert item["occurred_at"] == "2026-05-01"
    assert "110.5" in item["title"] or "110.5" in item["summary"]
    assert item["raw_ref"] == {"router": "measurements", "id": 7}


def test_observation_to_timeline_item():
    from datetime import date
    from services.timeline_aggregator import observation_to_timeline_item

    class _O:
        id = 9
        observation_date = date(2026, 5, 5)
        narrative = "今天小明很開心地完成了積木挑戰"
        domain = "認知"
        rating = 4
        is_highlight = True

    item = observation_to_timeline_item(_O())
    assert item["id"] == "observation-9"
    assert item["type"] == "observation"
    assert item["is_highlight"] is True
    assert item["extra"]["domain"] == "認知"
    assert item["extra"]["rating"] == 4
