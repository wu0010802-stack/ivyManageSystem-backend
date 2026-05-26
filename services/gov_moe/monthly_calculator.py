"""MOE Phase 2 月報計算純函式。

純函式（無 session）：calc_age_group, is_foreign
DB query helper：working_days_in_month, classroom_at_month_end
聚合：compute_student_attendance_for_month, build_snapshot_rows
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from dateutil.relativedelta import relativedelta
from sqlalchemy import or_
from sqlalchemy.orm import Session

from models.classroom import Student, StudentAttendance
from models.event import Holiday, WorkdayOverride
from models.student_transfer import StudentClassroomTransfer

TAIWAN_ALIASES = {"本國", "台灣", "中華民國", "中華民國（台灣）", "ROC"}
ATTENDED_STATUSES = {"出席", "遲到"}
EXCLUDED_LIFECYCLE = {"prospect"}


def calc_age_group(birthday: date | None, ref_date: date) -> str:
    """以 ref_date 滿歲切 2-3/3-4/4-5/5-6 四段。

    < 2 歲（罕見資料）歸 2-3；> 5 歲（含超齡）歸 5-6（fallback 防呆）。
    birthday 為 None → '未知'。
    """
    if birthday is None:
        return "未知"
    age = relativedelta(ref_date, birthday).years
    if age <= 2:
        return "2-3"
    if age == 3:
        return "3-4"
    if age == 4:
        return "4-5"
    return "5-6"


def is_foreign(nationality: str | None) -> bool:
    """nationality 為 NULL/空 視為本國（保守不誤報）。"""
    if not nationality:
        return False
    return nationality.strip() not in TAIWAN_ALIASES


def working_days_in_month(session: Session, year: int, month: int) -> set[date]:
    """月份工作日集合 = weekday(Mon-Fri) - 假日 + 補班日。"""
    first = date(year, month, 1)
    last = first + relativedelta(months=1, days=-1)

    days = {
        first + timedelta(days=d)
        for d in range((last - first).days + 1)
        if (first + timedelta(days=d)).weekday() < 5
    }

    holiday_dates = {
        row[0]
        for row in session.query(Holiday.date)
        .filter(
            Holiday.is_active == True,  # noqa: E712
            Holiday.date.between(first, last),
        )
        .all()
    }

    override_dates = {
        row[0]
        for row in session.query(WorkdayOverride.date)
        .filter(
            WorkdayOverride.is_active == True,  # noqa: E712
            WorkdayOverride.date.between(first, last),
        )
        .all()
    }

    return (days - holiday_dates) | override_dates


def classroom_at_month_end(
    session: Session,
    student_id: int,
    snapshot_date: date,
) -> int | None:
    """月底班級歸屬：先查 transfer 表，無紀錄則 fallback student.classroom_id。"""
    last_transfer = (
        session.query(StudentClassroomTransfer)
        .filter(
            StudentClassroomTransfer.student_id == student_id,
            StudentClassroomTransfer.transferred_at
            <= datetime.combine(snapshot_date, time.max),
        )
        .order_by(StudentClassroomTransfer.transferred_at.desc())
        .first()
    )
    if last_transfer:
        return last_transfer.to_classroom_id
    return session.query(Student.classroom_id).filter(Student.id == student_id).scalar()


def compute_student_attendance_for_month(
    session: Session,
    student: Student,
    year: int,
    month: int,
    working_days: set[date],
) -> tuple[int, int]:
    """回傳 (expected_days, actual_days)。"""
    first = date(year, month, 1)
    last = first + relativedelta(months=1, days=-1)

    student_start = max(first, student.enrollment_date or first)
    student_end_candidates = [
        last,
        student.withdrawal_date or last,
        student.graduation_date or last,
    ]
    student_end = min(student_end_candidates)

    if student_start > student_end:
        return 0, 0

    student_days = {d for d in working_days if student_start <= d <= student_end}
    expected = len(student_days)

    if expected == 0:
        return 0, 0

    attended_rows = (
        session.query(StudentAttendance.date)
        .filter(
            StudentAttendance.student_id == student.id,
            StudentAttendance.date.between(student_start, student_end),
            StudentAttendance.status.in_(list(ATTENDED_STATUSES)),
        )
        .all()
    )
    attended_dates = {row[0] for row in attended_rows}
    actual = len(attended_dates & student_days)

    return expected, actual


@dataclass
class _StudentAggregate:
    total: int = 0
    male: int = 0
    female: int = 0
    disadvantaged: int = 0
    disability: int = 0
    indigenous: int = 0
    foreign: int = 0
    expected_days: int = 0
    actual_days: int = 0


def build_snapshot_rows(
    session: Session,
    year: int,
    month: int,
    *,
    generated_by: str,
) -> tuple[list[dict], list[dict]]:
    """產生 (group_rows, student_details)。group_rows 為 snapshot 表 row payload；
    student_details 為 per-student 明細（不寫表）。
    """
    first = date(year, month, 1)
    last = first + relativedelta(months=1, days=-1)

    candidates = (
        session.query(Student)
        .filter(
            ~Student.lifecycle_status.in_(list(EXCLUDED_LIFECYCLE)),
            or_(Student.enrollment_date.is_(None), Student.enrollment_date <= last),
            or_(Student.withdrawal_date.is_(None), Student.withdrawal_date >= first),
            or_(Student.graduation_date.is_(None), Student.graduation_date >= first),
        )
        .all()
    )

    wd = working_days_in_month(session, year, month)
    now = datetime.now()  # noqa: DTZ005

    groups: dict[tuple[int | None, str], _StudentAggregate] = defaultdict(
        _StudentAggregate
    )
    student_details: list[dict] = []

    for s in candidates:
        expected, actual = compute_student_attendance_for_month(
            session, s, year, month, wd
        )
        if expected == 0 and actual == 0:
            continue
        ag = calc_age_group(s.birthday, last)
        cls_id = classroom_at_month_end(session, s.id, last)
        key = (cls_id, ag)
        agg = groups[key]
        agg.total += 1
        if s.gender == "男":
            agg.male += 1
        elif s.gender == "女":
            agg.female += 1
        if s.is_disadvantaged:
            agg.disadvantaged += 1
        if s.disability_type:
            agg.disability += 1
        if s.indigenous_status:
            agg.indigenous += 1
        if is_foreign(s.nationality):
            agg.foreign += 1
        agg.expected_days += expected
        agg.actual_days += actual

        student_details.append(
            {
                "student_id": s.id,
                "student_no": s.student_id,
                "name": s.name,
                "id_number": s.id_number,
                "classroom_id": cls_id,
                "age_group": ag,
                "expected_days": expected,
                "actual_days": actual,
                "attendance_rate_pct": (
                    round(actual / expected * 100, 2) if expected else 0
                ),
                "is_disadvantaged": bool(s.is_disadvantaged),
            }
        )

    rows: list[dict] = []
    for (cls_id, ag), agg in groups.items():
        rate = (
            round(agg.actual_days / agg.expected_days * 10000)
            if agg.expected_days
            else 0
        )
        rows.append(
            {
                "year": year,
                "month": month,
                "classroom_id": cls_id,
                "age_group": ag,
                "total_count": agg.total,
                "male_count": agg.male,
                "female_count": agg.female,
                "disadvantaged_count": agg.disadvantaged,
                "disability_count": agg.disability,
                "indigenous_count": agg.indigenous,
                "foreign_count": agg.foreign,
                "expected_attendance_days": agg.expected_days,
                "actual_attendance_days": agg.actual_days,
                "attendance_rate": rate,  # 萬分比整數，e.g. 9543 = 95.43%
                "snapshot_date": last,
                "generated_at": now,
                "generated_by": generated_by,
            }
        )

    return rows, student_details
