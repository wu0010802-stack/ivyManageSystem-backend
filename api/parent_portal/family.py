"""api/parent_portal/family.py — 家校樞紐頁 timeline 彙整端點。

把出勤 / 公告 / 聯絡簿 / 事件簽閱 / 用藥單 / 請假審核結果 6 種資料
合成單一時間軸，避免 /family 樞紐頁需打 6 支 API。

Perf：30s in-process TTLCache（key=(user_id, student_id, limit)）；
家長端 useCachedAsync 再加 60s 前端 cache，雙層緩衝。
"""

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from models.classroom import StudentAttendance
from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    AnnouncementParentRead,
    Classroom,
    EventAcknowledgment,
    Guardian,
    SchoolEvent,
    Student,
    StudentContactBookEntry,
)
from models.portfolio import StudentMedicationOrder
from models.student_leave import StudentLeaveRequest
from utils.audit import write_explicit_audit
from utils.auth import require_parent_role
from utils.cache_layer import get_cache

from ._dependencies import get_parent_db
from ._shared import _assert_student_owned, _get_parent_student_ids

router = APIRouter(prefix="/family", tags=["parent-family"])

# (user_id, student_id, limit) → timeline payload；30s TTL
# scope: user — key 內含 user_id，user 間天然隔離
_CACHE_NS_PARENT_FAMILY_TIMELINE = "parent_family_timeline"
_CACHE_TTL_PARENT_FAMILY_TIMELINE = 30  # 30 秒


@router.get("/timeline")
def family_timeline(
    request: Request,
    student_id: int = Query(..., ge=1),
    limit: int = Query(7, ge=1, le=50),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """單一子女最近 N 筆混合 timeline。

    回傳格式：
        [
            {
                "kind": "attendance" | "announcement" | "contact_book" |
                        "event_ack" | "medication" | "leave_review",
                "id": str,  # 形如 "attendance:1"，client 不解析，僅作 key
                "title": str,
                "subtitle": str | None,
                "occurred_at": str,  # ISO 8601
                "is_pending": bool,  # 待辦標紅點
                "href": str,  # 前端對應 route
            }, ...
        ]
    """
    user_id = current_user["user_id"]
    _assert_student_owned(session, user_id, student_id, for_write=False)

    # family timeline 跨 6 來源（出勤/公告/聯絡簿/事件簽閱/用藥/請假），entity_type
    # 暫歸 student（無更貼切類別）。dedup 防家長 30s cache miss 後反覆觸發。
    write_explicit_audit(
        request,
        action="READ",
        entity_type="student",
        entity_id=str(student_id),
        summary=f"家長查家校 timeline：student_id={student_id} limit={limit}",
        changes={"student_id": student_id, "limit": limit, "source": "family_timeline"},
        dedup=True,
    )

    cache_key = f"{user_id}:{student_id}:{limit}"
    cached = get_cache().get(_CACHE_NS_PARENT_FAMILY_TIMELINE, cache_key)
    if cached is not None:
        return cached

    items = _collect_timeline_items(session, user_id, student_id, limit)
    get_cache().set(
        _CACHE_NS_PARENT_FAMILY_TIMELINE,
        cache_key,
        items,
        ttl=_CACHE_TTL_PARENT_FAMILY_TIMELINE,
    )
    return items


def _collect_timeline_items(
    session, user_id: int, student_id: int, limit: int
) -> list[dict[str, Any]]:
    """彙整 6 種來源的最新事件，按 occurred_at desc 排序、limit 切片。

    各來源策略：
    - attendance：最近 N 筆 StudentAttendance（依 date desc）
    - announcement：近 30 天可見公告（scope 預測），未讀 = is_pending
    - contact_book：最近 N 筆 StudentContactBookEntry（用 published_at；草稿略過）
    - event_ack：requires_acknowledgment 的 SchoolEvent 近 30 天，is_pending = 未簽
    - medication：今日 StudentMedicationOrder
    - leave_review：近 7 天家長提交且已 reviewed 的請假
    """
    fetch_n = max(limit * 3, 30)
    now = datetime.now()
    cutoff_30d = now - timedelta(days=30)
    today = date.today()
    items: list[dict[str, Any]] = []

    # 1. attendance（最近 N 筆，依 date desc）
    for a in (
        session.query(StudentAttendance)
        .filter(StudentAttendance.student_id == student_id)
        .order_by(StudentAttendance.date.desc())
        .limit(fetch_n)
        .all()
    ):
        items.append(
            {
                "kind": "attendance",
                "id": f"attendance:{a.id}",
                "title": f"出勤狀態：{a.status}",
                "subtitle": a.date.isoformat(),
                "occurred_at": datetime.combine(
                    a.date, datetime.min.time()
                ).isoformat(),
                "is_pending": False,
                "href": "/attendance",
            }
        )

    # 2. announcements（近 30 天 + 該家長透過 scope 可見）
    guardian_ids, student_ids = _get_parent_student_ids(session, user_id)
    classroom_ids: list[int] = []
    if student_ids:
        rows = (
            session.query(Student.classroom_id)
            .filter(
                Student.id.in_(student_ids),
                Student.classroom_id.isnot(None),
            )
            .distinct()
            .all()
        )
        classroom_ids = [r[0] for r in rows]

    apr = AnnouncementParentRecipient
    visibility_conds = [apr.scope == "all"]
    if classroom_ids:
        visibility_conds.append(
            and_(apr.scope == "classroom", apr.classroom_id.in_(classroom_ids))
        )
    if student_ids:
        visibility_conds.append(
            and_(apr.scope == "student", apr.student_id.in_(student_ids))
        )
    if guardian_ids:
        visibility_conds.append(
            and_(apr.scope == "guardian", apr.guardian_id.in_(guardian_ids))
        )
    visible_subq = exists().where(
        and_(apr.announcement_id == Announcement.id, or_(*visibility_conds))
    )

    ann_rows = (
        session.query(Announcement)
        .filter(
            visible_subq,
            Announcement.created_at.isnot(None),
            Announcement.created_at >= cutoff_30d,
        )
        .order_by(Announcement.created_at.desc())
        .limit(fetch_n)
        .all()
    )
    if ann_rows:
        ann_ids = [r.id for r in ann_rows]
        read_ids = {
            r[0]
            for r in (
                session.query(AnnouncementParentRead.announcement_id)
                .filter(
                    AnnouncementParentRead.user_id == user_id,
                    AnnouncementParentRead.announcement_id.in_(ann_ids),
                )
                .all()
            )
        }
        for ann in ann_rows:
            items.append(
                {
                    "kind": "announcement",
                    "id": f"announcement:{ann.id}",
                    "title": ann.title or "公告",
                    "subtitle": None,
                    "occurred_at": ann.created_at.isoformat(),
                    "is_pending": ann.id not in read_ids,
                    "href": f"/announcements?id={ann.id}",
                }
            )

    # 3. contact_book（最近 N 筆，已 published；草稿 published_at IS NULL 不收）
    for cb in (
        session.query(StudentContactBookEntry)
        .filter(
            StudentContactBookEntry.student_id == student_id,
            StudentContactBookEntry.published_at.isnot(None),
        )
        .order_by(StudentContactBookEntry.published_at.desc())
        .limit(fetch_n)
        .all()
    ):
        items.append(
            {
                "kind": "contact_book",
                "id": f"contact_book:{cb.id}",
                "title": "聯絡簿更新",
                "subtitle": (
                    cb.published_at.date().isoformat() if cb.published_at else None
                ),
                "occurred_at": cb.published_at.isoformat(),
                "is_pending": False,
                "href": f"/contact-book/{cb.id}",
            }
        )

    # 4. event_ack（requires_acknowledgment 的 SchoolEvent 近 30 天）
    ack_rows = (
        session.query(SchoolEvent, EventAcknowledgment)
        .outerjoin(
            EventAcknowledgment,
            (EventAcknowledgment.event_id == SchoolEvent.id)
            & (EventAcknowledgment.user_id == user_id)
            & (EventAcknowledgment.student_id == student_id),
        )
        .filter(
            SchoolEvent.is_active == True,  # noqa: E712
            SchoolEvent.requires_acknowledgment == True,  # noqa: E712
            SchoolEvent.created_at.isnot(None),
            SchoolEvent.created_at >= cutoff_30d,
        )
        .order_by(SchoolEvent.created_at.desc())
        .limit(fetch_n)
        .all()
    )
    for ev, ack in ack_rows:
        is_pending = ack is None
        items.append(
            {
                "kind": "event_ack",
                "id": f"event_ack:{ev.id}",
                "title": f"待簽閱：{ev.title}" if is_pending else f"已簽閱：{ev.title}",
                "subtitle": None,
                "occurred_at": ev.created_at.isoformat(),
                "is_pending": is_pending,
                "href": f"/events/{ev.id}/ack",
            }
        )

    # 5. medication（今日訂單）
    for med in (
        session.query(StudentMedicationOrder)
        .filter(
            StudentMedicationOrder.student_id == student_id,
            StudentMedicationOrder.order_date == today,
        )
        .all()
    ):
        items.append(
            {
                "kind": "medication",
                "id": f"medication:{med.id}",
                "title": "今日用藥單",
                "subtitle": None,
                "occurred_at": (
                    med.created_at.isoformat()
                    if med.created_at
                    else datetime.combine(today, datetime.min.time()).isoformat()
                ),
                "is_pending": False,
                "href": f"/medications/{med.id}",
            }
        )

    # 6. leave_review（7 日內 approved/rejected，由家長提交）
    review_cutoff = now - timedelta(days=7)
    for lr in (
        session.query(StudentLeaveRequest)
        .filter(
            StudentLeaveRequest.applicant_user_id == user_id,
            StudentLeaveRequest.student_id == student_id,
            StudentLeaveRequest.reviewed_at.isnot(None),
            StudentLeaveRequest.reviewed_at >= review_cutoff,
            StudentLeaveRequest.status.in_(("approved", "rejected")),
        )
        .order_by(StudentLeaveRequest.reviewed_at.desc())
        .all()
    ):
        items.append(
            {
                "kind": "leave_review",
                "id": f"leave_review:{lr.id}",
                "title": f"請假{'通過' if lr.status == 'approved' else '退回'}",
                "subtitle": lr.leave_type,
                "occurred_at": lr.reviewed_at.isoformat(),
                "is_pending": False,
                "href": "/leaves",
            }
        )

    # 排序（desc）並切片
    items.sort(key=lambda it: it["occurred_at"], reverse=True)
    return items[:limit]
