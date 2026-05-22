"""services/recruitment_funnel.py — 招生漏斗狀態機 + 寫入入口。

純函式（derive_stage / can_transition / is_destructive）位於檔頭，
orchestrator `transition_visit()` 在後續 task 補。
"""

from __future__ import annotations

from typing import Literal, Optional, Protocol

Stage = Literal["visited", "deposited", "enrolled", "active"]
STAGES: tuple[Stage, ...] = ("visited", "deposited", "enrolled", "active")


class _VisitLike(Protocol):
    has_deposit: bool


class _StudentLike(Protocol):
    lifecycle_status: str


def derive_stage(visit: _VisitLike, student: Optional[_StudentLike]) -> Stage:
    """從 (visit, student) 推導 4 階段。

    規則：student 存在性優先（avoid dual source of truth）。
    """
    if student is not None:
        return "active" if student.lifecycle_status == "active" else "enrolled"
    return "deposited" if visit.has_deposit else "visited"


def can_transition(from_stage: Stage, to_stage: Stage) -> bool:
    """Phase A：任意拖。保留位置給未來收緊規則（例：禁止跨多段躍進）。"""
    return True


_DESTRUCTIVE_FROM: frozenset[Stage] = frozenset({"enrolled", "active"})


def is_destructive(from_stage: Stage, to_stage: Stage) -> bool:
    """destructive = 從 enrolled/active 退回任何前段。"""
    if from_stage not in _DESTRUCTIVE_FROM:
        return False
    order = {s: i for i, s in enumerate(STAGES)}
    return order[to_stage] < order[from_stage]
