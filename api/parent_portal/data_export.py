"""api/parent_portal/data_export.py — 家長端個資查閱權（個資法 §10）

GET /api/parent/me/data-export

回當前家長綁的所有 student 的全資料 JSON。同步生成，rate-limit 1/小時/user，
容量 50MB 上限。符合 PDPA Art.10 個資主體查閱及複製個人資料之請求。

稽核：此 GET endpoint 存取 PII，透過 write_explicit_audit 留下明確稽核紀錄。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from utils.taipei_time import now_taipei_naive

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from utils.auth import require_parent_role
from utils.rate_limit import create_limiter

from ._dependencies import get_parent_db
from ._shared import (
    _get_parent_student_ids,
    _get_parent_user,
    resolve_parent_display_name,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["parent-data-export"])

_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

_export_limiter = create_limiter(
    max_calls=1,
    window_seconds=3600,
    name="parent_data_export",
    error_detail="每小時限下載 1 次，請稍後再試",
)


@router.get("/me/data-export")
def get_data_export(
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session=Depends(get_parent_db),
):
    """家長下載自身及子女的全部個人資料（個資法 §10 查閱複製權）。

    rate-limit：每 user_id 每 1 小時限 1 次。
    50 MB 上限：超出則 413（建議聯絡園所協助）。
    """
    user = _get_parent_user(session, current_user)
    _export_limiter.check(f"user:{user.id}")

    _, student_ids = _get_parent_student_ids(session, user.id)

    students_payload = [
        _collect_student_export(session, sid, user.id) for sid in student_ids
    ]

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by_user_id": user.id,
        "schema_version": 1,
        "parent": {
            "display_name": resolve_parent_display_name(session, user),
            "line_user_id": (
                user.username
                if user.username and user.username.startswith("parent_line_")
                else None
            ),
        },
        "students": students_payload,
    }

    body = json.dumps(payload, ensure_ascii=False, default=str)
    if len(body.encode("utf-8")) > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail="資料量超過 50MB，請聯絡園所協助匯出",
        )

    filename = f"ivy_data_export_{user.id}_{now_taipei_naive().strftime('%Y%m%d')}.json"

    # 顯式 audit：GET 但讀 PII，須留稽核軌跡（AuditMiddleware 只攔 POST/PATCH/PUT/DELETE）
    from utils.audit import write_explicit_audit

    write_explicit_audit(
        request,
        action="READ",
        entity_type="parent_data_export",
        summary=f"家長下載個人資料 ({len(students_payload)} 學生)",
        entity_id=str(user.id),
        changes={"student_count": len(students_payload), "size_bytes": len(body)},
    )

    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── per-student aggregator ─────────────────────────────────────────────────


def _collect_student_export(session, student_id: int, user_id: int) -> dict:
    """聚合單一 student 的所有 module 資料。"""
    from models.classroom import Student
    from models.database import Guardian

    student = session.query(Student).filter(Student.id == student_id).first()
    if student is None:
        return {}

    guardian = (
        session.query(Guardian)
        .filter(
            Guardian.student_id == student_id,
            Guardian.user_id == user_id,
            Guardian.deleted_at.is_(None),
        )
        .first()
    )

    return {
        "id": student.id,
        "name": student.name,
        "birthday": student.birthday.isoformat() if student.birthday else None,
        "lifecycle_status": student.lifecycle_status,
        "guardian_role": (
            {
                "name": guardian.name,
                "relation": guardian.relation,
                "is_primary": guardian.is_primary,
            }
            if guardian
            else None
        ),
        "contact_book": _list_contact_book(session, student_id),
        "attendance": _list_attendance(session, student_id),
        "leaves": _list_leaves(session, student_id),
        "fees": _list_fees(session, student_id),
        "medications": _list_medications(session, student_id),
        "photos": _list_photos(session, student_id),
        "messages": _list_messages(session, student_id, user_id),
        "growth_reports": _list_growth_reports(session, student_id),
    }


# ── module helpers（對齊各 api/parent_portal/<module>.py 的 list query） ────


def _list_contact_book(session, student_id: int) -> list[dict]:
    """對齊 api/parent_portal/contact_book.py list_history：
    已發布、未軟刪，全歷史（無日期上限）。
    """
    try:
        from models.contact_book import StudentContactBookEntry

        rows = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.student_id == student_id,
                StudentContactBookEntry.deleted_at.is_(None),
                StudentContactBookEntry.published_at.isnot(None),
            )
            .order_by(StudentContactBookEntry.log_date.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "log_date": r.log_date.isoformat() if r.log_date else None,
                "mood": r.mood,
                "meal_lunch": r.meal_lunch,
                "meal_snack": r.meal_snack,
                "nap_minutes": r.nap_minutes,
                "bowel": r.bowel,
                "temperature_c": (
                    float(r.temperature_c) if r.temperature_c is not None else None
                ),
                "teacher_note": r.teacher_note,
                "learning_highlight": r.learning_highlight,
                "published_at": (
                    r.published_at.isoformat() if r.published_at else None
                ),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("contact_book export failed for student %s", student_id)
        return []


def _list_attendance(session, student_id: int) -> list[dict]:
    """對齊 api/parent_portal/attendance.py：全歷史出席記錄。"""
    try:
        from models.database import StudentAttendance

        rows = (
            session.query(StudentAttendance)
            .filter(StudentAttendance.student_id == student_id)
            .order_by(StudentAttendance.date.asc())
            .all()
        )
        return [
            {
                "date": r.date.isoformat() if r.date else None,
                "status": r.status,
                "remark": r.remark,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("attendance export failed for student %s", student_id)
        return []


def _list_leaves(session, student_id: int) -> list[dict]:
    """對齊 api/parent_portal/leaves.py list_leaves：全歷史請假。"""
    try:
        from models.database import StudentLeaveRequest

        rows = (
            session.query(StudentLeaveRequest)
            .filter(StudentLeaveRequest.student_id == student_id)
            .order_by(StudentLeaveRequest.created_at.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "leave_type": r.leave_type,
                "start_date": r.start_date.isoformat() if r.start_date else None,
                "end_date": r.end_date.isoformat() if r.end_date else None,
                "reason": r.reason,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("leaves export failed for student %s", student_id)
        return []


def _list_fees(session, student_id: int) -> list[dict]:
    """對齊 api/parent_portal/fees.py list_records：全歷史費用記錄。
    不含 operator/refunded_by 等員工欄位（家長端隱私規範）。
    """
    try:
        from models.fees import StudentFeeRecord

        rows = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.student_id == student_id)
            .order_by(
                StudentFeeRecord.due_date.asc().nulls_last(),
                StudentFeeRecord.created_at.asc(),
            )
            .all()
        )
        return [
            {
                "id": r.id,
                "fee_item_name": r.fee_item_name,
                "period": r.period,
                "amount_due": r.amount_due or 0,
                "amount_paid": r.amount_paid or 0,
                "outstanding": max(0, (r.amount_due or 0) - (r.amount_paid or 0)),
                "status": r.status,
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "payment_date": (
                    r.payment_date.isoformat() if r.payment_date else None
                ),
            }
            for r in rows
        ]
    except Exception:
        logger.exception("fees export failed for student %s", student_id)
        return []


def _list_medications(session, student_id: int) -> list[dict]:
    """對齊 api/parent_portal/medications.py list_medication_orders：全歷史用藥單。"""
    try:
        from models.database import StudentMedicationOrder

        orders = (
            session.query(StudentMedicationOrder)
            .filter(StudentMedicationOrder.student_id == student_id)
            .order_by(
                StudentMedicationOrder.order_date.desc(),
                StudentMedicationOrder.id.desc(),
            )
            .all()
        )
        return [
            {
                "id": o.id,
                "order_date": o.order_date.isoformat() if o.order_date else None,
                "medication_name": o.medication_name,
                "dose": o.dose,
                "time_slots": list(o.time_slots or []),
                "note": o.note,
                "source": o.source,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in orders
        ]
    except Exception:
        logger.exception("medications export failed for student %s", student_id)
        return []


def _list_photos(session, student_id: int) -> list[dict]:
    """對齊 api/parent_portal/photos.py：已發布、未軟刪的圖片 attachment metadata。
    不含 URL（匯出僅為 metadata；家長可透過正常 endpoint 下載附件）。
    """
    try:
        from models.database import (
            Attachment,
            StudentGrowthReport,
            StudentMedicationOrder,
            StudentObservation,
        )
        from models.contact_book import StudentContactBookEntry
        from models.portfolio import (
            ATTACHMENT_OWNER_CONTACT_BOOK,
            ATTACHMENT_OWNER_MEDICATION_ORDER,
            ATTACHMENT_OWNER_OBSERVATION,
            ATTACHMENT_OWNER_REPORT,
            REPORT_STATUS_READY,
        )
        from api.portfolio.student_attachments import SUPPORTED_OWNER_TYPES, _is_image

        all_items: list[dict] = []
        for ot in SUPPORTED_OWNER_TYPES:
            # 取出此 owner_type 在此 student 下的可見 owner_id（與 photos.py 一致）
            if ot == ATTACHMENT_OWNER_OBSERVATION:
                owner_ids = [
                    r[0]
                    for r in session.query(StudentObservation.id)
                    .filter(
                        StudentObservation.student_id == student_id,
                        StudentObservation.deleted_at.is_(None),
                    )
                    .all()
                ]
            elif ot == ATTACHMENT_OWNER_CONTACT_BOOK:
                owner_ids = [
                    r[0]
                    for r in session.query(StudentContactBookEntry.id)
                    .filter(
                        StudentContactBookEntry.student_id == student_id,
                        StudentContactBookEntry.deleted_at.is_(None),
                        StudentContactBookEntry.published_at.isnot(None),
                    )
                    .all()
                ]
            elif ot == ATTACHMENT_OWNER_MEDICATION_ORDER:
                owner_ids = [
                    r[0]
                    for r in session.query(StudentMedicationOrder.id)
                    .filter(StudentMedicationOrder.student_id == student_id)
                    .all()
                ]
            elif ot == ATTACHMENT_OWNER_REPORT:
                owner_ids = [
                    r[0]
                    for r in session.query(StudentGrowthReport.id)
                    .filter(
                        StudentGrowthReport.student_id == student_id,
                        StudentGrowthReport.status == REPORT_STATUS_READY,
                    )
                    .all()
                ]
            else:
                continue

            if not owner_ids:
                continue

            rows = (
                session.query(Attachment)
                .filter(
                    Attachment.owner_type == ot,
                    Attachment.owner_id.in_(owner_ids),
                    Attachment.deleted_at.is_(None),
                )
                .order_by(Attachment.created_at.desc())
                .all()
            )
            for a in rows:
                if not _is_image(a.mime_type):
                    continue
                all_items.append(
                    {
                        "id": a.id,
                        "owner_type": a.owner_type,
                        "owner_id": a.owner_id,
                        "original_filename": a.original_filename,
                        "mime_type": a.mime_type,
                        "size_bytes": a.size_bytes,
                        "created_at": (
                            a.created_at.isoformat() if a.created_at else None
                        ),
                    }
                )

        all_items.sort(key=lambda x: x.get("created_at", "") or "", reverse=True)
        return all_items
    except Exception:
        logger.exception("photos export failed for student %s", student_id)
        return []


def _list_messages(session, student_id: int, user_id: int) -> list[dict]:
    """對齊 api/parent_portal/messages.py：此家長參與的 thread 列表（含最後訊息）。

    messages 涉及多表 join（Thread → Student → Teacher → Messages），僅匯出
    thread-level summary（不逐筆列出全部訊息）以控制 payload 大小。
    若需完整訊息，家長可透過正常 /messages/threads/{id}/messages 端點取得。
    """
    try:
        from models.database import ParentMessage, ParentMessageThread

        threads = (
            session.query(ParentMessageThread)
            .filter(
                ParentMessageThread.student_id == student_id,
                ParentMessageThread.parent_user_id == user_id,
                ParentMessageThread.deleted_at.is_(None),
            )
            .order_by(ParentMessageThread.last_message_at.desc().nulls_last())
            .all()
        )
        if not threads:
            return []

        thread_ids = [t.id for t in threads]
        # 每個 thread 的最後一筆訊息（非軟刪）
        from sqlalchemy import func

        last_subq = (
            session.query(
                ParentMessage.thread_id.label("thread_id"),
                func.max(ParentMessage.created_at).label("max_at"),
            )
            .filter(
                ParentMessage.thread_id.in_(thread_ids),
                ParentMessage.deleted_at.is_(None),
            )
            .group_by(ParentMessage.thread_id)
            .subquery()
        )
        last_msgs = (
            session.query(ParentMessage)
            .join(
                last_subq,
                (ParentMessage.thread_id == last_subq.c.thread_id)
                & (ParentMessage.created_at == last_subq.c.max_at),
            )
            .filter(ParentMessage.deleted_at.is_(None))
            .all()
        )
        last_by_thread: dict[int, ParentMessage] = {m.thread_id: m for m in last_msgs}

        result = []
        for t in threads:
            last = last_by_thread.get(t.id)
            result.append(
                {
                    "thread_id": t.id,
                    "student_id": t.student_id,
                    "last_message_at": (
                        t.last_message_at.isoformat() if t.last_message_at else None
                    ),
                    "last_message_preview": (
                        (last.body or "(附件)")[:60] if last else None
                    ),
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
            )
        return result
    except Exception:
        logger.exception("messages export failed for student %s", student_id)
        return []


def _list_growth_reports(session, student_id: int) -> list[dict]:
    """對齊 api/parent_portal/growth_reports.py：已 READY 的成長報告 metadata。"""
    try:
        from models.database import StudentGrowthReport
        from models.portfolio import REPORT_STATUS_READY

        rows = (
            session.query(StudentGrowthReport)
            .filter(
                StudentGrowthReport.student_id == student_id,
                StudentGrowthReport.status == REPORT_STATUS_READY,
            )
            .order_by(StudentGrowthReport.created_at.desc())
            .all()
        )
        return [
            {
                "id": r.id,
                "period_label": r.period_label,
                "period_start": r.period_start.isoformat() if r.period_start else None,
                "period_end": r.period_end.isoformat() if r.period_end else None,
                "status": r.status,
                "generated_at": (
                    r.generated_at.isoformat() if r.generated_at else None
                ),
                "teacher_narrative": r.teacher_narrative,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("growth_reports export failed for student %s", student_id)
        return []
