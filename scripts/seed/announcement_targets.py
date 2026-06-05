"""scripts/seed/announcement_targets.py — 公告對象/已讀 + 活動回條 dev DB 示範資料。

家長端公告（GET /api/parent/announcements）與活動回條（GET /api/parent/events）
原本撈不到任何資料，因為以下四張對象/回條表全空。本模組就既有的 8 則公告、
8 個 school_event 補上對象與已讀/簽閱，讓家長端與後台手測有東西可看：

灌入四張表：
- announcement_recipients：公告 → 員工對象（空＝全員可見；本模組為部分公告
  指定具名員工，示範「定向發送」案例）。自然鍵 (announcement_id, employee_id)。
- announcement_parent_recipients：公告 → 家長端對象（scope=all/classroom/student）。
  這是家長端可見性的單一來源（api/parent_portal/announcements._build_visibility_subquery
  用 EXISTS 比對 scope）。無唯一鍵 → 以 (announcement_id, scope, classroom_id,
  student_id, guardian_id) 自然組合做 exists dedup。
- announcement_parent_reads：家長已讀。自然鍵 (announcement_id, user_id)
  UniqueConstraint。需要 parent User.id，故只能掛在有綁定家長 User 的學生身上。
- event_acknowledgments：家長對 school_event 的回條簽閱。自然鍵
  (event_id, user_id, student_id) UniqueConstraint。同樣需要 parent User.id。

家長端撈取條件對齊（讀過 api/parent_portal/announcements.py 與 events.py）：
- 公告可見性：家長 user_id → guardians → (guardian_ids, student_ids) →
  student.classroom_id → classroom_ids；recipient.scope in
  {all, classroom(classroom_id in ...), student(student_id in ...),
  guardian(guardian_id in ...)} 任一命中即可見。故本模組為「全園 / 班級 /
  指定學生」三種 scope 各灌一批，確保唯一有綁定家長的 user_id=5
  （→ student_id=1 → classroom_id=1）能看到 all / classroom=1 / student=1 三路。
- 活動回條：list_events 只在 SchoolEvent.requires_acknowledgment=True 時才把
  學生列入 need_ack；既有 8 個 event 全為 False → 家長端不顯示「簽閱」。本模組
  把時間窗內（today−30 ~ today+180）2-3 個 event 旗標翻 True 並補 ack_deadline，
  再為有綁定家長的 (user_id, student_id) 建 EventAcknowledgment（部分已簽、
  部分留空表示「未回」）。requires_acknowledgment 為資料層旗標，翻它屬於 seed
  資料範疇（非改程式碼）。

家長 User 限制（與 contact_book.py 同）：dev DB 僅 user_id=5（username=parent）
綁定到 1 名 active 學生（student_id=1, classroom_id=1）。其餘 parent User
（99401/99402）綁的是 phase1e 測試學生（classroom_id 為 NULL、非 active），
99403 無 guardian 連結。故 parent_reads / event_acknowledgments 只能掛在
user_id=5 / student_id=1 上；recipients（班級/學生/全園層級）則可廣泛覆蓋。
擴充家長 User / 綁定不在本模組範圍。

冪等契約：每筆插入前先 exists 查；重跑必新增 0 筆、不刪改現有資料。
決定論：以公告 id / event id 取模分配 scope、挑選哪幾則標已讀，重跑命中相同。

日期界線：所有 read_at / acknowledged_at ≤ TODAY（2026-06-05），絕不生未來。
"""

from __future__ import annotations

import logging
from datetime import datetime

from scripts.seed._common import (
    session_scope,
    get_active_students,
    get_active_employees,
    get_admin_user,  # noqa: F401  # 介面齊備性保留（本模組對象不需 admin user）
    get_classrooms,
    rand_date_between,
    TERM2,
    TODAY,
)
from models.database import (
    Announcement,
    AnnouncementParentRead,
    AnnouncementParentRecipient,
    AnnouncementRecipient,
    EventAcknowledgment,
    Guardian,
    SchoolEvent,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("seed_announcement_targets")

# 活動回條時間窗（對齊 api/parent_portal/events._PAST_DAYS / _FUTURE_DAYS）
from datetime import timedelta

_EVENT_WINDOW_START = TODAY - timedelta(days=30)
_EVENT_WINDOW_END = TODAY + timedelta(days=180)
# 最多翻幾個 event 為「需回條」
_MAX_ACK_EVENTS = 3


def _parent_user_to_students(session) -> dict[int, list[int]]:
    """回傳 {parent_user_id: [student_id, ...]}（活的監護人關係）。

    對齊 api/parent_portal/_shared._get_parent_student_ids 的撈法：
    Guardian.user_id IS NOT NULL AND Guardian.deleted_at IS NULL。
    """
    rows = (
        session.query(Guardian.user_id, Guardian.student_id)
        .filter(Guardian.user_id.isnot(None), Guardian.deleted_at.is_(None))
        .all()
    )
    mapping: dict[int, set[int]] = {}
    for uid, sid in rows:
        mapping.setdefault(uid, set()).add(sid)
    return {uid: sorted(sids) for uid, sids in mapping.items()}


def _seed_parent_recipients(session, announcements, classrooms, students) -> int:
    """為每則公告灌家長端對象（scope=all/classroom/student 輪替）。"""
    added = 0
    apr = AnnouncementParentRecipient
    # 取前幾個活躍班級當 classroom-scope 目標；student-scope 取前幾名學生。
    # 一定包含 classroom_id=1 與 student_id=1，確保 user_id=5 能命中。
    classroom_ids = sorted({c.id for c in classrooms})
    target_classrooms = sorted(set([1] + classroom_ids[:3]))
    student_ids = [s.id for s in students]
    target_students = sorted(set([1] + student_ids[:5]))

    for ann in announcements:
        # 以公告 id 取模決定主 scope；同時保證 student_id=1 / classroom_id=1
        # 至少各有公告覆蓋。
        mod = ann.id % 3
        rows_to_add: list[tuple[str, int | None, int | None, int | None]] = []
        if mod == 0:
            # 全園公告
            rows_to_add.append(("all", None, None, None))
        elif mod == 1:
            # 班級公告（多個班級，必含 classroom 1）
            for cid in target_classrooms:
                rows_to_add.append(("classroom", cid, None, None))
        else:
            # 指定學生公告（必含 student 1）
            for sid in target_students:
                rows_to_add.append(("student", None, sid, None))

        for scope, cid, sid, gid in rows_to_add:
            exists_q = (
                session.query(apr.id)
                .filter(
                    apr.announcement_id == ann.id,
                    apr.scope == scope,
                    (
                        apr.classroom_id.is_(cid)
                        if cid is None
                        else apr.classroom_id == cid
                    ),
                    apr.student_id.is_(sid) if sid is None else apr.student_id == sid,
                    apr.guardian_id.is_(gid) if gid is None else apr.guardian_id == gid,
                )
                .first()
            )
            if exists_q is None:
                session.add(
                    apr(
                        announcement_id=ann.id,
                        scope=scope,
                        classroom_id=cid,
                        student_id=sid,
                        guardian_id=gid,
                    )
                )
                added += 1
    return added


def _seed_employee_recipients(session, announcements, employees) -> int:
    """為部分公告指定具名員工對象（示範「定向發送」；空對象＝全員可見）。

    取 id 為偶數的公告，定向給全部 active 員工。自然鍵
    (announcement_id, employee_id) UniqueConstraint。
    """
    added = 0
    emp_ids = [e.id for e in employees]
    for ann in announcements:
        if ann.id % 2 != 0:
            continue
        for eid in emp_ids:
            exists_q = (
                session.query(AnnouncementRecipient.id)
                .filter(
                    AnnouncementRecipient.announcement_id == ann.id,
                    AnnouncementRecipient.employee_id == eid,
                )
                .first()
            )
            if exists_q is None:
                session.add(
                    AnnouncementRecipient(
                        announcement_id=ann.id,
                        employee_id=eid,
                    )
                )
                added += 1
    return added


def _seed_parent_reads(session, announcements, parent_map) -> int:
    """家長已讀：對綁定家長的 user_id，挑部分公告標已讀。

    決定論：以公告 id % 2 == 0 視為「已讀」（約半數），其餘留未讀。
    read_at 落在 TERM2 範圍內且 ≤ TODAY。自然鍵 (announcement_id, user_id)。
    """
    added = 0
    if not parent_map:
        return 0
    for ann in announcements:
        if ann.id % 2 != 0:
            continue  # 留約半數未讀，未讀計數端點才有東西可顯示
        for user_id in parent_map.keys():
            exists_q = (
                session.query(AnnouncementParentRead.id)
                .filter(
                    AnnouncementParentRead.announcement_id == ann.id,
                    AnnouncementParentRead.user_id == user_id,
                )
                .first()
            )
            if exists_q is None:
                read_dt = _read_datetime_for(ann)
                session.add(
                    AnnouncementParentRead(
                        announcement_id=ann.id,
                        user_id=user_id,
                        read_at=read_dt,
                    )
                )
                added += 1
    return added


def _read_datetime_for(ann) -> datetime:
    """已讀時間：取公告 created_at 與 TERM2 起點的較晚者 ~ TODAY 之間。

    保證 ≥ 公告建立時間且 ≤ TODAY（不生未來）。
    """
    created = ann.created_at.date() if ann.created_at else TERM2[0]
    lower = max(created, TERM2[0])
    upper = TODAY
    if lower > upper:
        lower = upper
    d = rand_date_between(lower, upper)
    return datetime(d.year, d.month, d.day, 19, 30)


def _ensure_ack_events(session) -> list[SchoolEvent]:
    """挑時間窗內的 event 翻 requires_acknowledgment=True 並補 ack_deadline。

    冪等：已是 True 的不動；只翻仍為 False 的，最多翻 _MAX_ACK_EVENTS 個。
    ack_deadline 設為 event_date+14 天但不超過給定值；若已過則設 None
    （避免後台/家長端 deadline 守衛卡住，但本 seed 直寫 DB 不經守衛）。
    """
    candidates = (
        session.query(SchoolEvent)
        .filter(
            SchoolEvent.is_active.is_(True),
            SchoolEvent.event_date >= _EVENT_WINDOW_START,
            SchoolEvent.event_date <= _EVENT_WINDOW_END,
        )
        .order_by(SchoolEvent.event_date.asc())
        .all()
    )
    # 已經是 True 的先收進來
    already = [e for e in candidates if e.requires_acknowledgment]
    to_flip = [e for e in candidates if not e.requires_acknowledgment]
    flipped = 0
    result = list(already)
    for e in to_flip:
        if len(result) >= _MAX_ACK_EVENTS:
            break
        e.requires_acknowledgment = True
        # 截止日設事件日後 30 天（多為未來，給家長簽閱空間）
        e.ack_deadline = e.event_date + timedelta(days=30)
        result.append(e)
        flipped += 1
    if flipped:
        logger.info("翻 requires_acknowledgment=True 的 event 數：%d", flipped)
    return result[:_MAX_ACK_EVENTS]


def _seed_event_acks(session, ack_events, parent_map) -> int:
    """為綁定家長的 (user_id, student_id) 在部分 event 建簽閱紀錄。

    決定論：對每個 (user, student)，event 依序號 enumerate，index 為偶數的
    視為「已簽」，奇數留空表示「未回」→ 家長端 need_ack_student_ids 才有對比。
    自然鍵 (event_id, user_id, student_id) UniqueConstraint。
    """
    added = 0
    if not ack_events or not parent_map:
        return 0
    for user_id, student_ids in parent_map.items():
        for student_id in student_ids:
            for idx, ev in enumerate(ack_events):
                if idx % 2 != 0:
                    continue  # 留奇數序號未回
                exists_q = (
                    session.query(EventAcknowledgment.id)
                    .filter(
                        EventAcknowledgment.event_id == ev.id,
                        EventAcknowledgment.user_id == user_id,
                        EventAcknowledgment.student_id == student_id,
                    )
                    .first()
                )
                if exists_q is None:
                    acked_dt = _ack_datetime_for(ev)
                    session.add(
                        EventAcknowledgment(
                            event_id=ev.id,
                            user_id=user_id,
                            student_id=student_id,
                            acknowledged_at=acked_dt,
                            signature_name="家長簽閱",
                        )
                    )
                    added += 1
    return added


def _ack_datetime_for(ev) -> datetime:
    """簽閱時間：event_date 與 TERM2 起點較晚者 ~ TODAY 之間，且 ≤ TODAY。"""
    base = ev.event_date if ev.event_date else TERM2[0]
    lower = max(base, TERM2[0])
    # 未來的 event（如畢業典禮）以 TODAY 為上限，避免生未來日期
    lower = min(lower, TODAY)
    upper = TODAY
    if lower > upper:
        lower = upper
    d = rand_date_between(lower, upper)
    return datetime(d.year, d.month, d.day, 20, 0)


def step() -> None:
    with session_scope() as session:
        announcements = session.query(Announcement).order_by(Announcement.id).all()
        events_total = session.query(SchoolEvent).count()
        classrooms = get_classrooms(session)
        students = get_active_students(session)
        employees = get_active_employees(session)
        parent_map = _parent_user_to_students(session)

        logger.info(
            "前置：公告 %d 則、school_event %d 個、班級 %d、active 學生 %d、"
            "active 員工 %d、綁定家長 User %d（%s）",
            len(announcements),
            events_total,
            len(classrooms),
            len(students),
            len(employees),
            len(parent_map),
            {uid: sids for uid, sids in parent_map.items()},
        )

        n_parent_recip = _seed_parent_recipients(
            session, announcements, classrooms, students
        )
        n_emp_recip = _seed_employee_recipients(session, announcements, employees)
        n_parent_reads = _seed_parent_reads(session, announcements, parent_map)

        ack_events = _ensure_ack_events(session)
        n_event_acks = _seed_event_acks(session, ack_events, parent_map)

        # 各表現況總數（含本次新增）
        total_parent_recip = session.query(AnnouncementParentRecipient).count()
        total_emp_recip = session.query(AnnouncementRecipient).count()
        total_parent_reads = session.query(AnnouncementParentRead).count()
        total_event_acks = session.query(EventAcknowledgment).count()

    logger.info(
        "公告對象/回條 seed 完成（本次新增）：\n"
        "  announcement_parent_recipients +%d（現況 %d）\n"
        "  announcement_recipients        +%d（現況 %d）\n"
        "  announcement_parent_reads      +%d（現況 %d）\n"
        "  event_acknowledgments          +%d（現況 %d）",
        n_parent_recip,
        total_parent_recip,
        n_emp_recip,
        total_emp_recip,
        n_parent_reads,
        total_parent_reads,
        n_event_acks,
        total_event_acks,
    )


if __name__ == "__main__":
    step()
