"""services/student_lifecycle_overview.py — 學生在校歷程聚合（read-only）。

純函式集中（compute_*），不依賴 DB session；
build_lifecycle_overview() 是 orchestrator，由 API 層呼叫。

See: docs/superpowers/specs/2026-05-29-student-lifecycle-tracking-panel-design.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Literal, Optional

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


def compute_terminal(
    student,
    inner_grade_steps: list[GradeStepInfo],
    graduation_grade_sort_order: Optional[int],
    term_end_date_for: "Callable[[int], Optional[date]]",
) -> TerminalInfo:
    """推算終態。

    term_end_date_for(school_year) → 該學年下學期 end_date 或 None。
    """
    status = student.lifecycle_status
    if status == "graduated":
        return TerminalInfo(kind="graduated", actual_date=student.graduation_date)
    if status == "withdrawn":
        return TerminalInfo(kind="withdrawn", actual_date=student.withdrawal_date)
    if status == "transferred":
        return TerminalInfo(kind="transferred", actual_date=student.withdrawal_date)

    # 在學中（active / on_leave / enrolled / prospect）— 預測畢業日
    if graduation_grade_sort_order is None:
        return TerminalInfo(kind="none")

    current = next((s for s in inner_grade_steps if s.status == "current"), None)
    if current is None or current.entered_at is None:
        return TerminalInfo(kind="none")

    # 學年差 = 畢業年級 sort_order - 當前年級 sort_order
    diff = graduation_grade_sort_order - current.sort_order
    # 「進入當前年級的學年」= entered_at.year（若 entered_at 在 8 月之前則學年是去年的）
    entered_year = current.entered_at.year
    if current.entered_at.month < 8:
        entered_year -= 1
    expected_school_year = entered_year + diff

    # 優先用 AcademicTerm 的 end_date
    explicit = term_end_date_for(expected_school_year)
    if explicit is not None:
        return TerminalInfo(kind="none", expected_date=explicit)

    # 預設 7/31 of (expected_school_year + 1)
    return TerminalInfo(
        kind="none",
        expected_date=date(expected_school_year + 1, 7, 31),
    )


def build_lifecycle_overview(session, student_id: int) -> LifecycleOverview:
    """API entrypoint — 一次 query 完所有需要資料，丟給純函式。"""
    from models.classroom import Student, Classroom, ClassGrade
    from models.recruitment import RecruitmentEventLog
    from models.student_log import StudentChangeLog
    from models.student_transfer import StudentClassroomTransfer
    from models.academic_term import AcademicTerm

    student = session.query(Student).filter(Student.id == student_id).first()
    if student is None:
        raise ValueError(f"student not found: id={student_id}")

    funnel_events = (
        session.query(RecruitmentEventLog)
        .filter(RecruitmentEventLog.student_id == student_id)
        .order_by(RecruitmentEventLog.created_at.asc())
        .all()
    )
    # 早期 funnel events 可能 student_id IS NULL 但 visit_id 對應 — 補一筆 by visit_id
    if student.recruitment_visit_id:
        extra = (
            session.query(RecruitmentEventLog)
            .filter(
                RecruitmentEventLog.recruitment_visit_id
                == student.recruitment_visit_id,
                RecruitmentEventLog.student_id.is_(None),
            )
            .order_by(RecruitmentEventLog.created_at.asc())
            .all()
        )
        funnel_events = sorted(extra + funnel_events, key=lambda e: e.created_at)

    change_logs = (
        session.query(StudentChangeLog)
        .filter(StudentChangeLog.student_id == student_id)
        .order_by(StudentChangeLog.event_date.asc())
        .all()
    )

    transfers = (
        session.query(StudentClassroomTransfer)
        .filter(StudentClassroomTransfer.student_id == student_id)
        .order_by(StudentClassroomTransfer.transferred_at.asc())
        .all()
    )

    all_grades = (
        session.query(ClassGrade)
        .filter(ClassGrade.is_active == True)  # noqa: E712
        .order_by(ClassGrade.sort_order.asc())
        .all()
    )

    classroom_rows = session.query(
        Classroom.id, Classroom.grade_id, Classroom.name
    ).all()
    classroom_grade_map = {
        row[0]: row[1] for row in classroom_rows if row[1] is not None
    }
    classroom_name_map = {row[0]: row[2] for row in classroom_rows}

    outer = compute_outer_steps(student, funnel_events, change_logs)
    inner = compute_inner_grade_steps(
        student, all_grades, transfers, classroom_grade_map, classroom_name_map
    )

    grad_sort = next((g.sort_order for g in all_grades if g.is_graduation_grade), None)

    def term_end_date_for(school_year: int) -> Optional[date]:
        row = (
            session.query(AcademicTerm.end_date)
            .filter(
                AcademicTerm.school_year == school_year,
                AcademicTerm.semester == 2,
            )
            .first()
        )
        return row[0] if row else None

    terminal = compute_terminal(student, inner, grad_sort, term_end_date_for)

    on_leave_badge = student.lifecycle_status == "on_leave"
    on_leave_since: Optional[date] = None
    if on_leave_badge:
        latest_leave = (
            session.query(StudentChangeLog)
            .filter(
                StudentChangeLog.student_id == student_id,
                StudentChangeLog.event_type == "休學",
            )
            .order_by(StudentChangeLog.event_date.desc())
            .first()
        )
        on_leave_since = latest_leave.event_date if latest_leave else None

    return LifecycleOverview(
        student_id=student.id,
        current_stage=student.lifecycle_status,
        on_leave_badge=on_leave_badge,
        on_leave_since=on_leave_since,
        outer_steps=outer,
        inner_grade_steps=inner,
        terminal=terminal,
    )
