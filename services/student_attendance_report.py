"""
學生出席報表計算 service。
"""

from __future__ import annotations

import calendar as cal_module
from collections import Counter, defaultdict
from datetime import date, timedelta

from models.database import Classroom, Holiday, Student, StudentAttendance, User
from services.report_cache_service import report_cache_service

VALID_STATUSES = ("出席", "缺席", "病假", "事假", "遲到")
ATTENDED_STATUSES = {"出席", "遲到"}
ABSENCE_STATUS = "缺席"
ALERT_STREAK_THRESHOLD = 3
WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"]
MONTHLY_ATTENDANCE_REPORT_CACHE_TTL_SECONDS = 1800


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    _, last_day = cal_module.monthrange(year, month)
    return date(year, month, 1), date(year, month, last_day)


def _daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _student_active_on(student: Student, target_date: date) -> bool:
    if student.enrollment_date and target_date < student.enrollment_date:
        return False
    if student.graduation_date and target_date > student.graduation_date:
        return False
    return True


def build_attendance_summary(total_students: int, raw_status_counts: dict[str, int]) -> dict:
    """將學生出席狀態分佈轉成管理端/首頁共用摘要。"""
    status_counts = Counter({status: raw_status_counts.get(status, 0) for status in VALID_STATUSES})
    recorded_count = sum(status_counts.values())
    on_campus_count = sum(status_counts[status] for status in ATTENDED_STATUSES)
    leave_count = status_counts["病假"] + status_counts["事假"]
    unmarked_count = max(total_students - recorded_count, 0)

    return {
        "total_students": total_students,
        "recorded_count": recorded_count,
        "on_campus_count": on_campus_count,
        "present_count": status_counts["出席"],
        "late_count": status_counts["遲到"],
        "absent_count": status_counts["缺席"],
        "leave_count": leave_count,
        "sick_leave_count": status_counts["病假"],
        "personal_leave_count": status_counts["事假"],
        "unmarked_count": unmarked_count,
        "record_completion_rate": round((recorded_count / total_students) * 100, 1) if total_students else 0,
        "attendance_rate": round((on_campus_count / total_students) * 100, 1) if total_students else 0,
    }


def _resolve_rollcall_status(recorded_count: int, student_count: int) -> str:
    if recorded_count == 0:
        return "unstarted"
    if recorded_count < student_count:
        return "partial"
    return "complete"


def build_daily_classroom_overview(session, target_date: date) -> dict:
    """建立指定日期的各班出席總覽。"""
    classrooms = (
        session.query(Classroom)
        .filter(Classroom.is_active == True)
        .order_by(Classroom.name)
        .all()
    )
    classroom_ids = [classroom.id for classroom in classrooms]

    students = []
    if classroom_ids:
        students = (
            session.query(Student)
            .filter(
                Student.classroom_id.in_(classroom_ids),
                Student.is_active == True,
            )
            .order_by(Student.classroom_id, Student.student_id)
            .all()
        )

    classroom_students = defaultdict(list)
    student_map = {}
    for student in students:
        if not _student_active_on(student, target_date):
            continue
        classroom_students[student.classroom_id].append(student)
        student_map[student.id] = student

    attendance_records = []
    student_ids = list(student_map.keys())
    if student_ids:
        attendance_records = (
            session.query(StudentAttendance)
            .filter(
                StudentAttendance.date == target_date,
                StudentAttendance.student_id.in_(student_ids),
            )
            .all()
        )

    user_ids = sorted({record.recorded_by for record in attendance_records if record.recorded_by})
    user_map = {}
    if user_ids:
        user_map = {
            user.id: user.username
            for user in session.query(User).filter(User.id.in_(user_ids)).all()
        }

    class_status_counts = defaultdict(Counter)
    class_records = defaultdict(list)
    total_status_counts = Counter()
    for record in attendance_records:
        student = student_map.get(record.student_id)
        if not student or record.status not in VALID_STATUSES:
            continue
        class_status_counts[student.classroom_id][record.status] += 1
        total_status_counts[record.status] += 1
        class_records[student.classroom_id].append(record)

    classrooms_payload = []
    total_students = 0
    for classroom in classrooms:
        student_count = len(classroom_students.get(classroom.id, []))
        total_students += student_count

        summary = build_attendance_summary(student_count, class_status_counts.get(classroom.id, {}))
        latest_record = max(
            class_records.get(classroom.id, []),
            key=lambda record: record.updated_at or record.created_at,
            default=None,
        )
        last_recorded_at = None
        last_recorded_by = None
        if latest_record:
            last_recorded_at = (latest_record.updated_at or latest_record.created_at).isoformat()
            if latest_record.recorded_by:
                last_recorded_by = user_map.get(latest_record.recorded_by)

        classrooms_payload.append({
            "classroom_id": classroom.id,
            "classroom_name": classroom.name,
            "student_count": student_count,
            "recorded_count": summary["recorded_count"],
            "on_campus_count": summary["on_campus_count"],
            "present_count": summary["present_count"],
            "late_count": summary["late_count"],
            "absent_count": summary["absent_count"],
            "leave_count": summary["leave_count"],
            "unmarked_count": summary["unmarked_count"],
            "record_completion_rate": summary["record_completion_rate"],
            "attendance_rate": summary["attendance_rate"],
            "last_recorded_at": last_recorded_at,
            "last_recorded_by": last_recorded_by,
            "rollcall_status": _resolve_rollcall_status(summary["recorded_count"], student_count),
        })

    return {
        "date": target_date.isoformat(),
        "totals": build_attendance_summary(total_students, total_status_counts),
        "classrooms": classrooms_payload,
    }


def _compute_monthly_attendance_report(session, classroom_id: int, year: int, month: int) -> dict:
    start_date, end_date = _month_bounds(year, month)

    classroom = (
        session.query(Classroom)
        .filter(Classroom.id == classroom_id)
        .first()
    )
    if not classroom:
        raise ValueError("班級不存在")

    students = (
        session.query(Student)
        .filter(Student.classroom_id == classroom_id, Student.is_active == True)
        .order_by(Student.student_id)
        .all()
    )
    student_ids = [student.id for student in students]

    holidays = (
        session.query(Holiday)
        .filter(
            Holiday.is_active == True,
            Holiday.date >= start_date,
            Holiday.date <= end_date,
        )
        .all()
    )
    holiday_map = {holiday.date: holiday.name for holiday in holidays}

    attendance_records = []
    if student_ids:
        attendance_records = (
            session.query(StudentAttendance)
            .filter(
                StudentAttendance.student_id.in_(student_ids),
                StudentAttendance.date >= start_date,
                StudentAttendance.date <= end_date,
            )
            .all()
        )

    record_map = {
        (record.student_id, record.date): record
        for record in attendance_records
    }

    calendar_days = []
    school_days = []
    for current_date in _daterange(start_date, end_date):
        weekday = current_date.weekday()
        is_weekend = weekday >= 5
        holiday_name = holiday_map.get(current_date)
        is_school_day = (not is_weekend) and (holiday_name is None)
        day_payload = {
            "date": current_date.isoformat(),
            "day": current_date.day,
            "weekday": WEEKDAY_LABELS[weekday],
            "is_weekend": is_weekend,
            "is_holiday": holiday_name is not None,
            "holiday_name": holiday_name,
            "is_school_day": is_school_day,
        }
        calendar_days.append(day_payload)
        if is_school_day:
            school_days.append(current_date)

    classroom_status_totals = {status: 0 for status in VALID_STATUSES}
    total_student_school_days = 0
    total_attended_days = 0
    total_recorded_days = 0
    flagged_students = []
    student_rows = []

    for student in students:
        counts = {status: 0 for status in VALID_STATUSES}
        applicable_school_days = [d for d in school_days if _student_active_on(student, d)]
        student_calendar_records = []
        current_absence_streak = 0
        longest_absence_streak = 0
        recorded_days = 0

        for day_meta in calendar_days:
            current_date = date.fromisoformat(day_meta["date"])
            record = record_map.get((student.id, current_date))
            status = record.status if record else None
            is_active_school_day = day_meta["is_school_day"] and _student_active_on(student, current_date)

            if is_active_school_day and status in counts:
                counts[status] += 1
                classroom_status_totals[status] += 1
                recorded_days += 1

            if is_active_school_day:
                if status == ABSENCE_STATUS:
                    current_absence_streak += 1
                    longest_absence_streak = max(longest_absence_streak, current_absence_streak)
                else:
                    current_absence_streak = 0
            else:
                current_absence_streak = 0

            student_calendar_records.append({
                "date": day_meta["date"],
                "status": status,
                "remark": record.remark if record else None,
                "is_school_day": is_active_school_day,
            })

        school_day_count = len(applicable_school_days)
        attended_days = counts["出席"] + counts["遲到"]
        unmarked_days = max(school_day_count - recorded_days, 0)
        attendance_rate = round((attended_days / school_day_count) * 100, 1) if school_day_count else 0.0
        completion_rate = round((recorded_days / school_day_count) * 100, 1) if school_day_count else 0.0

        student_row = {
            "student_id": student.id,
            "student_no": student.student_id,
            "name": student.name,
            "school_days": school_day_count,
            "recorded_days": recorded_days,
            "attendance_rate": attendance_rate,
            "record_completion_rate": completion_rate,
            "current_absence_streak": current_absence_streak,
            "longest_absence_streak": longest_absence_streak,
            "absence_alert": longest_absence_streak >= ALERT_STREAK_THRESHOLD,
            "出席": counts["出席"],
            "缺席": counts["缺席"],
            "病假": counts["病假"],
            "事假": counts["事假"],
            "遲到": counts["遲到"],
            "未點名": unmarked_days,
            "daily_records": student_calendar_records,
        }
        if student_row["absence_alert"]:
            flagged_students.append({
                "student_id": student.id,
                "student_no": student.student_id,
                "name": student.name,
                "longest_absence_streak": longest_absence_streak,
                "current_absence_streak": current_absence_streak,
            })

        total_student_school_days += school_day_count
        total_attended_days += attended_days
        total_recorded_days += recorded_days
        student_rows.append(student_row)

    classroom_attendance_rate = (
        round((total_attended_days / total_student_school_days) * 100, 1)
        if total_student_school_days
        else 0.0
    )
    classroom_completion_rate = (
        round((total_recorded_days / total_student_school_days) * 100, 1)
        if total_student_school_days
        else 0.0
    )

    return {
        "year": year,
        "month": month,
        "classroom_id": classroom.id,
        "classroom_name": classroom.name,
        "days_in_month": len(calendar_days),
        "holiday_count": len(holiday_map),
        "school_days_count": len(school_days),
        "classroom_attendance_rate": classroom_attendance_rate,
        "classroom_record_completion_rate": classroom_completion_rate,
        "status_totals": classroom_status_totals,
        "students": student_rows,
        "alerts": flagged_students,
        "calendar_days": calendar_days,
    }


def build_monthly_attendance_report(
    session,
    classroom_id: int,
    year: int,
    month: int,
    *,
    force_refresh: bool = False,
) -> dict:
    return report_cache_service.get_or_build(
        session,
        category="student_attendance_monthly",
        ttl_seconds=MONTHLY_ATTENDANCE_REPORT_CACHE_TTL_SECONDS,
        params={
            "classroom_id": classroom_id,
            "year": year,
            "month": month,
        },
        force_refresh=force_refresh,
        builder=lambda: _compute_monthly_attendance_report(session, classroom_id, year, month),
    )
