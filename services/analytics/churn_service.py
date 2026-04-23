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
from services.analytics.constants import (
    CHURN_CONSECUTIVE_ABSENCE_DAYS,
)

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
