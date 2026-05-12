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


def measurement_to_timeline_item(m) -> dict:
    parts = []
    if m.height_cm is not None:
        parts.append(f"身高 {m.height_cm} cm")
    if m.weight_kg is not None:
        parts.append(f"體重 {m.weight_kg} kg")
    if m.head_circumference_cm is not None:
        parts.append(f"頭圍 {m.head_circumference_cm} cm")
    if m.vision_left is not None or m.vision_right is not None:
        left = m.vision_left if m.vision_left is not None else "—"
        right = m.vision_right if m.vision_right is not None else "—"
        parts.append(f"視力 L{left}/R{right}")
    summary = "、".join(parts) if parts else (m.note or "")
    title = parts[0] if parts else "量測紀錄"
    return {
        "id": f"measurement-{m.id}",
        "type": "measurement",
        "occurred_at": _date_iso(m.measured_on),
        "title": title,
        "summary": _truncate(summary),
        "icon": SOURCE_ICONS["measurement"],
        "is_highlight": False,
        "raw_ref": {"router": "measurements", "id": m.id},
        "extra": {
            "height_cm": str(m.height_cm) if m.height_cm is not None else None,
            "weight_kg": str(m.weight_kg) if m.weight_kg is not None else None,
        },
    }


def observation_to_timeline_item(o) -> dict:
    return {
        "id": f"observation-{o.id}",
        "type": "observation",
        "occurred_at": _date_iso(o.observation_date),
        "title": f"觀察記錄（{o.domain}）" if o.domain else "教師觀察",
        "summary": _truncate(o.narrative),
        "icon": SOURCE_ICONS["observation"],
        "is_highlight": bool(o.is_highlight),
        "raw_ref": {"router": "observations", "id": o.id},
        "extra": {
            "domain": o.domain,
            "rating": o.rating,
        },
    }


def assessment_to_timeline_item(a) -> dict:
    domain = getattr(a, "domain", None)
    content = getattr(a, "content", None) or getattr(a, "comment", None) or ""
    return {
        "id": f"assessment-{a.id}",
        "type": "assessment",
        "occurred_at": _date_iso(a.assessment_date),
        "title": f"學期評量（{domain}）" if domain else "學期評量",
        "summary": _truncate(content),
        "icon": SOURCE_ICONS["assessment"],
        "is_highlight": False,
        "raw_ref": {"router": "assessments", "id": a.id},
        "extra": {
            "domain": domain,
            "rating": getattr(a, "rating", None),
        },
    }


def incident_to_timeline_item(i) -> dict:
    occurred = (
        getattr(i, "occurred_at", None)
        or getattr(i, "incident_date", None)
        or getattr(i, "created_at", None)
    )
    return {
        "id": f"incident-{i.id}",
        "type": "incident",
        "occurred_at": _date_iso(occurred),
        "title": getattr(i, "incident_type", None)
        or getattr(i, "title", None)
        or "事件記錄",
        "summary": _truncate(getattr(i, "description", None)),
        "icon": SOURCE_ICONS["incident"],
        "is_highlight": False,
        "raw_ref": {"router": "incidents", "id": i.id},
        "extra": {"severity": getattr(i, "severity", None)},
    }


def communication_to_timeline_item(c) -> dict:
    return {
        "id": f"communication-{c.id}",
        "type": "communication",
        "occurred_at": _date_iso(
            getattr(c, "communication_date", None) or getattr(c, "created_at", None)
        ),
        "title": getattr(c, "topic", None) or getattr(c, "subject", None) or "親師溝通",
        "summary": _truncate(
            getattr(c, "content", None) or getattr(c, "message", None)
        ),
        "icon": SOURCE_ICONS["communication"],
        "is_highlight": False,
        "raw_ref": {"router": "communications", "id": c.id},
        "extra": {"communication_type": getattr(c, "communication_type", None)},
    }


def contact_book_to_timeline_item(e) -> dict:
    summary = (
        getattr(e, "teacher_note", None)
        or getattr(e, "learning_highlight", None)
        or getattr(e, "teacher_summary", None)
        or getattr(e, "parent_message", None)
        or ""
    )
    return {
        "id": f"contact_book-{e.id}",
        "type": "contact_book",
        "occurred_at": _date_iso(
            getattr(e, "log_date", None) or getattr(e, "entry_date", None)
        ),
        "title": "聯絡簿",
        "summary": _truncate(summary),
        "icon": SOURCE_ICONS["contact_book"],
        "is_highlight": False,
        "raw_ref": {"router": "contact_book", "id": e.id},
        "extra": {},
    }


def attendance_to_timeline_item(a) -> dict:
    status_labels = {
        "缺席": "缺席",
        "病假": "病假",
        "事假": "事假",
        "遲到": "遲到",
        "absent": "缺席",
        "late": "遲到",
        "leave": "請假",
        "sick": "病假",
        "early_leave": "早退",
    }
    status = getattr(a, "status", "") or ""
    return {
        "id": f"attendance-{a.id}",
        "type": "attendance",
        "occurred_at": _date_iso(a.date),
        "title": status_labels.get(status, status or "出勤異常"),
        "summary": "",
        "icon": SOURCE_ICONS["attendance"],
        "is_highlight": False,
        "raw_ref": {"router": "attendance", "id": a.id},
        "extra": {"status": status},
    }


def activity_to_timeline_item(r) -> dict:
    return {
        "id": f"activity-{r.id}",
        "type": "activity",
        "occurred_at": _date_iso(
            getattr(r, "registered_at", None) or getattr(r, "created_at", None)
        ),
        "title": f"報名活動 #{r.id}",  # v1 不 join course name
        "summary": "",
        "icon": SOURCE_ICONS["activity"],
        "is_highlight": False,
        "raw_ref": {"router": "activity-registrations", "id": r.id},
        "extra": {},
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
