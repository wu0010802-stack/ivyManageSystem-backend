"""api/parent_portal/calendar.py — 家長端本週/本月行程聚合。

把分散在公告 / 行事曆事件 / 繳費期限 / 聯絡簿 / 請假 / 用藥的時間點，
整合成「N 天 / 月」的時間軸。家長不必再各分頁掃時間，一目了然。

回傳結構（time-sorted）：
```
{
  "from": "2026-05-01",
  "to":   "2026-05-08",
  "items": [
    {
      "date": "...",
      "kind": "event"|"announcement"|"fee_due"|"holiday"|
              "contact_book"|"leave"|"medication",
      "title": "...",
      "subtitle": "...",
      "target_id": int | null,         # deep-link 主鍵
      "ref": {"type": "...", "id": ...}, # 舊欄位保留以維持向後相容
      ...
    }, ...
  ]
}
```

`category` 欄位仍保留 = `kind`（向後相容，舊版前端讀 category 不破）。

可選 query：student_id（限縮到單一子女）— 公告/事件/節日視為全家庭層級不過濾。

僅 GET，純讀取；個資隔離走 _get_parent_student_ids 並驗 student_id 屬此家長。
"""

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    SchoolEvent,
    Student,
    StudentContactBookEntry,
    StudentLeaveRequest,
    StudentMedicationOrder,
    get_session,
)
from models.fees import StudentFeeRecord
from utils.auth import require_parent_role

from ._shared import _get_parent_student_ids

router = APIRouter(prefix="/calendar", tags=["parent-calendar"])


_EVENT_TYPE_LABEL = {
    "meeting": "會議",
    "activity": "活動",
    "holiday": "假日",
    "general": "事件",
}

_LEAVE_TYPE_LABEL = {
    "sick": "病假",
    "personal": "事假",
    "other": "其他假",
}


def _make_item(
    *,
    item_date: date,
    kind: str,
    title: str,
    subtitle: str = "",
    target_id: Optional[int] = None,
    ref_type: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """統一建構一筆 timeline item，同時帶 kind / category / target_id / ref。"""
    item = {
        "date": item_date.isoformat(),
        "kind": kind,
        "category": kind,  # 舊欄位保留向後相容
        "title": title,
        "subtitle": subtitle,
        "target_id": target_id,
        "ref": {"type": ref_type or kind, "id": target_id},
    }
    if extra:
        item.update(extra)
    return item


def _aggregate_period(
    session,
    *,
    user_id: int,
    start: date,
    end: date,
    student_id: Optional[int] = None,
) -> list[dict]:
    """彙整 [start, end) 期間的所有家長端行程項目。

    包含：events / announcements / fee_due / contact_book / leave / medication。
    end 不含。當 student_id 指定時，把學生層級的事件（聯絡簿 / 請假 / 用藥 / 學費）
    限縮到該學生；事件 / 公告 / 節日仍是家庭層級。
    """
    items: list[dict] = []
    today = date.today()

    _, all_student_ids = _get_parent_student_ids(session, user_id)
    if student_id is not None:
        if student_id not in all_student_ids:
            raise HTTPException(status_code=403, detail="此學生不屬於您")
        target_student_ids = [student_id]
    else:
        target_student_ids = all_student_ids

    student_name_map: dict[int, str] = {}
    if target_student_ids:
        student_name_map = {
            s.id: s.name
            for s in session.query(Student.id, Student.name)
            .filter(Student.id.in_(target_student_ids))
            .all()
        }

    # 1) 行事曆事件（家庭層級，不依 student_id 過濾）
    events = (
        session.query(SchoolEvent)
        .filter(
            SchoolEvent.is_active == True,  # noqa: E712
            SchoolEvent.event_date < end,
        )
        .all()
    )
    for ev in events:
        start_d = ev.event_date
        end_d = ev.end_date or ev.event_date
        if end_d < start:
            continue
        display_d = start_d if start_d >= start else start
        subtitle = _EVENT_TYPE_LABEL.get(ev.event_type, ev.event_type or "")
        if ev.is_all_day is False and ev.start_time:
            subtitle = f"{subtitle}・{ev.start_time}"
        if ev.location:
            subtitle = f"{subtitle}・{ev.location}" if subtitle else ev.location
        kind = "holiday" if ev.event_type == "holiday" else "event"
        items.append(
            _make_item(
                item_date=display_d,
                kind=kind,
                title=ev.title,
                subtitle=subtitle,
                target_id=ev.id,
                ref_type="school_event",
                extra={
                    "requires_acknowledgment": bool(ev.requires_acknowledgment),
                },
            )
        )

    # 2) 繳費截止（學生層級）
    if target_student_ids:
        fees = (
            session.query(StudentFeeRecord)
            .filter(
                StudentFeeRecord.student_id.in_(target_student_ids),
                StudentFeeRecord.status.in_(("unpaid", "partial")),
                StudentFeeRecord.due_date.isnot(None),
                StudentFeeRecord.due_date >= start,
                StudentFeeRecord.due_date < end,
            )
            .all()
        )
        for f in fees:
            items.append(
                _make_item(
                    item_date=f.due_date,
                    kind="fee_due",
                    title=f"繳費截止：{f.fee_item_name or '學費'}",
                    subtitle=(
                        f"{f.student_name or ''} "
                        f"$ {(f.amount_due - f.amount_paid):,}"
                    ).strip(),
                    target_id=f.id,
                    ref_type="fee_record",
                )
            )

    # 3) 公告（家庭層級；scope='all' 才入）
    period_days = max((end - start).days, 1)
    cutoff = datetime.combine(start, datetime.min.time()) - timedelta(days=period_days)
    ann_rows = (
        session.query(Announcement)
        .join(
            AnnouncementParentRecipient,
            AnnouncementParentRecipient.announcement_id == Announcement.id,
        )
        .filter(Announcement.created_at >= cutoff)
        .order_by(Announcement.is_pinned.desc(), Announcement.created_at.desc())
        .distinct()
        .all()
    )
    if ann_rows:
        ann_ids_all_scope = {
            r.announcement_id
            for r in session.query(AnnouncementParentRecipient)
            .filter(
                AnnouncementParentRecipient.announcement_id.in_(
                    [a.id for a in ann_rows]
                ),
                AnnouncementParentRecipient.scope == "all",
            )
            .all()
        }
        for ann in ann_rows:
            if ann.id not in ann_ids_all_scope:
                continue
            d = ann.created_at.date() if ann.created_at else start
            if d < start:
                d = start
            if d >= end:
                continue
            items.append(
                _make_item(
                    item_date=d,
                    kind="announcement",
                    title=ann.title,
                    subtitle="公告" + ("・置頂" if ann.is_pinned else ""),
                    target_id=ann.id,
                    ref_type="announcement",
                    extra={"priority": ann.priority},
                )
            )

    # 4) 聯絡簿發布（學生層級）
    if target_student_ids:
        cb_rows = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.student_id.in_(target_student_ids),
                StudentContactBookEntry.deleted_at.is_(None),
                StudentContactBookEntry.published_at.isnot(None),
                StudentContactBookEntry.log_date >= start,
                StudentContactBookEntry.log_date < end,
            )
            .all()
        )
        for cb in cb_rows:
            sname = student_name_map.get(cb.student_id, "")
            preview = (cb.teacher_note or cb.learning_highlight or "").strip()
            if len(preview) > 30:
                preview = preview[:30] + "…"
            items.append(
                _make_item(
                    item_date=cb.log_date,
                    kind="contact_book",
                    title=f"{sname} 聯絡簿".strip() if sname else "聯絡簿",
                    subtitle=preview,
                    target_id=cb.id,
                    ref_type="contact_book_entry",
                    extra={"student_id": cb.student_id},
                )
            )

    # 5) 學生請假（依 start_date..end_date 展開到區間內每一天）
    if target_student_ids:
        leaves = (
            session.query(StudentLeaveRequest)
            .filter(
                StudentLeaveRequest.student_id.in_(target_student_ids),
                StudentLeaveRequest.status.in_(("pending", "approved")),
                StudentLeaveRequest.start_date < end,
                StudentLeaveRequest.end_date >= start,
            )
            .all()
        )
        for lv in leaves:
            sname = student_name_map.get(lv.student_id, "")
            label = _LEAVE_TYPE_LABEL.get(lv.leave_type, lv.leave_type or "請假")
            d = max(lv.start_date, start)
            stop = min(lv.end_date, end - timedelta(days=1))
            while d <= stop:
                items.append(
                    _make_item(
                        item_date=d,
                        kind="leave",
                        title=f"{sname} 請假" if sname else "學生請假",
                        subtitle=f"{label}・{lv.status}",
                        target_id=lv.id,
                        ref_type="student_leave",
                        extra={"student_id": lv.student_id},
                    )
                )
                d += timedelta(days=1)

    # 6) 用藥單（單日生效）
    if target_student_ids:
        meds = (
            session.query(StudentMedicationOrder)
            .filter(
                StudentMedicationOrder.student_id.in_(target_student_ids),
                StudentMedicationOrder.order_date >= start,
                StudentMedicationOrder.order_date < end,
            )
            .all()
        )
        for m in meds:
            sname = student_name_map.get(m.student_id, "")
            items.append(
                _make_item(
                    item_date=m.order_date,
                    kind="medication",
                    title=f"{sname} 用藥".strip() if sname else "用藥",
                    subtitle=m.medication_name or "",
                    target_id=m.id,
                    ref_type="medication_order",
                    extra={"student_id": m.student_id},
                )
            )

    items.sort(key=lambda x: (x["date"], x["kind"]))
    return items


@router.get("/week")
def get_week_agenda(
    days: int = Query(7, ge=1, le=14, description="從今日起算的天數，預設 7"),
    student_id: Optional[int] = Query(default=None, gt=0),
    current_user: dict = Depends(require_parent_role()),
):
    """整合本週聚合行程（events / announcements / fee_due / contact_book / leave / medication）。"""
    user_id = current_user["user_id"]
    today = date.today()
    end = today + timedelta(days=days)

    session = get_session()
    try:
        items = _aggregate_period(
            session,
            user_id=user_id,
            start=today,
            end=end,
            student_id=student_id,
        )
        return {
            "from": today.isoformat(),
            "to": end.isoformat(),
            "items": items,
        }
    finally:
        session.close()


@router.get("/month")
def get_month_agenda(
    year: int = Query(..., ge=2024, le=2100),
    month: int = Query(..., ge=1, le=12),
    student_id: Optional[int] = Query(default=None, gt=0),
    current_user: dict = Depends(require_parent_role()),
):
    """月份視圖：[year-month-01, 隔月-01) 區間內所有家長行程。"""
    user_id = current_user["user_id"]
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    session = get_session()
    try:
        items = _aggregate_period(
            session,
            user_id=user_id,
            start=start,
            end=end,
            student_id=student_id,
        )
        return {
            "year": year,
            "month": month,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "items": items,
        }
    finally:
        session.close()
