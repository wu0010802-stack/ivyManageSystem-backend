"""管理端行事曆跨模組聚合 endpoint。

設計：
- 各 layer 由 `_fetch_<layer>(session, from_, to, current_user) -> list[CalendarFeedItem]` 提供
- 主 endpoint 純編排：驗證 window → 過濾 layers → 收集 → 排序 → 回傳
- 權限濾在每個 fetcher 入口（無權限直接 return []），避免越權外洩
"""

from datetime import date, datetime, time
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models.activity import ActivityCourse, ActivitySession
from models.appraisal import AppraisalCycle
from models.base import get_session_dep
from models.employee import Employee
from models.event import Holiday, MeetingRecord, SchoolEvent, WorkdayOverride
from models.approval import ApprovalStatus
from models.leave import LeaveRecord
from schemas.calendar_admin import CalendarFeedItem, CalendarFeedResponse
from utils.auth import get_current_user
from utils.calendar_colors import (
    ALL_LAYERS,
    APPRAISAL_MILESTONE_LABELS,
    LAYER_COLORS,
    MEETING_TYPE_LABELS,
)
from utils.permissions import Permission, has_permission
from utils.recurrence import expand_event

router = APIRouter(tags=["calendar-admin"])

MAX_WINDOW_DAYS = 90

LAYER_FETCHERS: dict[
    str, Callable[[Session, date, date, dict], list[CalendarFeedItem]]
] = {}


def _fetch_event(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permission_names"), Permission.CALENDAR):
        return []
    stmt = (
        select(
            SchoolEvent.id,
            SchoolEvent.title,
            SchoolEvent.event_date,
            SchoolEvent.end_date,
            SchoolEvent.start_time,
            SchoolEvent.end_time,
            SchoolEvent.requires_acknowledgment,
            SchoolEvent.event_type,
            SchoolEvent.recurrence_rule,
        )
        .where(SchoolEvent.is_active.is_(True))
        .where(
            or_(
                # 非重複：原 overlap
                SchoolEvent.recurrence_rule.is_(None)
                & (SchoolEvent.event_date <= to)
                & or_(
                    SchoolEvent.end_date.is_(None) & (SchoolEvent.event_date >= from_),
                    SchoolEvent.end_date.is_not(None) & (SchoolEvent.end_date >= from_),
                ),
                # 重複：source event_date <= to；until check 留給 Python expander
                SchoolEvent.recurrence_rule.is_not(None)
                & (SchoolEvent.event_date <= to),
            )
        )
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        color = (
            LAYER_COLORS["event"]["ack"]
            if r.requires_acknowledgment
            else LAYER_COLORS["event"]["default"]
        )
        occurrences = expand_event(
            r.event_date,
            r.end_date,
            r.recurrence_rule,
            from_,
            to,
        )
        # Phase B：start_time + end_time 都有值才轉 datetime（時段事件）；
        # 否則維持 Phase A all-day 行為。time.fromisoformat 接 "HH:MM"。
        has_time = bool(r.start_time and r.end_time)
        for occ_start, occ_end in occurrences:
            item_id = f"{r.id}@{occ_start.isoformat()}" if r.recurrence_rule else r.id
            if has_time:
                start_val: date | datetime = datetime.combine(
                    occ_start, time.fromisoformat(r.start_time)
                )
                end_val: date | datetime = datetime.combine(
                    occ_end, time.fromisoformat(r.end_time)
                )
                all_day_val = False
            else:
                start_val = occ_start
                end_val = occ_end
                all_day_val = True
            out.append(
                CalendarFeedItem(
                    layer="event",
                    id=item_id,
                    title=r.title,
                    start=start_val,
                    end=end_val,
                    all_day=all_day_val,
                    color=color,
                    link=f"/calendar?eventId={r.id}",
                    meta={
                        "event_type": r.event_type,
                        "requires_acknowledgment": r.requires_acknowledgment,
                        "is_recurring": r.recurrence_rule is not None,
                    },
                )
            )
    return out


LAYER_FETCHERS["event"] = _fetch_event


def _fetch_holiday(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permission_names"), Permission.CALENDAR):
        return []

    holiday_rows = session.execute(
        select(Holiday.date, Holiday.name).where(Holiday.date.between(from_, to))
    ).all()
    override_rows = session.execute(
        select(WorkdayOverride.date, WorkdayOverride.name).where(
            WorkdayOverride.date.between(from_, to)
        )
    ).all()

    out: list[CalendarFeedItem] = []
    for r in holiday_rows:
        out.append(
            CalendarFeedItem(
                layer="holiday",
                id=f"holiday:{r.date.isoformat()}",
                title=r.name,
                start=r.date,
                end=r.date,
                color=LAYER_COLORS["holiday"]["default"],
                link=None,
                meta={"kind": "holiday"},
            )
        )
    for r in override_rows:
        out.append(
            CalendarFeedItem(
                layer="holiday",
                id=f"workday_override:{r.date.isoformat()}",
                title=r.name,
                start=r.date,
                end=r.date,
                color=LAYER_COLORS["holiday"]["workday_override"],
                link=None,
                meta={"kind": "workday_override"},
            )
        )
    return out


LAYER_FETCHERS["holiday"] = _fetch_holiday


def _fetch_leave(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permission_names"), Permission.LEAVES_READ):
        return []
    stmt = (
        select(
            LeaveRecord.id,
            LeaveRecord.start_date,
            LeaveRecord.end_date,
            LeaveRecord.leave_type,
            LeaveRecord.status,
            Employee.name.label("employee_name"),
        )
        .join(Employee, Employee.id == LeaveRecord.employee_id)
        .where(LeaveRecord.start_date <= to)
        .where(LeaveRecord.end_date >= from_)
        .where(
            or_(
                LeaveRecord.status == ApprovalStatus.PENDING.value,
                LeaveRecord.status == ApprovalStatus.APPROVED.value,
            )
        )
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        is_pending = r.status == ApprovalStatus.PENDING.value
        color = LAYER_COLORS["leave"]["pending" if is_pending else "default"]
        out.append(
            CalendarFeedItem(
                layer="leave",
                id=r.id,
                title=f"{r.employee_name} {r.leave_type}",
                start=r.start_date,
                end=r.end_date,
                color=color,
                link=f"/leaves?id={r.id}",
                meta={
                    "status": "pending" if is_pending else "approved",
                    "leave_type": r.leave_type,
                },
            )
        )
    return out


LAYER_FETCHERS["leave"] = _fetch_leave


def _fetch_activity(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    """課後才藝場次層。

    session_no 取「該課程內依 session_date 升序之全域序號」（不是 window 內序號），
    以維持「第 N 堂」對使用者的穩定語意；用 SQL window function 一次算出，再以
    outer where 過濾 date window 與課程 JOIN — 單 query、無 N+1。
    """
    if not has_permission(
        current_user.get("permission_names"), Permission.ACTIVITY_READ
    ):
        return []

    session_no_col = (
        func.row_number()
        .over(
            partition_by=ActivitySession.course_id,
            order_by=ActivitySession.session_date,
        )
        .label("session_no")
    )
    numbered = select(
        ActivitySession.id.label("id"),
        ActivitySession.course_id.label("course_id"),
        ActivitySession.session_date.label("session_date"),
        session_no_col,
    ).subquery()

    stmt = (
        select(
            numbered.c.id,
            numbered.c.course_id,
            numbered.c.session_date,
            numbered.c.session_no,
            ActivityCourse.name.label("course_name"),
        )
        .join(ActivityCourse, ActivityCourse.id == numbered.c.course_id)
        .where(numbered.c.session_date.between(from_, to))
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        out.append(
            CalendarFeedItem(
                layer="activity",
                id=r.id,
                title=f"{r.course_name} 第{r.session_no}堂",
                start=r.session_date,
                end=r.session_date,
                color=LAYER_COLORS["activity"]["default"],
                link=f"/activity?courseId={r.course_id}",
                meta={"course_id": r.course_id, "session_no": r.session_no},
            )
        )
    return out


LAYER_FETCHERS["activity"] = _fetch_activity


def _fetch_appraisal(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    """考核週期里程碑層。

    一個 AppraisalCycle 拆 start_date / end_date / base_score_calc_date 三筆，
    僅落 window 內者下發；id 用 `{cycle_id}:{milestone}` 區分。
    """
    if not has_permission(
        current_user.get("permission_names"), Permission.APPRAISAL_READ
    ):
        return []
    # 三日期任一落 window 都要拉 cycle
    stmt = select(
        AppraisalCycle.id,
        AppraisalCycle.academic_year,
        AppraisalCycle.semester,
        AppraisalCycle.start_date,
        AppraisalCycle.end_date,
        AppraisalCycle.base_score_calc_date,
    ).where(
        AppraisalCycle.start_date.between(from_, to)
        | AppraisalCycle.end_date.between(from_, to)
        | AppraisalCycle.base_score_calc_date.between(from_, to)
    )
    # Semester enum FIRST/SECOND → 上/下 對齊 api/student_enrollment.py 慣例
    SEMESTER_LABEL = {"FIRST": "上", "SECOND": "下"}
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        sem_raw = r.semester.value if hasattr(r.semester, "value") else r.semester
        sem_label = SEMESTER_LABEL.get(str(sem_raw), str(sem_raw))
        cycle_title = f"{r.academic_year} 學年度 {sem_label}學期"
        for milestone in ("start_date", "end_date", "base_score_calc_date"):
            d = getattr(r, milestone)
            if not (from_ <= d <= to):
                continue
            label = APPRAISAL_MILESTONE_LABELS[milestone]
            out.append(
                CalendarFeedItem(
                    layer="appraisal",
                    id=f"{r.id}:{milestone}",
                    title=f"{cycle_title} {label}",
                    start=d,
                    end=d,
                    color=LAYER_COLORS["appraisal"]["default"],
                    link=f"/appraisal?cycleId={r.id}",
                    meta={"cycle_id": r.id, "milestone": milestone},
                )
            )
    return out


LAYER_FETCHERS["appraisal"] = _fetch_appraisal


def _fetch_meeting(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    """園務會議層：DISTINCT (date, type) 聚合，多員工出席同場只下發一筆。"""
    if not has_permission(current_user.get("permission_names"), Permission.MEETINGS):
        return []
    stmt = (
        select(MeetingRecord.meeting_date, MeetingRecord.meeting_type)
        .where(MeetingRecord.meeting_date.between(from_, to))
        .distinct()
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        label = MEETING_TYPE_LABELS.get(r.meeting_type, r.meeting_type)
        out.append(
            CalendarFeedItem(
                layer="meeting",
                id=f"{r.meeting_type}:{r.meeting_date.isoformat()}",
                title=label,
                start=r.meeting_date,
                end=r.meeting_date,
                color=LAYER_COLORS["meeting"]["default"],
                link=f"/meetings?date={r.meeting_date.isoformat()}",
                meta={"meeting_type": r.meeting_type},
            )
        )
    return out


LAYER_FETCHERS["meeting"] = _fetch_meeting


@router.get("/admin_feed", response_model=CalendarFeedResponse)
def get_admin_feed(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    layers: str | None = Query(None),
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(get_current_user),
) -> CalendarFeedResponse:
    if to < from_:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (to - from_).days > MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=422, detail=f"window exceeds {MAX_WINDOW_DAYS} days"
        )

    if layers is None:
        requested = ALL_LAYERS
    else:
        requested = {x.strip() for x in layers.split(",") if x.strip()} & ALL_LAYERS

    items: list[CalendarFeedItem] = []
    for layer in requested:
        fetcher = LAYER_FETCHERS.get(layer)
        if fetcher is None:
            continue
        items.extend(fetcher(session, from_, to, current_user))

    items.sort(key=lambda x: (x.start, x.layer, str(x.id)))
    return CalendarFeedResponse(from_=from_, to=to, items=items)
