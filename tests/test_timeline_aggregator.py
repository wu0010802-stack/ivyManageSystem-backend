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


def test_assessment_to_timeline_item():
    from datetime import date

    from services.timeline_aggregator import assessment_to_timeline_item

    class _A:
        id = 11
        assessment_date = date(2026, 4, 1)
        domain = "認知"
        rating = "優"  # 實際 model 為 String（優/良/需加強）
        content = "進步明顯"  # 實際 model 欄位為 content，非 comment

    item = assessment_to_timeline_item(_A())
    assert item["id"] == "assessment-11"
    assert item["type"] == "assessment"
    assert item["occurred_at"] == "2026-04-01"
    assert "認知" in item["title"]
    assert item["raw_ref"] == {"router": "assessments", "id": 11}
    assert item["extra"]["domain"] == "認知"
    assert item["extra"]["rating"] == "優"


def test_incident_to_timeline_item():
    from datetime import datetime

    from services.timeline_aggregator import incident_to_timeline_item

    class _I:
        id = 13
        occurred_at = datetime(2026, 5, 10, 14, 0)  # 實際 model 欄位為 occurred_at
        incident_type = "意外受傷"  # 實際 model 欄位為 incident_type，非 title
        description = "戶外活動時膝蓋擦傷"
        severity = "輕微"

    item = incident_to_timeline_item(_I())
    assert item["id"] == "incident-13"
    assert item["type"] == "incident"
    assert item["occurred_at"] == "2026-05-10T14:00:00"
    assert item["title"] == "意外受傷"
    assert item["extra"]["severity"] == "輕微"


def test_communication_to_timeline_item():
    from datetime import date

    from services.timeline_aggregator import communication_to_timeline_item

    class _C:
        id = 17
        communication_date = date(2026, 5, 8)
        topic = "詢問活動"  # 實際 model 欄位為 topic，非 subject
        content = "下週活動內容？"
        communication_type = "電話"

    item = communication_to_timeline_item(_C())
    assert item["id"] == "communication-17"
    assert item["type"] == "communication"
    assert item["occurred_at"] == "2026-05-08"
    assert item["title"] == "詢問活動"
    assert item["extra"]["communication_type"] == "電話"
