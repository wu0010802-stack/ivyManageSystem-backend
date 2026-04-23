"""流失預警服務 — A 訊號偵測（連續缺勤）。

設計決策：
- 學生請假直接反映在 StudentAttendance.status（"病假"/"事假"），
  沒有獨立的學生假單表（LeaveRecord 為員工專用），因此不查 LeaveRecord。
- 僅 "缺席" 計入連續缺勤串；"病假"/"事假"/"出席"/"遲到" 皆中斷缺勤串。
- 工作日判斷採簡易版（weekday() < 5）；精確假日表（services.workday_rules）
  需 load holiday/makeup map，MVP 不引入依賴。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.orm import Session

from models.classroom import Student, StudentAttendance
from models.fees import FeeItem, StudentFeeRecord
from models.student_log import StudentChangeLog
from services.analytics.constants import (
    CHURN_CONSECUTIVE_ABSENCE_DAYS,
    CHURN_FEE_OVERDUE_DAYS,
    CHURN_ON_LEAVE_DAYS,
    term_start_date,
)
from utils.academic import resolve_current_academic_term

logger = logging.getLogger(__name__)

# 僅此 status 計入連續缺勤串
_ABSENT_STATUS = "缺席"


def _is_workday(d: date) -> bool:
    """簡易工作日判斷：週一~週五為工作日（weekday 0-4）。

    NOTE: 既有 services.workday_rules 提供更精確的假日表，
    但其 API 需要先 load 整個 holiday/makeup map；MVP 採簡易版。
    後續若需精確判斷，改為 load_day_rule_maps + classify_day。
    """
    return d.weekday() < 5


def _last_n_workdays(today: date, n: int) -> list[date]:
    """從 today 往前抓最多 n 個工作日（含 today 若為工作日），由舊到新排序。"""
    days: list[date] = []
    cursor = today
    while len(days) < n:
        if _is_workday(cursor):
            days.append(cursor)
        cursor -= timedelta(days=1)
        if (today - cursor).days > 60:
            break
    return list(reversed(days))


def _build_unrecorded_class_days(
    students: list,
    by_student: dict,
    candidate_days: list,
) -> set:
    """若某天某班所有 active 學生皆無紀錄或皆為「缺席」→ 視為漏點名，回傳 (cls_id, day) 集合。"""
    by_classroom: dict = {}
    for s in students:
        if s.classroom_id is None:
            continue
        by_classroom.setdefault(s.classroom_id, []).append(s.id)

    unrecorded: set = set()
    for cls_id, sids in by_classroom.items():
        for day in candidate_days:
            statuses = [by_student.get(sid, {}).get(day) for sid in sids]
            # 若有任一學生有「非缺席」的紀錄（出席/遲到/病假/事假），代表老師有點名
            non_absent = [
                st for st in statuses if st is not None and st != _ABSENT_STATUS
            ]
            if not non_absent:
                unrecorded.add((cls_id, day))
    return unrecorded


def detect_signal_consecutive_absence(
    session: Session,
    *,
    today: date,
) -> list[dict]:
    """偵測 A 訊號：active 學生末端連續 ≥ N 個工作日「缺席」。

    回傳 [{student_id, type, severity, detail}, ...]

    規則：
    1. 只計 "缺席" status；"病假"/"事假"/"出席"/"遲到" 皆中斷缺勤串。
    2. 過濾整班漏點名：若某天某班所有 active 學生皆無紀錄/皆缺席，略過該天。
    3. 從 today 往前掃描，遇到非缺席即停（末端連續缺勤語意）。
    """
    window_days = CHURN_CONSECUTIVE_ABSENCE_DAYS + 4
    candidate_days = _last_n_workdays(today, window_days)
    if not candidate_days:
        return []
    earliest = candidate_days[0]

    students = session.query(Student).filter(Student.lifecycle_status == "active").all()
    student_ids = [s.id for s in students]
    if not student_ids:
        return []

    att_rows = (
        session.query(StudentAttendance)
        .filter(
            StudentAttendance.student_id.in_(student_ids),
            StudentAttendance.date >= earliest,
            StudentAttendance.date <= today,
        )
        .all()
    )
    by_student: dict[int, dict[date, str]] = {}
    for r in att_rows:
        by_student.setdefault(r.student_id, {})[r.date] = r.status

    unrecorded = _build_unrecorded_class_days(students, by_student, candidate_days)

    triggered = []
    for s in students:
        statuses = by_student.get(s.id, {})
        streak = 0
        absence_dates: list[date] = []
        for day in reversed(candidate_days):
            if (s.classroom_id, day) in unrecorded:
                continue  # 整班漏點名日，略過
            status = statuses.get(day)
            if status == _ABSENT_STATUS:
                streak += 1
                absence_dates.append(day)
            else:
                break  # 末端必須連續，遇非缺席就停
        if streak >= CHURN_CONSECUTIVE_ABSENCE_DAYS:
            absence_dates.sort()
            triggered.append(
                {
                    "student_id": s.id,
                    "type": "consecutive_absence",
                    "severity": "high",
                    "detail": (
                        f"連續缺勤 {streak} 天"
                        f"（{absence_dates[0]} ~ {absence_dates[-1]}）"
                    ),
                }
            )
    return triggered


def detect_signal_long_on_leave(
    session: Session,
    *,
    today: date,
) -> list[dict]:
    """C 訊號：on_leave 學生最近一筆「休學」事件距今 ≥ N 天。"""
    students = (
        session.query(Student).filter(Student.lifecycle_status == "on_leave").all()
    )
    triggered = []
    for s in students:
        last_log = (
            session.query(StudentChangeLog)
            .filter(
                StudentChangeLog.student_id == s.id,
                StudentChangeLog.event_type == "休學",
            )
            .order_by(StudentChangeLog.event_date.desc())
            .first()
        )
        if last_log is None:
            continue
        days = (today - last_log.event_date).days
        if days >= CHURN_ON_LEAVE_DAYS:
            triggered.append(
                {
                    "student_id": s.id,
                    "type": "long_on_leave",
                    "severity": "medium",
                    "detail": f"休學已 {days} 天未復學（自 {last_log.event_date}）",
                }
            )
    return triggered


def detect_signal_fee_overdue(
    session: Session,
    *,
    today: date,
) -> list[dict]:
    """D 訊號：當期未繳費 ≥ 14 天。

    當期：utils.academic.resolve_current_academic_term(today) 回傳 (year_民國, semester)
    轉成 FeeItem.period 西元字串 "{year+1911}-{semester}" 進行比對。
    """
    year_roc, semester = resolve_current_academic_term(today)
    current_period = f"{year_roc + 1911}-{semester}"

    overdue_records = (
        session.query(StudentFeeRecord, FeeItem)
        .join(FeeItem, StudentFeeRecord.fee_item_id == FeeItem.id)
        .join(Student, StudentFeeRecord.student_id == Student.id)
        .filter(
            StudentFeeRecord.payment_date.is_(None),
            FeeItem.period == current_period,
            Student.lifecycle_status.in_(("active", "on_leave")),
        )
        .all()
    )

    triggered_by_student: dict[int, dict] = {}
    for rec, item in overdue_records:
        start = term_start_date(item.period)
        if start is None:
            continue
        # 已逾期天數需 ≥ 14（threshold = start + 14；today >= threshold 即觸發）
        threshold = start + timedelta(days=CHURN_FEE_OVERDUE_DAYS)
        if today < threshold:
            continue
        actual_overdue_days = (today - start).days
        severity = "high" if actual_overdue_days >= 30 else "medium"
        existing = triggered_by_student.get(rec.student_id)
        if existing is None or _severity_rank(severity) > _severity_rank(
            existing["severity"]
        ):
            triggered_by_student[rec.student_id] = {
                "student_id": rec.student_id,
                "type": "fee_overdue",
                "severity": severity,
                "detail": f"學費逾期 {actual_overdue_days} 天，項目：{item.name}",
            }
    return list(triggered_by_student.values())


def _severity_rank(s: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(s, 0)
