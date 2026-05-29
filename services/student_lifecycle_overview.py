"""services/student_lifecycle_overview.py — 學生在校歷程聚合（read-only）。

純函式集中（compute_*），不依賴 DB session；
build_lifecycle_overview() 是 orchestrator，由 API 層呼叫。

See: docs/superpowers/specs/2026-05-29-student-lifecycle-tracking-panel-design.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional

StepStatus = Literal["done", "current", "future"]
GradeStepStatus = Literal["done", "current", "future", "skipped"]
TerminalKind = Literal["graduated", "withdrawn", "transferred", "none"]
OuterKey = Literal["visited", "deposited", "enrolled", "active", "terminal"]


@dataclass
class StepInfo:
    key: OuterKey
    label: str
    status: StepStatus
    occurred_at: Optional[date] = None
    meta: Optional[dict] = None


@dataclass
class GradeStepInfo:
    grade_id: int
    name: str
    sort_order: int
    status: GradeStepStatus
    entered_at: Optional[date] = None
    expected_at: Optional[date] = None
    classroom_name: Optional[str] = None


@dataclass
class TerminalInfo:
    kind: TerminalKind
    actual_date: Optional[date] = None
    expected_date: Optional[date] = None


@dataclass
class LifecycleOverview:
    student_id: int
    current_stage: str
    on_leave_badge: bool
    on_leave_since: Optional[date]
    outer_steps: list[StepInfo] = field(default_factory=list)
    inner_grade_steps: list[GradeStepInfo] = field(default_factory=list)
    terminal: TerminalInfo = field(default_factory=lambda: TerminalInfo(kind="none"))


# 中文標籤
_OUTER_LABELS: dict[OuterKey, str] = {
    "visited": "參觀",
    "deposited": "預繳",
    "enrolled": "報到",
    "active": "在學",
    "terminal": "終態",
}


def compute_outer_steps(
    student,
    funnel_events: list,
    change_logs: list,
) -> list[StepInfo]:
    # 找 funnel events 中各 to_stage 的最早時間
    def _earliest_funnel(stage: str) -> Optional[date]:
        candidates = [
            fe.created_at.date() if hasattr(fe.created_at, "date") else fe.created_at
            for fe in funnel_events
            if fe.to_stage == stage
        ]
        return min(candidates) if candidates else None

    visited_at = _earliest_funnel("visited")
    deposited_at = _earliest_funnel("deposited")
    # 報到：to_stage="enrolled" 或 event_type="converted"
    enrolled_at = _earliest_funnel("enrolled")
    if enrolled_at is None and student.enrollment_date:
        enrolled_at = student.enrollment_date

    # 在學：to_stage="active" 或 lifecycle_status 已 active 用 enrollment_date 兜底
    active_at = _earliest_funnel("active")
    if active_at is None and student.lifecycle_status in (
        "active",
        "on_leave",
        "graduated",
        "withdrawn",
        "transferred",
    ):
        active_at = student.enrollment_date

    # 當前 lifecycle 決定哪一點 current / done
    current = student.lifecycle_status
    terminal_kinds = {"graduated", "withdrawn", "transferred"}

    def _status_for(key: OuterKey) -> StepStatus:
        # 從前段往後判斷
        if key == "visited":
            if visited_at:
                return "done" if current != "prospect" or deposited_at else "current"
            return "future"
        if key == "deposited":
            if deposited_at:
                return (
                    "done" if current not in ("prospect",) or enrolled_at else "current"
                )
            return "future"
        if key == "enrolled":
            if enrolled_at:
                return "done" if current not in ("prospect", "enrolled") else "current"
            return "future"
        if key == "active":
            if active_at is None:
                return "future"
            if current in terminal_kinds:
                return "done"
            return "current"
        # terminal
        if current in terminal_kinds:
            return "done"
        return "future"

    terminal_at: Optional[date] = None
    if current == "graduated":
        terminal_at = student.graduation_date
    elif current in ("withdrawn", "transferred"):
        terminal_at = student.withdrawal_date

    return [
        StepInfo(
            key="visited",
            label=_OUTER_LABELS["visited"],
            status=_status_for("visited"),
            occurred_at=visited_at,
        ),
        StepInfo(
            key="deposited",
            label=_OUTER_LABELS["deposited"],
            status=_status_for("deposited"),
            occurred_at=deposited_at,
        ),
        StepInfo(
            key="enrolled",
            label=_OUTER_LABELS["enrolled"],
            status=_status_for("enrolled"),
            occurred_at=enrolled_at,
        ),
        StepInfo(
            key="active",
            label=_OUTER_LABELS["active"],
            status=_status_for("active"),
            occurred_at=active_at,
        ),
        StepInfo(
            key="terminal",
            label=_OUTER_LABELS["terminal"],
            status=_status_for("terminal"),
            occurred_at=terminal_at,
        ),
    ]


def compute_inner_grade_steps(
    student,
    all_grades: list,  # 已 filter is_active=true，已 sort by sort_order
    transfers: list,  # list[StudentClassroomTransfer]，可空
    classroom_grade_map: dict[int, int],  # classroom_id → grade_id
    classroom_name_map: dict[int, str],
) -> list[GradeStepInfo]:
    if not all_grades:
        return []

    # 依 transfers 找出「曾進入過的 grade」與其最早日期
    grade_entered: dict[int, date] = {}
    grade_classroom_name: dict[int, str] = {}
    for tr in sorted(transfers, key=lambda t: t.transferred_at):
        gid = classroom_grade_map.get(tr.to_classroom_id)
        if gid is None:
            continue
        d = (
            tr.transferred_at.date()
            if hasattr(tr.transferred_at, "date")
            else tr.transferred_at
        )
        if gid not in grade_entered or d < grade_entered[gid]:
            grade_entered[gid] = d
        grade_classroom_name[gid] = classroom_name_map.get(
            tr.to_classroom_id
        ) or grade_classroom_name.get(gid)

    # 推當前年級：student.classroom_id → grade_id
    current_grade_id: Optional[int] = None
    if student.classroom_id is not None:
        current_grade_id = classroom_grade_map.get(student.classroom_id)
    # 若 transfers 有但 student.classroom_id 對不到，用最晚的 transfer 推
    if current_grade_id is None and transfers:
        latest = max(transfers, key=lambda t: t.transferred_at)
        current_grade_id = classroom_grade_map.get(latest.to_classroom_id)

    # 入學年級：transfer 中最早的 grade；若無 transfer，用 current_grade_id 兜底
    if grade_entered:
        first_grade_id = min(
            grade_entered.keys(),
            key=lambda gid: next(g.sort_order for g in all_grades if g.id == gid),
        )
    else:
        first_grade_id = current_grade_id

    # current_grade 的 fallback entered_at = student.enrollment_date
    if (
        current_grade_id is not None
        and current_grade_id not in grade_entered
        and student.enrollment_date
    ):
        grade_entered[current_grade_id] = student.enrollment_date
        if student.classroom_id is not None:
            grade_classroom_name[current_grade_id] = classroom_name_map.get(
                student.classroom_id
            )

    first_sort = (
        next((g.sort_order for g in all_grades if g.id == first_grade_id), None)
        if first_grade_id
        else None
    )
    current_sort = (
        next((g.sort_order for g in all_grades if g.id == current_grade_id), None)
        if current_grade_id
        else None
    )

    steps: list[GradeStepInfo] = []
    for g in all_grades:
        if current_sort is None:
            status: GradeStepStatus = "future"
        elif first_sort is not None and g.sort_order < first_sort:
            status = "skipped"
        elif g.sort_order > current_sort:
            status = "future"
        elif g.sort_order == current_sort:
            status = "current"
        else:
            # 介於 first 與 current 之間
            status = "done" if g.id in grade_entered else "skipped"
        steps.append(
            GradeStepInfo(
                grade_id=g.id,
                name=g.name,
                sort_order=g.sort_order,
                status=status,
                entered_at=grade_entered.get(g.id),
                classroom_name=grade_classroom_name.get(g.id),
            )
        )
    return steps
