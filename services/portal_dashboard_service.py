"""services/portal_dashboard_service.py — 教師首頁彙總用 helper

純查詢函式，無副作用；endpoint 統一在 api/portal/home.py 組合呼叫。

涵蓋：
- compute_consecutive_absences  連續缺席學生
- compute_upcoming_birthdays    近期生日
- compute_allergy_alerts        過敏警示
- count_pending_medications     今日未執行用藥數量
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy.orm import Session

from models.classroom import LIFECYCLE_ACTIVE, Student, StudentAttendance
from models.portfolio import (
    StudentAllergy,
    StudentMedicationLog,
    StudentMedicationOrder,
)

logger = logging.getLogger(__name__)


def _active_students_in_classroom(session: Session, classroom_id: int) -> list[Student]:
    return (
        session.query(Student)
        .filter(
            Student.classroom_id == classroom_id,
            Student.is_active.is_(True),
            Student.lifecycle_status == LIFECYCLE_ACTIVE,
        )
        .all()
    )


def compute_consecutive_absences(
    session: Session,
    *,
    classroom_id: int,
    today: date,
    threshold_days: int = 2,
    lookback_days: int = 14,
) -> list[dict]:
    """偵測連續缺席學生。

    從 (today - 1) 往前掃 lookback_days，對每位學生計算「最近連續缺席天數」。
    僅 status='缺席' 計入（請假類別不算）；超過 threshold_days 才回報。

    回傳：[{student_id, student_name, days, last_absent_date}, ...]
    """
    students = _active_students_in_classroom(session, classroom_id)
    if not students:
        return []
    student_by_id = {s.id: s for s in students}
    student_ids = list(student_by_id.keys())

    start = today - timedelta(days=lookback_days)
    end = today - timedelta(days=1)
    rows = (
        session.query(StudentAttendance)
        .filter(
            StudentAttendance.student_id.in_(student_ids),
            StudentAttendance.date >= start,
            StudentAttendance.date <= end,
        )
        .all()
    )
    by_student: dict[int, dict[date, str]] = {}
    for r in rows:
        by_student.setdefault(r.student_id, {})[r.date] = r.status

    results: list[dict] = []
    for sid, record in by_student.items():
        # 從昨日開始連續往前掃
        days = 0
        last_absent: date | None = None
        cursor = today - timedelta(days=1)
        while cursor >= start:
            status = record.get(cursor)
            if status == "缺席":
                days += 1
                if last_absent is None:
                    last_absent = cursor
                cursor -= timedelta(days=1)
                continue
            break
        if days >= threshold_days:
            results.append(
                {
                    "student_id": sid,
                    "student_name": student_by_id[sid].name,
                    "days": days,
                    "last_absent_date": (
                        last_absent.isoformat() if last_absent else None
                    ),
                }
            )
    # 連續天數高的排前面
    results.sort(key=lambda x: (-x["days"], x["student_name"]))
    return results


def compute_upcoming_birthdays(
    session: Session,
    *,
    classroom_id: int,
    today: date,
    window_days: int = 7,
) -> list[dict]:
    """回傳未來 window_days 內生日的學生（含今日）。

    跨 dialect：撈全班學生 birthday 後在 Python 端比對 month-day。
    回傳：[{student_id, student_name, birthday, age_turning, days_until}, ...]
    """
    students = _active_students_in_classroom(session, classroom_id)
    results: list[dict] = []
    for s in students:
        if not s.birthday:
            continue
        # 計算今年生日（若已過則用明年）
        try:
            this_year_bday = s.birthday.replace(year=today.year)
        except ValueError:
            # 2/29 → 平年退一日
            this_year_bday = s.birthday.replace(year=today.year, day=28)
        if this_year_bday < today:
            try:
                this_year_bday = s.birthday.replace(year=today.year + 1)
            except ValueError:
                this_year_bday = s.birthday.replace(year=today.year + 1, day=28)
        days_until = (this_year_bday - today).days
        if 0 <= days_until <= window_days:
            age_turning = this_year_bday.year - s.birthday.year
            results.append(
                {
                    "student_id": s.id,
                    "student_name": s.name,
                    "birthday": s.birthday.isoformat(),
                    "age_turning": age_turning,
                    "days_until": days_until,
                }
            )
    results.sort(key=lambda x: x["days_until"])
    return results


def compute_allergy_alerts(
    session: Session,
    *,
    classroom_id: int,
) -> list[dict]:
    """班級內 active 過敏紀錄列表（紅色 badge 用）。"""
    students = _active_students_in_classroom(session, classroom_id)
    if not students:
        return []
    student_by_id = {s.id: s for s in students}
    rows = (
        session.query(StudentAllergy)
        .filter(
            StudentAllergy.student_id.in_(student_by_id.keys()),
            StudentAllergy.active.is_(True),
        )
        .all()
    )
    grouped: dict[int, list[dict]] = {}
    for a in rows:
        grouped.setdefault(a.student_id, []).append(
            {
                "allergen": a.allergen,
                "severity": a.severity,
                "reaction": a.reaction_symptom,
            }
        )
    return [
        {
            "student_id": sid,
            "student_name": student_by_id[sid].name,
            "allergens": items,
        }
        for sid, items in grouped.items()
    ]


def count_pending_medications(
    session: Session,
    *,
    classroom_id: int,
    today: date,
) -> int:
    """班級今日尚未執行（pending）的 medication log 數。

    定義 pending：administered_at IS NULL AND skipped=false AND correction_of IS NULL，
    且關聯 order.order_date == today，且學生屬該班且 active。
    """
    students = _active_students_in_classroom(session, classroom_id)
    if not students:
        return 0
    student_ids = [s.id for s in students]
    return (
        session.query(StudentMedicationLog)
        .join(
            StudentMedicationOrder,
            StudentMedicationOrder.id == StudentMedicationLog.order_id,
        )
        .filter(
            StudentMedicationOrder.student_id.in_(student_ids),
            StudentMedicationOrder.order_date == today,
            StudentMedicationLog.administered_at.is_(None),
            StudentMedicationLog.skipped.is_(False),
            StudentMedicationLog.correction_of.is_(None),
        )
        .count()
    )


def has_attendance_today(
    session: Session,
    *,
    classroom_id: int,
    today: date,
) -> bool:
    """班級當日是否已有任何 attendance 紀錄（粗略視為「已點名」）。"""
    students = _active_students_in_classroom(session, classroom_id)
    if not students:
        return True  # 沒學生不需點名
    student_ids = [s.id for s in students]
    cnt = (
        session.query(StudentAttendance)
        .filter(
            StudentAttendance.student_id.in_(student_ids),
            StudentAttendance.date == today,
        )
        .count()
    )
    return cnt > 0
