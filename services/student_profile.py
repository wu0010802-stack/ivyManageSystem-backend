"""學生檔案聚合服務。

`assemble_profile(session, student_id)` 回傳一個 dict，包含：
  - basic: 基本資料
  - lifecycle: 目前生命週期狀態 / 入學 / 退學 / 畢業 日期
  - guardians: 監護人列表（過濾軟刪）
  - health: 過敏、用藥、特殊需求
  - attendance_summary: 本學期出席統計
  - fee_summary: 本學期費用摘要
  - incident_summary: 最近事件摘要
  - timeline: StudentChangeLog 最近 N 筆

每個 summary 各自獨立，方便單元測試與快取。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.classroom import (
    Classroom,
    LIFECYCLE_ACTIVE,
    Student,
    StudentAssessment,
    StudentAttendance,
    StudentIncident,
)
from models.fees import StudentFeeRecord
from models.guardian import Guardian
from models.student_log import StudentChangeLog
from utils.academic import resolve_current_academic_term


DEFAULT_TIMELINE_LIMIT = 20
DEFAULT_INCIDENT_LIMIT = 5
DEFAULT_ASSESSMENT_LIMIT = 5


def _serialize_guardian(g: Guardian) -> dict[str, Any]:
    return {
        "id": g.id,
        "name": g.name,
        "phone": g.phone,
        "email": g.email,
        "relation": g.relation,
        "is_primary": bool(g.is_primary),
        "is_emergency": bool(g.is_emergency),
        "can_pickup": bool(g.can_pickup),
        "custody_note": g.custody_note,
        "sort_order": g.sort_order,
    }


def get_guardians(session: Session, student_id: int) -> list[dict[str, Any]]:
    rows = (
        session.query(Guardian)
        .filter(
            Guardian.student_id == student_id,
            Guardian.deleted_at.is_(None),
        )
        .order_by(Guardian.is_primary.desc(), Guardian.sort_order.asc(), Guardian.id.asc())
        .all()
    )
    return [_serialize_guardian(g) for g in rows]


def get_attendance_summary(
    session: Session, student_id: int, period_start: date, period_end: date
) -> dict[str, Any]:
    rows = (
        session.query(StudentAttendance.status, func.count(StudentAttendance.id))
        .filter(
            StudentAttendance.student_id == student_id,
            StudentAttendance.date >= period_start,
            StudentAttendance.date <= period_end,
        )
        .group_by(StudentAttendance.status)
        .all()
    )
    counts = {status: int(n or 0) for status, n in rows}
    total = sum(counts.values())
    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "total_records": total,
        "by_status": counts,
    }


def get_fee_summary(
    session: Session, student_id: int, period: Optional[str] = None
) -> dict[str, Any]:
    """回傳指定 period 的費用摘要。period=None 則聚合全部歷史。

    注意：FeeItem.period 是使用者自由輸入字串（如 "114-1"、"2025上"），
    沒有固定格式，所以預設不限制。
    """
    query = session.query(
        func.coalesce(func.sum(StudentFeeRecord.amount_due), 0),
        func.coalesce(func.sum(StudentFeeRecord.amount_paid), 0),
        func.count(StudentFeeRecord.id),
    ).filter(StudentFeeRecord.student_id == student_id)
    if period:
        query = query.filter(StudentFeeRecord.period == period)

    total_due, total_paid, item_count = query.one()
    total_due = int(total_due or 0)
    total_paid = int(total_paid or 0)
    return {
        "period": period,
        "item_count": int(item_count or 0),
        "total_due": total_due,
        "total_paid": total_paid,
        "outstanding": max(total_due - total_paid, 0),
    }


def get_incident_summary(
    session: Session, student_id: int, limit: int = DEFAULT_INCIDENT_LIMIT
) -> list[dict[str, Any]]:
    rows = (
        session.query(StudentIncident)
        .filter(StudentIncident.student_id == student_id)
        .order_by(StudentIncident.occurred_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "incident_type": r.incident_type,
            "severity": r.severity,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "description": r.description,
            "parent_notified": bool(r.parent_notified),
        }
        for r in rows
    ]


def get_assessment_summary(
    session: Session, student_id: int, limit: int = DEFAULT_ASSESSMENT_LIMIT
) -> list[dict[str, Any]]:
    rows = (
        session.query(StudentAssessment)
        .filter(StudentAssessment.student_id == student_id)
        .order_by(StudentAssessment.assessment_date.desc(), StudentAssessment.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "semester": r.semester,
            "assessment_type": r.assessment_type,
            "domain": r.domain,
            "rating": r.rating,
            "content": r.content,
            "suggestions": r.suggestions,
            "assessment_date": r.assessment_date.isoformat() if r.assessment_date else None,
        }
        for r in rows
    ]


def _merged_timeline(
    incident_items: list[dict[str, Any]],
    assessment_items: list[dict[str, Any]],
    change_log_items: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """合併三類為統一時間軸（最新優先）。每筆加 `record_type`。"""
    merged: list[dict[str, Any]] = []
    for it in incident_items:
        merged.append(
            {
                "record_type": "incident",
                "record_id": it["id"],
                "occurred_at": it["occurred_at"],
                "summary": it.get("incident_type") or "",
                "payload": it,
            }
        )
    for it in assessment_items:
        merged.append(
            {
                "record_type": "assessment",
                "record_id": it["id"],
                "occurred_at": it["assessment_date"],
                "summary": "｜".join(
                    p for p in [it.get("assessment_type"), it.get("domain"), it.get("rating")] if p
                ),
                "payload": it,
            }
        )
    for it in change_log_items:
        merged.append(
            {
                "record_type": "change_log",
                "record_id": it["id"],
                "occurred_at": it["event_date"],
                "summary": it.get("event_type") or "",
                "payload": it,
            }
        )

    def sort_key(item):
        ts = item["occurred_at"] or ""
        return (ts, item["record_type"], item["record_id"])

    merged.sort(key=sort_key, reverse=True)
    return merged[:limit]


def get_timeline(
    session: Session, student_id: int, limit: int = DEFAULT_TIMELINE_LIMIT
) -> list[dict[str, Any]]:
    rows = (
        session.query(StudentChangeLog)
        .filter(StudentChangeLog.student_id == student_id)
        .order_by(
            StudentChangeLog.event_date.desc(),
            StudentChangeLog.id.desc(),
        )
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "event_date": r.event_date.isoformat() if r.event_date else None,
            "school_year": r.school_year,
            "semester": r.semester,
            "classroom_id": r.classroom_id,
            "from_classroom_id": r.from_classroom_id,
            "to_classroom_id": r.to_classroom_id,
            "reason": r.reason,
            "notes": r.notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def _serialize_basic(student: Student, classroom: Optional[Classroom]) -> dict[str, Any]:
    return {
        "id": student.id,
        "student_id": student.student_id,
        "name": student.name,
        "gender": student.gender,
        "birthday": student.birthday.isoformat() if student.birthday else None,
        "classroom_id": student.classroom_id,
        "classroom_name": classroom.name if classroom else None,
        "address": student.address,
        "notes": student.notes,
        "status_tag": student.status_tag,
        "is_active": bool(student.is_active),
        # deprecated 但相容期保留
        "parent_name": student.parent_name,
        "parent_phone": student.parent_phone,
    }


def _serialize_lifecycle(student: Student) -> dict[str, Any]:
    return {
        "status": student.lifecycle_status or LIFECYCLE_ACTIVE,
        "legacy_status": student.status,
        "enrollment_date": (
            student.enrollment_date.isoformat() if student.enrollment_date else None
        ),
        "graduation_date": (
            student.graduation_date.isoformat() if student.graduation_date else None
        ),
        "withdrawal_date": (
            student.withdrawal_date.isoformat() if student.withdrawal_date else None
        ),
        "recruitment_visit_id": student.recruitment_visit_id,
    }


def _serialize_health(student: Student) -> dict[str, Any]:
    return {
        "allergy": student.allergy,
        "medication": student.medication,
        "special_needs": student.special_needs,
        "emergency_contact_name": student.emergency_contact_name,
        "emergency_contact_phone": student.emergency_contact_phone,
        "emergency_contact_relation": student.emergency_contact_relation,
    }


def _default_attendance_window(today: Optional[date] = None) -> tuple[date, date]:
    """預設本學期起訖日（依 resolve_current_academic_term 決定）。

    `resolve_current_academic_term()` 回傳**民國年**（學年度 100–200），
    故需 +1911 轉為西元才能建立 `date()`。

    規則：
    - 學期 1 = 民國 YYY/08/01 ~ (YYY+1)/01/31
    - 學期 2 = (YYY+1)/02/01 ~ (YYY+1)/07/31
    """
    today = today or date.today()
    school_year_roc, semester = resolve_current_academic_term()
    ad_start_year = school_year_roc + 1911
    if semester == 1:
        start = date(ad_start_year, 8, 1)
        end = date(ad_start_year + 1, 1, 31)
    else:
        # 下學期實際日曆年為 民國 (YYY+1) 年
        start = date(ad_start_year + 1, 2, 1)
        end = date(ad_start_year + 1, 7, 31)
    return start, end


def assemble_profile(
    session: Session,
    student_id: int,
    *,
    timeline_limit: int = DEFAULT_TIMELINE_LIMIT,
    incident_limit: int = DEFAULT_INCIDENT_LIMIT,
    fee_period: Optional[str] = None,
    attendance_window: Optional[tuple[date, date]] = None,
) -> Optional[dict[str, Any]]:
    """組裝學生完整檔案。回傳 None 表示學生不存在。"""
    student = session.query(Student).filter(Student.id == student_id).first()
    if student is None:
        return None

    classroom = None
    if student.classroom_id:
        classroom = (
            session.query(Classroom).filter(Classroom.id == student.classroom_id).first()
        )

    # fee_period=None 表示聚合所有歷史費用（FeeItem.period 為自由字串無固定格式）
    period = fee_period
    att_start, att_end = attendance_window or _default_attendance_window()

    incident_summary = get_incident_summary(
        session, student_id, limit=incident_limit
    )
    assessment_summary = get_assessment_summary(
        session, student_id, limit=DEFAULT_ASSESSMENT_LIMIT
    )
    timeline = get_timeline(session, student_id, limit=timeline_limit)
    # timeline_all 從三類各自取較寬池（limit * 3）再合併截尾，避免某類過多時
    # 較舊資料提早被 summary 截掉、無法進入 top-N 合併。
    merge_pool_limit = timeline_limit * 3
    timeline_all = _merged_timeline(
        get_incident_summary(session, student_id, limit=merge_pool_limit),
        get_assessment_summary(session, student_id, limit=merge_pool_limit),
        get_timeline(session, student_id, limit=merge_pool_limit),
        limit=timeline_limit,
    )

    return {
        "basic": _serialize_basic(student, classroom),
        "lifecycle": _serialize_lifecycle(student),
        "health": _serialize_health(student),
        "guardians": get_guardians(session, student_id),
        "attendance_summary": get_attendance_summary(
            session, student_id, att_start, att_end
        ),
        "fee_summary": get_fee_summary(session, student_id, period),
        "incident_summary": incident_summary,
        "assessment_summary": assessment_summary,
        "timeline": timeline,
        "timeline_all": timeline_all,
    }
