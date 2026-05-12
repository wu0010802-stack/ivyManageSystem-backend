"""Timeline aggregator pure functions — no DB / no FastAPI dependencies."""

from __future__ import annotations

import base64
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


SOURCE_ICONS = {
    "observation": "📝",
    "assessment": "📊",
    "incident": "⚠️",
    "communication": "💬",
    "contact_book": "📒",
    "attendance": "📅",
    "activity": "🎨",
    "milestone": "🏆",
    "measurement": "📏",
}

SOURCE_TYPES = tuple(SOURCE_ICONS.keys())


def encode_cursor(*, occurred_at: str, type_: str, id_: int) -> str:
    payload = {
        "last_occurred_at": occurred_at,
        "last_type": type_,
        "last_id": id_,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: Optional[str]) -> Optional[dict]:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        if (
            "last_occurred_at" not in data
            or "last_type" not in data
            or "last_id" not in data
        ):
            return None
        return data
    except Exception:
        return None


def _truncate(text: Optional[str], limit: int = 200) -> str:
    if not text:
        return ""
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _date_iso(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def milestone_to_timeline_item(m) -> dict:
    return {
        "id": f"milestone-{m.id}",
        "type": "milestone",
        "occurred_at": _date_iso(m.achieved_on),
        "title": m.title,
        "summary": _truncate(m.description) if m.description else "",
        "icon": m.icon or SOURCE_ICONS["milestone"],
        "is_highlight": False,
        "raw_ref": {"router": "milestones", "id": m.id},
        "extra": {"milestone_type": m.milestone_type},
    }


def sort_and_paginate(items: list[dict], *, limit: int = 30) -> dict:
    """合併多源排序後分頁。Sort key: (occurred_at desc, type asc, id desc)."""
    sorted_items = sorted(
        items,
        key=lambda x: (
            x.get("occurred_at", ""),
            x.get("type", ""),
            x.get("id", ""),
        ),
        reverse=True,
    )
    page = sorted_items[:limit]
    next_cursor = None
    if len(page) == limit:
        last = page[-1]
        try:
            raw_id = int(str(last["id"]).rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            raw_id = 0
        next_cursor = encode_cursor(
            occurred_at=last["occurred_at"],
            type_=last["type"],
            id_=raw_id,
        )
    return {"items": page, "next_cursor": next_cursor}
