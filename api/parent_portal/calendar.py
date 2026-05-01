"""api/parent_portal/calendar.py — 家長端本週行程聚合。

把分散在公告 / 行事曆事件 / 繳費期限的時間點，整合成「未來 N 天的時間軸」。
家長不必再各分頁掃時間，一目了然。

回傳結構（time-sorted）：
```
{
  "from": "2026-05-01",
  "to":   "2026-05-08",
  "items": [
    {"date": "...", "category": "event"|"announcement"|"fee_due"|"holiday",
     "title": "...", "subtitle": "...", "ref": {"type": "...", "id": ...}},
    ...
  ]
}
```

僅 GET，純讀取；個資隔離仍走 _get_parent_student_ids（fee 用）。
"""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query

from models.database import (
    Announcement,
    AnnouncementParentRead,
    AnnouncementParentRecipient,
    Guardian,
    SchoolEvent,
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


@router.get("/week")
def get_week_agenda(
    days: int = Query(7, ge=1, le=14, description="從今日起算的天數，預設 7"),
    current_user: dict = Depends(require_parent_role()),
):
    """整合本週公告 / 行事曆事件 / 繳費截止。

    範圍：[today, today + days)，依 date asc 排序。

    - 行事曆事件（SchoolEvent）：event_date 或 [event_date, end_date] 與區間重疊
    - 公告（Announcement）：建立於最近 days 天內、家長可見（recipient scope）
    - 繳費期限（StudentFeeRecord）：due_date 落在區間內、本家庭子女、未繳清
    """
    user_id = current_user["user_id"]
    today = date.today()
    end = today + timedelta(days=days)

    session = get_session()
    try:
        items: list[dict] = []

        # 1) 行事曆事件
        events = (
            session.query(SchoolEvent)
            .filter(
                SchoolEvent.is_active == True,  # noqa: E712
                # 範圍重疊：event_date <= end 且 (end_date 或 event_date) >= today
                SchoolEvent.event_date < end,
            )
            .all()
        )
        for ev in events:
            start_d = ev.event_date
            end_d = ev.end_date or ev.event_date
            if end_d < today:
                continue
            # 顯示 from = max(start_d, today)
            display_d = start_d if start_d >= today else today
            subtitle = _EVENT_TYPE_LABEL.get(ev.event_type, ev.event_type or "")
            if ev.is_all_day is False and ev.start_time:
                subtitle = f"{subtitle}・{ev.start_time}"
            if ev.location:
                subtitle = f"{subtitle}・{ev.location}" if subtitle else ev.location
            items.append(
                {
                    "date": display_d.isoformat(),
                    "category": "event",
                    "title": ev.title,
                    "subtitle": subtitle,
                    "requires_acknowledgment": bool(ev.requires_acknowledgment),
                    "ref": {"type": "school_event", "id": ev.id},
                }
            )

        # 2) 繳費截止
        _, student_ids = _get_parent_student_ids(session, user_id)
        if student_ids:
            fees = (
                session.query(StudentFeeRecord)
                .filter(
                    StudentFeeRecord.student_id.in_(student_ids),
                    StudentFeeRecord.status.in_(("unpaid", "partial")),
                    StudentFeeRecord.due_date.isnot(None),
                    StudentFeeRecord.due_date >= today,
                    StudentFeeRecord.due_date < end,
                )
                .all()
            )
            for f in fees:
                items.append(
                    {
                        "date": f.due_date.isoformat(),
                        "category": "fee_due",
                        "title": f"繳費截止：{f.fee_item_name or '學費'}",
                        "subtitle": (
                            f"{f.student_name or ''} "
                            f"$ {(f.amount_due - f.amount_paid):,}"
                        ).strip(),
                        "ref": {"type": "fee_record", "id": f.id},
                    }
                )

        # 3) 近期公告（簡單列入：最近 days 天內建立、家長可見）
        cutoff = datetime.now() - timedelta(days=days)
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
        # 只保留「scope=all」的公告（family/classroom scope 過濾較複雜，先做 all 維持輕量；
        # family 細粒度過濾交由 announcements 列表頁負責）
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
            d = ann.created_at.date() if ann.created_at else today
            # 「本週行程」是未來向，過去 created 的公告不應排在 timeline 之前；
            # 統一把它們 pin 在 today（家長一打開即看到，但不會壓掉真正的未來行程順序）
            if d < today:
                d = today
            items.append(
                {
                    "date": d.isoformat(),
                    "category": "announcement",
                    "title": ann.title,
                    "subtitle": "公告" + ("・置頂" if ann.is_pinned else ""),
                    "priority": ann.priority,
                    "ref": {"type": "announcement", "id": ann.id},
                }
            )

        items.sort(key=lambda x: (x["date"], x["category"]))

        return {
            "from": today.isoformat(),
            "to": end.isoformat(),
            "items": items,
        }
    finally:
        session.close()
