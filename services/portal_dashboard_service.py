"""services/portal_dashboard_service.py — 教師首頁彙總用 helper

純查詢函式，無副作用；endpoint 統一在 api/portal/home.py 組合呼叫。

涵蓋：
- compute_consecutive_absences  連續缺席學生
- compute_upcoming_birthdays    近期生日
- compute_allergy_alerts        過敏警示
- count_pending_medications     今日未執行用藥數量
- has_attendance_today          當日是否已點名

所有函式均支援 dispatch by input type：
  classroom_id: int      → 向後相容，回傳原型別
  classroom_id: list[int] → 回傳 dict[int, T]，缺值 fill 空值
  classroom_id: []        → 回傳 {}
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func
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


# ════════════════════════════════════════════════════════════════════════
# compute_consecutive_absences
# ════════════════════════════════════════════════════════════════════════


def compute_consecutive_absences(
    session: Session,
    *,
    classroom_id: int | list[int],
    today: date,
    threshold_days: int = 2,
    lookback_days: int = 14,
) -> list[dict] | dict[int, list[dict]]:
    """偵測連續缺席學生。

    classroom_id: int       → list[dict]（向後相容）
    classroom_id: list[int] → dict[int, list[dict]]
    classroom_id: []        → {}
    """
    if isinstance(classroom_id, list):
        if not classroom_id:
            return {}
        return _compute_consecutive_absences_batch(
            session,
            classroom_id,
            today,
            threshold_days=threshold_days,
            lookback_days=lookback_days,
        )
    return _compute_consecutive_absences_single(
        session,
        classroom_id,
        today,
        threshold_days=threshold_days,
        lookback_days=lookback_days,
    )


def _compute_consecutive_absences_single(
    session: Session,
    classroom_id: int,
    today: date,
    *,
    threshold_days: int = 2,
    lookback_days: int = 14,
) -> list[dict]:
    """從 (today - 1) 往前掃 lookback_days，對每位學生計算「最近連續缺席天數」。
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


def _compute_consecutive_absences_batch(
    session: Session,
    classroom_ids: list[int],
    today: date,
    *,
    threshold_days: int = 2,
    lookback_days: int = 14,
) -> dict[int, list[dict]]:
    """Fallback dict-comp：語意複雜（per-student 連續天數），逐班呼叫 _single。

    NOTE: 若後續確認是性能瓶頸，再優化為跨班單 query。
    """
    return {
        cid: _compute_consecutive_absences_single(
            session,
            cid,
            today,
            threshold_days=threshold_days,
            lookback_days=lookback_days,
        )
        for cid in classroom_ids
    }


# ════════════════════════════════════════════════════════════════════════
# compute_upcoming_birthdays
# ════════════════════════════════════════════════════════════════════════


def compute_upcoming_birthdays(
    session: Session,
    *,
    classroom_id: int | list[int],
    today: date,
    window_days: int = 7,
) -> list[dict] | dict[int, list[dict]]:
    """回傳未來 window_days 內生日的學生（含今日）。

    classroom_id: int       → list[dict]（向後相容）
    classroom_id: list[int] → dict[int, list[dict]]
    classroom_id: []        → {}
    """
    if isinstance(classroom_id, list):
        if not classroom_id:
            return {}
        return _compute_upcoming_birthdays_batch(
            session, classroom_id, today, window_days=window_days
        )
    return _compute_upcoming_birthdays_single(
        session, classroom_id, today, window_days=window_days
    )


def _compute_upcoming_birthdays_single(
    session: Session,
    classroom_id: int,
    today: date,
    *,
    window_days: int = 7,
) -> list[dict]:
    """跨 dialect：撈全班學生 birthday 後在 Python 端比對 month-day。
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


def _compute_upcoming_birthdays_batch(
    session: Session,
    classroom_ids: list[int],
    today: date,
    *,
    window_days: int = 7,
) -> dict[int, list[dict]]:
    """Fallback dict-comp：birthday 需在 Python 端計算，逐班呼叫 _single。

    NOTE: 若後續確認是性能瓶頸，再優化為跨班單 query。
    """
    return {
        cid: _compute_upcoming_birthdays_single(
            session, cid, today, window_days=window_days
        )
        for cid in classroom_ids
    }


# ════════════════════════════════════════════════════════════════════════
# compute_allergy_alerts
# ════════════════════════════════════════════════════════════════════════


def compute_allergy_alerts(
    session: Session,
    *,
    classroom_id: int | list[int],
) -> list[dict] | dict[int, list[dict]]:
    """班級內 active 過敏紀錄列表（紅色 badge 用）。

    classroom_id: int       → list[dict]（向後相容）
    classroom_id: list[int] → dict[int, list[dict]]
    classroom_id: []        → {}
    """
    if isinstance(classroom_id, list):
        if not classroom_id:
            return {}
        return _compute_allergy_alerts_batch(session, classroom_id)
    return _compute_allergy_alerts_single(session, classroom_id)


def _compute_allergy_alerts_single(
    session: Session,
    classroom_id: int,
) -> list[dict]:
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


def _compute_allergy_alerts_batch(
    session: Session,
    classroom_ids: list[int],
) -> dict[int, list[dict]]:
    """真 IN clause 一次取所有班級的過敏學生，再按 classroom_id 分組。"""
    # 先取所有相關班級的 active 學生（一次 query）
    students = (
        session.query(Student)
        .filter(
            Student.classroom_id.in_(classroom_ids),
            Student.is_active.is_(True),
            Student.lifecycle_status == LIFECYCLE_ACTIVE,
        )
        .all()
    )
    if not students:
        return {cid: [] for cid in classroom_ids}

    student_by_id = {s.id: s for s in students}
    student_ids = list(student_by_id.keys())

    # 一次取所有 active 過敏紀錄
    rows = (
        session.query(StudentAllergy)
        .filter(
            StudentAllergy.student_id.in_(student_ids),
            StudentAllergy.active.is_(True),
        )
        .all()
    )

    # 按 student_id 分組過敏紀錄
    allergy_by_student: dict[int, list[dict]] = {}
    for a in rows:
        allergy_by_student.setdefault(a.student_id, []).append(
            {
                "allergen": a.allergen,
                "severity": a.severity,
                "reaction": a.reaction_symptom,
            }
        )

    # 按 classroom_id 組結果
    result_by_classroom: dict[int, list[dict]] = {cid: [] for cid in classroom_ids}
    for sid, allergens in allergy_by_student.items():
        s = student_by_id[sid]
        result_by_classroom[s.classroom_id].append(
            {
                "student_id": sid,
                "student_name": s.name,
                "allergens": allergens,
            }
        )
    return result_by_classroom


# ════════════════════════════════════════════════════════════════════════
# count_pending_medications
# ════════════════════════════════════════════════════════════════════════


def count_pending_medications(
    session: Session,
    *,
    classroom_id: int | list[int],
    today: date,
) -> int | dict[int, int]:
    """班級今日尚未執行（pending）的 medication log 數。

    classroom_id: int       → int（向後相容）
    classroom_id: list[int] → dict[int, int]
    classroom_id: []        → {}
    """
    if isinstance(classroom_id, list):
        if not classroom_id:
            return {}
        return _count_pending_medications_batch(session, classroom_id, today)
    return _count_pending_medications_single(session, classroom_id, today)


def _count_pending_medications_single(
    session: Session,
    classroom_id: int,
    today: date,
) -> int:
    """定義 pending：administered_at IS NULL AND skipped=false AND correction_of IS NULL，
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


def _count_pending_medications_batch(
    session: Session,
    classroom_ids: list[int],
    today: date,
) -> dict[int, int]:
    """真 IN + GROUP BY：一次 query 取各班 pending medication 計數。"""
    # 先取所有相關班級的 active 學生
    students = (
        session.query(Student)
        .filter(
            Student.classroom_id.in_(classroom_ids),
            Student.is_active.is_(True),
            Student.lifecycle_status == LIFECYCLE_ACTIVE,
        )
        .all()
    )
    if not students:
        return {cid: 0 for cid in classroom_ids}

    student_ids = [s.id for s in students]
    # student_id → classroom_id 映射
    classroom_by_student = {s.id: s.classroom_id for s in students}

    # 一次 query：取所有符合條件的 (student_id, count)
    rows = (
        session.query(
            StudentMedicationOrder.student_id,
            func.count(StudentMedicationLog.id),
        )
        .join(
            StudentMedicationLog,
            StudentMedicationLog.order_id == StudentMedicationOrder.id,
        )
        .filter(
            StudentMedicationOrder.student_id.in_(student_ids),
            StudentMedicationOrder.order_date == today,
            StudentMedicationLog.administered_at.is_(None),
            StudentMedicationLog.skipped.is_(False),
            StudentMedicationLog.correction_of.is_(None),
        )
        .group_by(StudentMedicationOrder.student_id)
        .all()
    )

    # 累加到 classroom 維度
    counts: dict[int, int] = {cid: 0 for cid in classroom_ids}
    for student_id, cnt in rows:
        cid = classroom_by_student.get(student_id)
        if cid is not None:
            counts[cid] = counts.get(cid, 0) + cnt
    return counts


# ════════════════════════════════════════════════════════════════════════
# has_attendance_today
# ════════════════════════════════════════════════════════════════════════


def has_attendance_today(
    session: Session,
    *,
    classroom_id: int | list[int],
    today: date,
) -> bool | dict[int, bool]:
    """班級當日是否已有任何 attendance 紀錄（粗略視為「已點名」）。

    classroom_id: int       → bool（向後相容）
    classroom_id: list[int] → dict[int, bool]
    classroom_id: []        → {}
    """
    if isinstance(classroom_id, list):
        if not classroom_id:
            return {}
        return _has_attendance_today_batch(session, classroom_id, today)
    return _has_attendance_today_single(session, classroom_id, today)


def _has_attendance_today_single(
    session: Session,
    classroom_id: int,
    today: date,
) -> bool:
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


def _has_attendance_today_batch(
    session: Session,
    classroom_ids: list[int],
    today: date,
) -> dict[int, bool]:
    """真 IN + GROUP BY：一次 query 取各班今日是否有 attendance 紀錄。

    空班（無 active 學生）視為已點名（True），與 _single 行為一致。
    """
    # 先取所有相關班級的 active 學生
    students = (
        session.query(Student)
        .filter(
            Student.classroom_id.in_(classroom_ids),
            Student.is_active.is_(True),
            Student.lifecycle_status == LIFECYCLE_ACTIVE,
        )
        .all()
    )

    # 無學生的班級：視為已點名（True）
    classroom_ids_with_students = {s.classroom_id for s in students}
    empty_classrooms = set(classroom_ids) - classroom_ids_with_students

    if not students:
        # 全部班級都無學生
        return {cid: True for cid in classroom_ids}

    student_ids = [s.id for s in students]
    # student_id → classroom_id 映射
    classroom_by_student = {s.id: s.classroom_id for s in students}

    # 一次 query：取今日有 attendance 的 student_ids
    rows = (
        session.query(StudentAttendance.student_id)
        .filter(
            StudentAttendance.student_id.in_(student_ids),
            StudentAttendance.date == today,
        )
        .distinct()
        .all()
    )

    # 找出有點名的班級 set
    called_classroom_ids: set[int] = set()
    for (student_id,) in rows:
        cid = classroom_by_student.get(student_id)
        if cid is not None:
            called_classroom_ids.add(cid)

    result: dict[int, bool] = {}
    for cid in classroom_ids:
        if cid in empty_classrooms:
            result[cid] = True  # 無學生不需點名
        else:
            result[cid] = cid in called_classroom_ids
    return result
