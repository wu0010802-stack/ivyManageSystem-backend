"""跨模型學生紀錄時間軸聚合服務。

`list_timeline()` 合併三種學生紀錄為統一時間軸：
  - `incident`（StudentIncident，`occurred_at` 為時間鍵）
  - `assessment`（StudentAssessment，`assessment_date` 為時間鍵）
  - `change_log`（StudentChangeLog，`event_date` 為時間鍵）

策略：三次查詢 + Python 合併排序（不使用 UNION ALL）
  - 三表欄位差異大，投影共同欄位後仍需 payload
  - 權限 / 篩選在三個 query 上獨立套用較直覺
  - tie-break：`(ts desc, record_type, record_id desc)` 保穩定
  - 合併後再批次以 `in_(...)` 補 student_name / classroom_name，避免 N+1

本服務**不**呼叫 `resolve_academic_term_filters` —— 參數 None 代表「不過濾」，
避免聚合端點隱形填入本學期而遮蔽資料。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Optional

from sqlalchemy.orm import Session

from models.classroom import Classroom, Student, StudentAssessment, StudentIncident
from models.student_log import StudentChangeLog

RECORD_TYPES = ("incident", "assessment", "change_log")


def _to_datetime(d) -> datetime:
    """日期型別統一升為 datetime (midnight)，datetime 則原樣回傳。"""
    if isinstance(d, datetime):
        return d
    if isinstance(d, date):
        return datetime.combine(d, time.min)
    return d


def _incident_summary(inc: StudentIncident) -> str:
    desc = inc.description or ""
    if len(desc) > 80:
        desc = desc[:80] + "…"
    return f"{inc.incident_type}：{desc}" if desc else (inc.incident_type or "")


def _assessment_summary(asm: StudentAssessment) -> str:
    parts = [asm.assessment_type]
    if asm.domain:
        parts.append(asm.domain)
    if asm.rating:
        parts.append(asm.rating)
    content = asm.content or ""
    if len(content) > 60:
        content = content[:60] + "…"
    if content:
        parts.append(content)
    return "｜".join(p for p in parts if p)


def _change_log_summary(log: StudentChangeLog) -> str:
    parts = [log.event_type]
    if log.reason:
        parts.append(log.reason)
    return "：".join(p for p in parts if p)


def _fetch_incidents(
    session: Session,
    *,
    student_id: Optional[int],
    classroom_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
) -> list[StudentIncident]:
    q = session.query(StudentIncident)
    if student_id:
        q = q.filter(StudentIncident.student_id == student_id)
    if classroom_id:
        q = q.join(Student, StudentIncident.student_id == Student.id).filter(
            Student.classroom_id == classroom_id
        )
    if date_from:
        q = q.filter(
            StudentIncident.occurred_at >= datetime.combine(date_from, time.min)
        )
    if date_to:
        q = q.filter(StudentIncident.occurred_at <= datetime.combine(date_to, time.max))
    return q.all()


def _fetch_assessments(
    session: Session,
    *,
    student_id: Optional[int],
    classroom_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
) -> list[StudentAssessment]:
    q = session.query(StudentAssessment)
    if student_id:
        q = q.filter(StudentAssessment.student_id == student_id)
    if classroom_id:
        q = q.join(Student, StudentAssessment.student_id == Student.id).filter(
            Student.classroom_id == classroom_id
        )
    if date_from:
        q = q.filter(StudentAssessment.assessment_date >= date_from)
    if date_to:
        q = q.filter(StudentAssessment.assessment_date <= date_to)
    return q.all()


def _fetch_change_logs(
    session: Session,
    *,
    student_id: Optional[int],
    classroom_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
    school_year: Optional[int],
    semester: Optional[int],
) -> list[StudentChangeLog]:
    q = session.query(StudentChangeLog)
    if student_id:
        q = q.filter(StudentChangeLog.student_id == student_id)
    if classroom_id:
        q = q.filter(StudentChangeLog.classroom_id == classroom_id)
    if date_from:
        q = q.filter(StudentChangeLog.event_date >= date_from)
    if date_to:
        q = q.filter(StudentChangeLog.event_date <= date_to)
    if school_year is not None:
        q = q.filter(StudentChangeLog.school_year == school_year)
    if semester is not None:
        q = q.filter(StudentChangeLog.semester == semester)
    return q.all()


def _build_incident_item(inc: StudentIncident) -> dict[str, Any]:
    return {
        "record_type": "incident",
        "record_id": inc.id,
        "occurred_at": inc.occurred_at,
        "_ts": _to_datetime(inc.occurred_at),
        "student_id": inc.student_id,
        "summary": _incident_summary(inc),
        "severity": inc.severity,
        "parent_notified": bool(inc.parent_notified),
        "payload": {
            "incident_type": inc.incident_type,
            "severity": inc.severity,
            "description": inc.description,
            "action_taken": inc.action_taken,
            "parent_notified": bool(inc.parent_notified),
            "parent_notified_at": (
                inc.parent_notified_at.isoformat() if inc.parent_notified_at else None
            ),
        },
    }


def _build_assessment_item(asm: StudentAssessment) -> dict[str, Any]:
    return {
        "record_type": "assessment",
        "record_id": asm.id,
        "occurred_at": asm.assessment_date,
        "_ts": _to_datetime(asm.assessment_date),
        "student_id": asm.student_id,
        "summary": _assessment_summary(asm),
        "severity": None,
        "parent_notified": None,
        "payload": {
            "semester": asm.semester,
            "assessment_type": asm.assessment_type,
            "domain": asm.domain,
            "rating": asm.rating,
            "content": asm.content,
            "suggestions": asm.suggestions,
            "assessment_date": (
                asm.assessment_date.isoformat() if asm.assessment_date else None
            ),
        },
    }


def _build_change_log_item(log: StudentChangeLog) -> dict[str, Any]:
    return {
        "record_type": "change_log",
        "record_id": log.id,
        "occurred_at": log.event_date,
        "_ts": _to_datetime(log.event_date),
        "student_id": log.student_id,
        "_classroom_id": log.classroom_id,  # 覆蓋 student.classroom_id（change_log 自帶）
        "summary": _change_log_summary(log),
        "severity": None,
        "parent_notified": None,
        "payload": {
            "event_type": log.event_type,
            "event_date": log.event_date.isoformat() if log.event_date else None,
            "school_year": log.school_year,
            "semester": log.semester,
            "classroom_id": log.classroom_id,
            "from_classroom_id": log.from_classroom_id,
            "to_classroom_id": log.to_classroom_id,
            "reason": log.reason,
            "notes": log.notes,
            "source": log.source or "manual",
        },
    }


def list_timeline(
    session: Session,
    *,
    types: Optional[list[str]] = None,
    classroom_id: Optional[int] = None,
    student_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    school_year: Optional[int] = None,
    semester: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """跨三模型的學生紀錄時間軸。

    Parameters
    ----------
    types : list[str] | None
        要納入的紀錄類型，預設 None 代表全部。未知值會被忽略。
    classroom_id : int | None
        事件/評量透過 Student.classroom_id 過濾；異動透過 StudentChangeLog.classroom_id。
    student_id, date_from, date_to : 一般過濾條件
    school_year, semester : **僅影響 change_log**（事件/評量沒有此欄位）。
    page, page_size : 分頁（合併後切片）。

    Returns
    -------
    {items, total, page, page_size}
    """
    if types is None:
        enabled = set(RECORD_TYPES)
    else:
        enabled = {t for t in types if t in RECORD_TYPES}

    items: list[dict[str, Any]] = []

    if "incident" in enabled:
        for inc in _fetch_incidents(
            session,
            student_id=student_id,
            classroom_id=classroom_id,
            date_from=date_from,
            date_to=date_to,
        ):
            items.append(_build_incident_item(inc))

    if "assessment" in enabled:
        for asm in _fetch_assessments(
            session,
            student_id=student_id,
            classroom_id=classroom_id,
            date_from=date_from,
            date_to=date_to,
        ):
            items.append(_build_assessment_item(asm))

    if "change_log" in enabled:
        for log in _fetch_change_logs(
            session,
            student_id=student_id,
            classroom_id=classroom_id,
            date_from=date_from,
            date_to=date_to,
            school_year=school_year,
            semester=semester,
        ):
            items.append(_build_change_log_item(log))

    # 穩定排序：時間倒序 → record_type 字典序 → record_id 倒序
    items.sort(
        key=lambda it: (it["_ts"], it["record_type"], it["record_id"]),
        reverse=True,
    )

    total = len(items)

    # 分頁切片
    start = max(0, (page - 1) * page_size)
    end = start + page_size
    page_items = items[start:end]

    # 批次補 student_name / classroom_name
    student_ids = {it["student_id"] for it in page_items if it.get("student_id")}
    # change_log 帶自己的 classroom_id；事件/評量靠學生
    # 收集學生對應 classroom 先查出來
    student_rows = (
        session.query(Student.id, Student.name, Student.classroom_id)
        .filter(Student.id.in_(student_ids))
        .all()
        if student_ids
        else []
    )
    student_name_map = {sid: name for sid, name, _ in student_rows}
    student_classroom_map = {sid: cid for sid, _, cid in student_rows}

    classroom_ids: set[int] = set()
    for it in page_items:
        if it["record_type"] == "change_log":
            if it.get("_classroom_id"):
                classroom_ids.add(it["_classroom_id"])
        else:
            cid = student_classroom_map.get(it["student_id"])
            if cid:
                classroom_ids.add(cid)
    classroom_name_map = (
        {
            c.id: c.name
            for c in session.query(Classroom)
            .filter(Classroom.id.in_(classroom_ids))
            .all()
        }
        if classroom_ids
        else {}
    )

    results: list[dict[str, Any]] = []
    for it in page_items:
        if it["record_type"] == "change_log":
            cid = it.pop("_classroom_id", None)
        else:
            cid = student_classroom_map.get(it["student_id"])
        it.pop("_ts", None)
        # 時間統一為 ISO 字串方便前端處理
        ts = it["occurred_at"]
        if isinstance(ts, (datetime, date)):
            it["occurred_at"] = (
                ts.isoformat()
                if isinstance(ts, datetime)
                else datetime.combine(ts, time.min).isoformat()
            )
        it["student_name"] = student_name_map.get(it["student_id"], "")
        it["classroom_id"] = cid
        it["classroom_name"] = classroom_name_map.get(cid, "") if cid else ""
        results.append(it)

    return {
        "items": results,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
