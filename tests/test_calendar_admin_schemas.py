from datetime import date
import pytest
from pydantic import ValidationError
from schemas.calendar_admin import CalendarFeedItem, CalendarFeedResponse


def test_feed_item_minimal_valid():
    item = CalendarFeedItem(
        layer="event",
        id=42,
        title="家長會",
        start=date(2026, 5, 19),
        end=date(2026, 5, 19),
        all_day=True,
        color="#10b981",
        link="/calendar?eventId=42",
        meta={},
    )
    assert item.layer == "event"
    assert item.link == "/calendar?eventId=42"


def test_feed_item_unknown_layer_rejected():
    with pytest.raises(ValidationError):
        CalendarFeedItem(
            layer="totally_made_up",
            id=1,
            title="x",
            start=date(2026, 5, 19),
            end=date(2026, 5, 19),
            all_day=True,
            color="#000000",
            link=None,
            meta={},
        )


def test_feed_item_id_accepts_string():
    item = CalendarFeedItem(
        layer="holiday",
        id="2026-05-19",
        title="勞動節",
        start=date(2026, 5, 19),
        end=date(2026, 5, 19),
        all_day=True,
        color="#f59e0b",
        link=None,
        meta={},
    )
    assert item.id == "2026-05-19"


def test_feed_response_alias_from():
    """Pydantic alias `from` 序列化檢查。"""
    resp = CalendarFeedResponse(
        **{"from": date(2026, 5, 1)},
        to=date(2026, 5, 31),
        items=[],
    )
    payload = resp.model_dump(by_alias=True)
    assert "from" in payload
    assert payload["from"] == date(2026, 5, 1)
