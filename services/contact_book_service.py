"""services/contact_book_service.py — 聯絡簿發布／統計集中入口

publish_entry：唯一發布路徑
- 標 published_at + 累加 version
- WS 廣播給該班級教師端 + 每位 guardian 的家長端（contact_book_ws，entity refresh
  payload）
- 對每位 guardian dispatch.enqueue("parent.contact_book_published", ...)；caller
  在 session.commit() 觸發 dispatch after_commit hook → LINE + 家長 inbox WS。

compute_class_completion：教師後台用「今日 X/Y 已發布」進度條
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy.orm import Session

from models.database import (
    Attachment,
    Classroom,
    Guardian,
    Student,
    StudentContactBookEntry,
    User,
)
from models.portfolio import ATTACHMENT_OWNER_CONTACT_BOOK

logger = logging.getLogger(__name__)


def _count_photos(session: Session, entry_id: int) -> int:
    return (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == ATTACHMENT_OWNER_CONTACT_BOOK,
            Attachment.owner_id == entry_id,
            Attachment.deleted_at.is_(None),
        )
        .count()
    )


def _gather_guardian_user_ids(session: Session, student_id: int) -> list[int]:
    rows = (
        session.query(Guardian.user_id)
        .filter(Guardian.student_id == student_id, Guardian.deleted_at.is_(None))
        .all()
    )
    return [r[0] for r in rows if r[0] is not None]


def publish_entry(
    session: Session,
    *,
    entry_id: int,
) -> StudentContactBookEntry:
    """發布一筆聯絡簿 entry。

    呼叫前 caller 已完成權限檢查；本函式：
    1. 標 published_at + 累加 version
    2. 推送既有 contact_book_ws WS（教師班級 + 家長個人，entity refresh payload）
    3. 對每位 guardian dispatch.enqueue("parent.contact_book_published", ...)
       — dispatch 在 caller 的 session.commit() 觸發 after_commit hook 完成
       LINE + 家長 inbox WS fan-out

    回傳更新後的 entry（caller 仍持 session 控制 commit）。
    """
    entry = (
        session.query(StudentContactBookEntry)
        .filter(
            StudentContactBookEntry.id == entry_id,
            StudentContactBookEntry.deleted_at.is_(None),
        )
        .first()
    )
    if entry is None:
        raise ValueError(f"contact_book entry not found: {entry_id}")

    if entry.published_at is None:
        entry.published_at = now_taipei_naive()
        entry.version = (entry.version or 1) + 1
    session.flush()

    student = session.query(Student).filter(Student.id == entry.student_id).first()
    student_name = student.name if student else f"student#{entry.student_id}"

    guardian_user_ids = _gather_guardian_user_ids(session, entry.student_id)

    # photo_publish 廣播咽喉（spec §3.1b，P2-1 review P1-1 修正）：
    # flag on + 含照片 entry → 過濾掉未同意 photo_publish 的 guardian，再 fan-out。
    # flag on + 純文字 entry（無照片）→ 不過濾，全部 guardian 照收。
    #   過嚴修正：photo_publish 同意只控管「照片」接收；純文字聯絡簿不含照片，
    #   不應因 photo_publish 未同意就阻斷整筆通知。
    # flag off 時 no-op，全部 guardian 皆收到。
    #
    # 完整「廣播照發但 payload 去照片」（未同意者收通知但不含照片內容）較複雜，
    # 列為 follow-up；本 fix 採「含照片 entry 才過濾整筆」的務實折中。
    from config import get_settings
    from models.consent import CONSENT_SCOPE_PHOTO_PUBLISH
    from services.consent.checker import consent_check

    if (
        get_settings().consent.enforcement_enabled
        and _count_photos(session, entry_id) > 0
    ):
        guardian_user_ids = [
            uid
            for uid in guardian_user_ids
            if consent_check(session, uid, CONSENT_SCOPE_PHOTO_PUBLISH)
        ]

    # 廣播 payload — 同 event 內含足夠 metadata 讓前端決定刷新範圍。
    # 與 dispatch ws channel 共存：本 payload 用 'type' key（entity refresh），
    # dispatch 用 'event_type' key（inbox 通知），前端依 key 分流。
    event_payload = {
        "type": "contact_book_published",
        "entry_id": entry.id,
        "student_id": entry.student_id,
        "classroom_id": entry.classroom_id,
        "log_date": entry.log_date.isoformat() if entry.log_date else None,
        "published_at": (
            entry.published_at.isoformat() if entry.published_at else None
        ),
    }

    # WS broadcasting：fire-and-forget；單筆失敗不影響交易
    try:
        from api.contact_book_ws import broadcast_classroom, broadcast_parent
        from utils.event_loop import get_main_loop

        async def _fanout():
            await broadcast_classroom(entry.classroom_id, event_payload)
            for uid in guardian_user_ids:
                await broadcast_parent(uid, event_payload)

        # 同 thread 內已有 running loop（async 路由直接呼叫）→ create_task；
        # sync def 路由跑在 starlette thread pool 沒 loop → 投回主 loop 跑，
        # 避免 asyncio.run() 起新 loop 後 WS transport 視為僵死誤踢訂閱者。
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_fanout())
        except RuntimeError:
            main_loop = get_main_loop()
            if main_loop is not None and main_loop.is_running():
                asyncio.run_coroutine_threadsafe(_fanout(), main_loop)
            else:
                # 測試或 lifespan 尚未跑（理論上不會走到）：保險走 asyncio.run
                asyncio.run(_fanout())
    except Exception as exc:
        logger.warning("contact_book WS 廣播失敗（不阻斷）：%s", exc)

    # 通知 enqueue：commit 前；dispatch._fan_out 對每位 guardian 做 LINE
    # 可達性 + 偏好 gate；caller 的 session.commit() 觸發 fan-out。
    from services.notification import dispatch

    log_date_iso = entry.log_date.isoformat() if entry.log_date else None
    for uid in guardian_user_ids:
        dispatch.enqueue(
            session=session,
            event_type="parent.contact_book_published",
            recipient_user_id=uid,
            context={
                "student_name": student_name,
                "date": log_date_iso,
            },
            source_entity_type="contact_book_entry",
            source_entity_id=entry.id,
        )

    return entry


# 範本可套用欄位清單（與 ContactBookTemplate.fields 對應）
TEMPLATE_FILLABLE_FIELDS = (
    "mood",
    "meal_lunch",
    "meal_snack",
    "nap_minutes",
    "bowel",
    "temperature_c",
    "teacher_note",
    "learning_highlight",
)


def apply_template_fields(
    entry: StudentContactBookEntry,
    template_fields: dict,
    *,
    only_fill_blank: bool = True,
) -> list[str]:
    """把 template_fields 套用到 entry。

    only_fill_blank=True：僅填入「entry 為 None」的欄位（不蓋已填值，避免破壞）。
    only_fill_blank=False：強制覆蓋（含 None → None）。

    回傳：實際被修改的欄位名稱清單。
    """
    if not template_fields:
        return []
    changed: list[str] = []
    for field in TEMPLATE_FILLABLE_FIELDS:
        if field not in template_fields:
            continue
        new_val = template_fields[field]
        if only_fill_blank:
            cur_val = getattr(entry, field, None)
            if cur_val not in (None, ""):
                continue
            if new_val in (None, ""):
                continue
        setattr(entry, field, new_val)
        changed.append(field)
    return changed


def copy_yesterday_to_today(
    session: Session,
    *,
    classroom_id: int,
    target_date,
    created_by_employee_id: int | None = None,
) -> int:
    """把昨日該班所有 entry 的欄位複製為當日草稿。

    - 已存在當日 entry 的學生 skip（避免覆蓋既有資料）
    - 從 yesterday(target_date - 1) 取每位學生最後一筆 entry，欄位整段複製
    - 不複製 published_at / version（一律 NEW 草稿）
    回傳：新建的 entry 數量。
    """
    from datetime import timedelta

    yesterday = target_date - timedelta(days=1)
    yesterday_entries = (
        session.query(StudentContactBookEntry)
        .filter(
            StudentContactBookEntry.classroom_id == classroom_id,
            StudentContactBookEntry.log_date == yesterday,
            StudentContactBookEntry.deleted_at.is_(None),
        )
        .all()
    )
    if not yesterday_entries:
        return 0

    existing_today_student_ids = {
        sid
        for (sid,) in session.query(StudentContactBookEntry.student_id)
        .filter(
            StudentContactBookEntry.classroom_id == classroom_id,
            StudentContactBookEntry.log_date == target_date,
            StudentContactBookEntry.deleted_at.is_(None),
        )
        .all()
    }

    created = 0
    for src in yesterday_entries:
        if src.student_id in existing_today_student_ids:
            continue
        new_entry = StudentContactBookEntry(
            student_id=src.student_id,
            classroom_id=classroom_id,
            log_date=target_date,
            mood=src.mood,
            meal_lunch=src.meal_lunch,
            meal_snack=src.meal_snack,
            nap_minutes=src.nap_minutes,
            bowel=src.bowel,
            temperature_c=src.temperature_c,
            teacher_note=src.teacher_note,
            learning_highlight=src.learning_highlight,
            created_by_employee_id=created_by_employee_id,
        )
        session.add(new_entry)
        created += 1
    session.flush()
    return created


def compute_class_completion(
    session: Session,
    *,
    classroom_id,
    log_date,
) -> dict:
    """回傳該班該日聯絡簿完成度：roster 數 / 草稿數 / 已發布數。

    支援 int 或 list[int]：
    - int → 原 dict 結構（向後相容）
    - list[int] → dict[int, dict]
    - 空 list → {}
    """
    if isinstance(classroom_id, list):
        if not classroom_id:
            return {}
        return _compute_class_completion_batch(
            session, classroom_ids=classroom_id, log_date=log_date
        )
    return _compute_class_completion_single(
        session, classroom_id=classroom_id, log_date=log_date
    )


def _compute_class_completion_single(
    session: Session,
    *,
    classroom_id: int,
    log_date,
) -> dict:
    """單班實作（原 compute_class_completion body）。"""
    roster = (
        session.query(Student)
        .filter(
            Student.classroom_id == classroom_id,
            Student.is_active.is_(True),
        )
        .count()
    )
    entries = (
        session.query(StudentContactBookEntry)
        .filter(
            StudentContactBookEntry.classroom_id == classroom_id,
            StudentContactBookEntry.log_date == log_date,
            StudentContactBookEntry.deleted_at.is_(None),
        )
        .all()
    )
    draft = sum(1 for e in entries if e.published_at is None)
    published = sum(1 for e in entries if e.published_at is not None)
    return {
        "roster": roster,
        "draft": draft,
        "published": published,
        "missing": max(roster - draft - published, 0),
    }


def _compute_class_completion_batch(
    session: Session,
    *,
    classroom_ids: list,
    log_date,
) -> dict:
    """多班 batch 實作：2 個 GROUP BY query 取代 N*2 個 query。

    Strategy:
      1. 一次 IN+GROUP BY 取所有班級 roster (Student count by classroom_id)
      2. 一次 IN 取所有 entries，Python 端按 classroom_id 分組
      3. Python 端組裝成 dict[classroom_id, dict]
    """
    from sqlalchemy import func

    rosters = dict(
        session.query(Student.classroom_id, func.count(Student.id))
        .filter(
            Student.classroom_id.in_(classroom_ids),
            Student.is_active.is_(True),
        )
        .group_by(Student.classroom_id)
        .all()
    )

    entries = (
        session.query(StudentContactBookEntry)
        .filter(
            StudentContactBookEntry.classroom_id.in_(classroom_ids),
            StudentContactBookEntry.log_date == log_date,
            StudentContactBookEntry.deleted_at.is_(None),
        )
        .all()
    )

    result = {}
    for cid in classroom_ids:
        roster = rosters.get(cid, 0)
        cid_entries = [e for e in entries if e.classroom_id == cid]
        draft = sum(1 for e in cid_entries if e.published_at is None)
        published = sum(1 for e in cid_entries if e.published_at is not None)
        result[cid] = {
            "roster": roster,
            "draft": draft,
            "published": published,
            "missing": max(roster - draft - published, 0),
        }
    return result
