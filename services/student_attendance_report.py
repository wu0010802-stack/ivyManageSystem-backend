"""
學生出席報表計算 service。
"""

from __future__ import annotations

import calendar as cal_module
from datetime import date, timedelta

from models.database import Classroom, Holiday, Student, StudentAttendance

VALID_STATUSES = ("出席", "缺席", "病假", "事假", "遲到")
ATTENDED_STATUSES = {"出席", "遲到"}
ABSENCE_STATUS = "缺席"
ALERT_STREAK_THRESHOLD = 3
WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"]


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


def build_monthly_attendance_report(session, classroom_id: int, year: int, month: int) -> dict:
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
