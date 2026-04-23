"""招生漏斗服務 — 雙源拼接（visit + lifecycle）。"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models.activity import ParentInquiry
from models.classroom import Student
from models.recruitment import RecruitmentVisit
from services.analytics.constants import RETENTION_WINDOWS_DAYS, parse_roc_month

logger = logging.getLogger(__name__)


def _visit_in_range(visit: RecruitmentVisit, start: date, end: date) -> bool:
    parsed = parse_roc_month(visit.month)
    if parsed is None:
        return False
    y, m = parsed
    # 該月份的任何一天落入區間就算
    month_start = date(y, m, 1)
    if m == 12:
        month_end = date(y + 1, 1, 1)
    else:
        month_end = date(y, m + 1, 1)
    return not (month_end <= start or month_start > end)


def count_visit_side_stages(
    session: Session,
    *,
    start_date: date,
    end_date: date,
    grade_filter: Optional[str] = None,
    source_filter: Optional[str] = None,
) -> dict:
    """回傳 {'lead': int, 'deposit': int, 'enrolled': int}

    lead = visit 數 + ParentInquiry 數（兩源不去重）。
    deposit/enrolled 只計 visit 端。
    """
    q = session.query(RecruitmentVisit)
    if grade_filter:
        q = q.filter(RecruitmentVisit.grade == grade_filter)
    if source_filter:
        q = q.filter(RecruitmentVisit.source == source_filter)

    visits = [v for v in q.all() if _visit_in_range(v, start_date, end_date)]

    visit_count = len(visits)
    deposit_count = sum(1 for v in visits if v.has_deposit)
    enrolled_count = sum(1 for v in visits if v.enrolled)

    # ParentInquiry 不支援 grade/source filter（沒有對應欄位）
    inquiry_count = (
        session.query(ParentInquiry)
        .filter(
            ParentInquiry.created_at >= start_date,
            ParentInquiry.created_at < _exclusive_end(end_date),
        )
        .count()
    )

    return {
        "lead": visit_count + inquiry_count,
        "deposit": deposit_count,
        "enrolled": enrolled_count,
    }


def summarize_no_deposit_reasons(
    session: Session,
    *,
    start_date: date,
    end_date: date,
    grade_filter: Optional[str] = None,
    source_filter: Optional[str] = None,
) -> list[dict]:
    """彙整未預繳原因分布；只計入 has_deposit=False AND no_deposit_reason 非空。"""
    q = session.query(RecruitmentVisit).filter(
        RecruitmentVisit.has_deposit == False,
        RecruitmentVisit.no_deposit_reason.isnot(None),
        RecruitmentVisit.no_deposit_reason != "",
    )
    if grade_filter:
        q = q.filter(RecruitmentVisit.grade == grade_filter)
    if source_filter:
        q = q.filter(RecruitmentVisit.source == source_filter)

    visits = [v for v in q.all() if _visit_in_range(v, start_date, end_date)]

    counter: dict[str, int] = {}
    for v in visits:
        counter[v.no_deposit_reason] = counter.get(v.no_deposit_reason, 0) + 1
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(counter.items(), key=lambda x: -x[1])
    ]


def _exclusive_end(d: date) -> date:
    """end_date 是 inclusive，回傳 exclusive 的下一天用於 < 比較。"""
    return d + timedelta(days=1)


def count_student_side_stages(
    session: Session,
    *,
    start_date: date,
    end_date: date,
    today: date,
) -> dict:
    """回傳 {'active': int, 'retained_1m': int, 'retained_6m': int}

    active：enrollment_date 落入區間且曾入學（lifecycle 已過 enrolled）
    retained_1m：active 子集 + 距 today ≥ 30 天 + 未在 30 天內退/轉
    retained_6m：active 子集 + 距 today ≥ 180 天 + 未在 180 天內退/轉
    """
    # enrolled state 表示「已報到但尚未開學」(prospect 也排除) — 此處只計實際入學者
    enrolled_states = (
        "active",
        "on_leave",
        "graduated",
        "transferred",
        "withdrawn",
    )
    students = (
        session.query(Student)
        .filter(
            Student.lifecycle_status.in_(enrolled_states),
            Student.enrollment_date >= start_date,
            Student.enrollment_date <= end_date,
        )
        .all()
    )

    active_count = len(students)
    retained_1m = sum(
        1 for s in students if _is_retained(s, today, RETENTION_WINDOWS_DAYS["1m"])
    )
    retained_6m = sum(
        1 for s in students if _is_retained(s, today, RETENTION_WINDOWS_DAYS["6m"])
    )
    return {
        "active": active_count,
        "retained_1m": retained_1m,
        "retained_6m": retained_6m,
    }


def _is_retained(student: Student, today: date, window_days: int) -> bool:
    """是否在入學後仍留存 window_days 天。

    Note: graduated 學生視為「成功完成」而非「流失」，因此 graduated_date
    不影響此判斷 — 只看 withdrawal_date（退學/轉出）。
    """
    if student.enrollment_date is None:
        return False
    # 條件 1：入學日 + window 必須 ≤ today（窗口已成熟）
    threshold = student.enrollment_date + timedelta(days=window_days)
    if threshold > today:
        return False
    # 條件 2：未在窗口內退/轉
    if student.withdrawal_date is not None and student.withdrawal_date < threshold:
        return False
    return True
