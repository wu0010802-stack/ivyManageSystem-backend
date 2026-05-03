"""今日工作台純函式：時段定義、任務歸屬、sticky_next。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal, Optional

SlotId = Literal["morning", "forenoon", "noon", "afternoon"]


@dataclass(frozen=True)
class SlotDef:
    slot_id: SlotId
    label: str
    start: time
    end: time


SLOT_DEFINITIONS: tuple[SlotDef, ...] = (
    SlotDef("morning", "早晨", time(7, 0), time(9, 0)),
    SlotDef("forenoon", "上午", time(9, 0), time(12, 0)),
    SlotDef("noon", "午間", time(12, 0), time(14, 0)),
    SlotDef("afternoon", "下午", time(14, 0), time(18, 0)),
)


def classify_time_to_slot(t: time) -> SlotId:
    """把一個 time 歸到 4 段中的某一段。早於 07:00 → morning，晚於 18:00 → afternoon。"""
    if t < SLOT_DEFINITIONS[0].start:
        return "morning"
    for sd in SLOT_DEFINITIONS:
        if sd.start <= t < sd.end:
            return sd.slot_id
    return "afternoon"


def pick_sticky_next(candidates: list[dict], now: datetime) -> Optional[dict]:
    """從待辦候選中挑「下一件最近且未過期」。candidates 每筆需有 'due_at' 欄位。"""
    future = [c for c in candidates if c.get("due_at") and c["due_at"] >= now]
    if not future:
        return None
    return min(future, key=lambda c: c["due_at"])
