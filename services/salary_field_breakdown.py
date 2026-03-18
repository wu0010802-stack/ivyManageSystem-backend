"""薪資欄位明細與 debug snapshot 共用 service。"""

from __future__ import annotations

from datetime import date, timedelta
import calendar as cal_module

from models.database import (
    Attendance,
    AllowanceType,
    Classroom,
    DailyShift,
    Employee,
    EmployeeAllowance,
    LeaveRecord,
    MeetingRecord,
    OvertimeRecord,
    Student,
)
from services.salary.constants import LEAVE_DEDUCTION_RULES, MONTHLY_BASE_DAYS
from services.salary.proration import _build_expected_workdays, _prorate_for_period
from services.salary.utils import get_bonus_distribution_month, get_meeting_deduction_period_start
from services.student_enrollment import count_students_active_on


FIELD_LABELS = {
    "festival_bonus": "節慶獎金",
    "overtime_bonus": "超額獎金",
    "overtime_pay": "加班津貼",
    "supervisor_dividend": "主管紅利",
    "meeting_overtime_pay": "會議加班",
    "birthday_bonus": "生日禮金",
    "leave_deduction": "請假扣款",
    "late_deduction": "遲到扣款",
    "early_leave_deduction": "早退扣款",
    "meeting_absence_deduction": "節慶獎金扣減",
    "absence_deduction": "曠職扣款",
}


def _to_iso(value):
    return value.isoformat() if value else None


def build_salary_debug_snapshot(session, engine, emp: Employee, year: int, month: int) -> dict:
    """建立單一員工當月薪資 debug snapshot。"""
    title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or "")
    grade_to_title = {"A": "幼兒園教師", "B": "教保員", "C": "助理教保員"}
    bonus_grade_override = getattr(emp, "bonus_grade", None)
    effective_title = (
        grade_to_title.get(bonus_grade_override, title_name) if bonus_grade_override else title_name
    )

    _, last_day = cal_module.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)
    is_bonus_month = get_bonus_distribution_month(month)

    attendances = session.query(Attendance).filter(
        Attendance.employee_id == emp.id,
        Attendance.attendance_date >= start_date,
        Attendance.attendance_date <= end_date,
    ).all()
    late_count = sum(1 for a in attendances if a.is_late)
    early_count = sum(1 for a in attendances if a.is_early_leave)
    missing_in = sum(1 for a in attendances if a.is_missing_punch_in)
    missing_out = sum(1 for a in attendances if a.is_missing_punch_out)
    total_late_min = sum(a.late_minutes or 0 for a in attendances if a.is_late)
    total_early_min = sum(a.early_leave_minutes or 0 for a in attendances if a.is_early_leave)
    late_details = [a.late_minutes or 0 for a in attendances if a.is_late and (a.late_minutes or 0) > 0]
    early_rows = [
        {
            "date": _to_iso(a.attendance_date),
            "minutes": a.early_leave_minutes or 0,
        }
        for a in attendances
        if a.is_early_leave and (a.early_leave_minutes or 0) > 0
    ]
    late_rows = [
        {
            "date": _to_iso(a.attendance_date),
            "minutes": a.late_minutes or 0,
        }
        for a in attendances
        if a.is_late and (a.late_minutes or 0) > 0
    ]

    approved_leaves = session.query(LeaveRecord).filter(
        LeaveRecord.employee_id == emp.id,
        LeaveRecord.is_approved == True,
        LeaveRecord.start_date <= end_date,
        LeaveRecord.end_date >= start_date,
    ).all()
    daily_salary = (emp.base_salary or 0) / MONTHLY_BASE_DAYS if emp.base_salary else 0
    leave_deduction_total = 0
    leave_breakdown = []
    for lv in approved_leaves:
        ratio = (
            lv.deduction_ratio
            if lv.deduction_ratio is not None
            else LEAVE_DEDUCTION_RULES.get(lv.leave_type, 1.0)
        )
        deduction = round((lv.leave_hours / 8) * daily_salary * ratio)
        leave_deduction_total += deduction
        leave_breakdown.append(
            {
                "type": lv.leave_type,
                "start": _to_iso(lv.start_date),
                "end": _to_iso(lv.end_date),
                "hours": lv.leave_hours or 0,
                "ratio": ratio,
                "deduction": deduction,
            }
        )

    personal_sick_leave_hours = sum(
        lv.leave_hours or 0 for lv in approved_leaves if lv.leave_type in ("personal", "sick")
    )
    bonus_forfeited_by_leave = personal_sick_leave_hours > 40

    approved_ot = session.query(OvertimeRecord).filter(
        OvertimeRecord.employee_id == emp.id,
        OvertimeRecord.is_approved == True,
        OvertimeRecord.overtime_date >= start_date,
        OvertimeRecord.overtime_date <= end_date,
    ).all()
    overtime_rows = [
        {
            "date": _to_iso(o.overtime_date),
            "hours": o.hours or 0,
            "overtime_type": o.overtime_type or "",
            "pay": round(o.overtime_pay or 0),
            "remark": o.reason or "",
        }
        for o in approved_ot
    ]
    ot_pay = sum(o.overtime_pay or 0 for o in approved_ot)

    meetings = session.query(MeetingRecord).filter(
        MeetingRecord.employee_id == emp.id,
        MeetingRecord.meeting_date >= start_date,
        MeetingRecord.meeting_date <= end_date,
    ).all()
    meeting_attended = sum(1 for m in meetings if m.attended)
    meeting_absent_current = sum(1 for m in meetings if not m.attended)
    absent_period = meeting_absent_current
    if is_bonus_month:
        period_start = get_meeting_deduction_period_start(year, month)
        if period_start is not None and period_start < start_date:
            prior_records = session.query(MeetingRecord).filter(
                MeetingRecord.employee_id == emp.id,
                MeetingRecord.meeting_date >= period_start,
                MeetingRecord.meeting_date < start_date,
            ).all()
            absent_period += sum(1 for m in prior_records if not m.attended)
    meeting_penalty = getattr(engine, "_meeting_absence_penalty", 100)
    meeting_absence_deduction = absent_period * meeting_penalty if is_bonus_month else 0
    per_meeting_pay = (
        getattr(engine, "_meeting_pay_6pm", 100)
        if (emp.work_end_time or "17:00") == "18:00"
        else getattr(engine, "_meeting_pay", 200)
    )
    meeting_rows = [
        {
            "date": _to_iso(m.meeting_date),
            "attended": "出席" if m.attended else "缺席",
            "pay": round(m.overtime_pay or (per_meeting_pay if m.attended else 0)),
            "remark": m.remark or "",
        }
        for m in meetings
    ]

    emp_allowances = session.query(EmployeeAllowance).filter(
        EmployeeAllowance.employee_id == emp.id,
        EmployeeAllowance.is_active == True,
    ).all()
    allowances = []
    for allowance in emp_allowances:
        allowance_type = session.query(AllowanceType).get(allowance.allowance_type_id)
        if allowance_type:
            allowances.append({"name": allowance_type.name, "amount": allowance.amount or 0})

    classroom_context = None
    classroom = session.query(Classroom).get(emp.classroom_id) if emp.classroom_id else None
    if classroom:
        role = "assistant_teacher"
        if classroom.head_teacher_id == emp.id:
            role = "head_teacher"
        elif classroom.art_teacher_id == emp.id:
            role = "art_teacher"
        has_assistant = bool(classroom.assistant_teacher_id)
        student_count = count_students_active_on(session, end_date, classroom.id)
        grade_name = classroom.grade.name if classroom.grade else ""
        classroom_context = {
            "role": role,
            "grade_name": grade_name,
            "current_enrollment": student_count,
            "has_assistant": has_assistant,
            "is_shared_assistant": False,
        }
        if role == "assistant_teacher":
            shared_classes = session.query(Classroom).filter(
                Classroom.assistant_teacher_id == emp.id
            ).all()
            if len(shared_classes) >= 2:
                classroom_context["is_shared_assistant"] = True
                second_class = next((c for c in shared_classes if c.id != classroom.id), None)
                if second_class:
                    second_count = count_students_active_on(session, end_date, second_class.id)
                    classroom_context["shared_second_class"] = {
                        "grade_name": second_class.grade.name if second_class.grade else "",
                        "current_enrollment": second_count,
                    }

    supervisor_role = emp.supervisor_role or ""
    office_staff_context = None
    supervisor_base = engine.get_supervisor_festival_bonus(title_name, emp.position or "", supervisor_role)
    office_bonus_base = engine.get_office_festival_bonus_base(emp.position or "", title_name)
    if supervisor_base is not None or (office_bonus_base is not None and not classroom_context):
        total_students = count_students_active_on(session, end_date)
        office_staff_context = {"school_enrollment": total_students}

    base_salary = emp.base_salary or 0
    resign_date = getattr(emp, "resign_date", None)
    prorated_base = _prorate_for_period(base_salary, emp.hire_date, resign_date, year, month)
    birthday_bonus = 500 if (emp.birthday and emp.birthday.month == month) else 0
    per_minute_rate = base_salary / (MONTHLY_BASE_DAYS * 8 * 60) if base_salary > 0 else 0

    att_deduction_detail = []
    normal_late_deduction = 0
    for minutes in late_details:
        deduction = round(minutes * per_minute_rate)
        att_deduction_detail.append({"minutes": minutes, "type": "per_minute", "deduction": deduction})
        normal_late_deduction += deduction
    early_deduction = round(total_early_min * per_minute_rate)

    festival_detail = {}
    is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
    if supervisor_base is not None:
        school_enrollment = office_staff_context["school_enrollment"] if office_staff_context else 0
        school_target = getattr(engine, "_school_wide_target", 160) or 160
        ratio = school_enrollment / school_target if school_target > 0 else 0
        raw_result = round(supervisor_base * ratio) if is_eligible else 0
        festival_detail = {
            "category": "主管",
            "base": supervisor_base,
            "enrollment": school_enrollment,
            "target": school_target,
            "ratio": round(ratio, 4),
            "eligible": is_eligible,
            "is_bonus_month": is_bonus_month,
            "result": raw_result,
            "result_after_penalty": max(0, raw_result - meeting_absence_deduction) if is_bonus_month else 0,
        }
    elif office_staff_context and emp.position and office_bonus_base:
        school_enrollment = office_staff_context["school_enrollment"]
        school_target = getattr(engine, "_school_wide_target", 160) or 160
        ratio = school_enrollment / school_target if school_target > 0 else 0
        raw_result = round(office_bonus_base * ratio) if is_eligible else 0
        festival_detail = {
            "category": "辦公室",
            "base": office_bonus_base,
            "enrollment": school_enrollment,
            "target": school_target,
            "ratio": round(ratio, 4),
            "eligible": is_eligible,
            "is_bonus_month": is_bonus_month,
            "result": raw_result,
            "result_after_penalty": max(0, raw_result - meeting_absence_deduction) if is_bonus_month else 0,
        }
    elif classroom_context:
        cc = classroom_context
        base_amount = engine.get_festival_bonus_base(effective_title, cc["role"])
        target = engine.get_target_enrollment(cc["grade_name"], cc["has_assistant"], cc["is_shared_assistant"])
        ratio = cc["current_enrollment"] / target if target > 0 else 0
        overtime_target = engine.get_overtime_target(
            cc["grade_name"], cc["has_assistant"], cc["is_shared_assistant"]
        )
        overtime_count = max(0, cc["current_enrollment"] - overtime_target)
        overtime_per_person = engine.get_overtime_per_person(
            cc["role"] if cc["role"] != "art_teacher" else "assistant_teacher",
            cc["grade_name"],
        )
        raw_festival = round(base_amount * ratio) if is_eligible else 0
        raw_overtime = round(overtime_count * overtime_per_person) if is_eligible else 0
        shared_second = cc.get("shared_second_class")
        if shared_second and is_eligible:
            target2 = engine.get_target_enrollment(shared_second["grade_name"], True, True)
            ratio2 = shared_second["current_enrollment"] / target2 if target2 > 0 else 0
            raw_festival2 = round(base_amount * ratio2)
            overtime_target2 = engine.get_overtime_target(shared_second["grade_name"], True, True)
            overtime_count2 = max(0, shared_second["current_enrollment"] - overtime_target2)
            raw_overtime2 = round(overtime_count2 * overtime_per_person)
            raw_festival = round((raw_festival + raw_festival2) / 2)
            raw_overtime = round((raw_overtime + raw_overtime2) / 2)
            cc["shared_second_target"] = target2
            cc["shared_second_ratio"] = round(ratio2, 4)
        festival_detail = {
            "category": "帶班老師",
            "role": cc["role"],
            "grade": cc["grade_name"],
            "base": base_amount,
            "enrollment": cc["current_enrollment"],
            "target": target,
            "ratio": round(ratio, 4),
            "eligible": is_eligible,
            "is_bonus_month": is_bonus_month,
            "festival_result": raw_festival,
            "festival_result_after_penalty": max(0, raw_festival - meeting_absence_deduction) if is_bonus_month else 0,
            "overtime_target": overtime_target,
            "overtime_count": overtime_count,
            "overtime_per_person": overtime_per_person,
            "overtime_result": raw_overtime if is_bonus_month else 0,
        }
        if shared_second:
            festival_detail["shared_second_class"] = {
                "grade": shared_second["grade_name"],
                "enrollment": shared_second["current_enrollment"],
                "target": cc.get("shared_second_target"),
                "ratio": cc.get("shared_second_ratio"),
            }

    if bonus_forfeited_by_leave and festival_detail:
        festival_detail["forfeited_by_leave"] = True
        for key in ("result_after_penalty", "festival_result_after_penalty", "overtime_result"):
            if key in festival_detail:
                festival_detail[key] = 0

    supervisor_dividend = engine.get_supervisor_dividend(title_name, emp.position or "", supervisor_role)
    if bonus_forfeited_by_leave:
        supervisor_dividend = 0

    holidays_in_month = session.query(DailyShift.date).filter(DailyShift.employee_id == -1).all()
    del holidays_in_month
    from models.database import Holiday

    holiday_set = {
        h.date
        for h in session.query(Holiday.date).filter(
            Holiday.date >= start_date, Holiday.date <= end_date, Holiday.is_active == True
        )
    }
    daily_shift_map = {
        ds.date: ds.shift_type_id
        for ds in session.query(DailyShift).filter(
            DailyShift.employee_id == emp.id,
            DailyShift.date >= start_date,
            DailyShift.date <= end_date,
        )
    }
    expected_workdays = _build_expected_workdays(
        year=year,
        month=month,
        holiday_set=holiday_set,
        daily_shift_map=daily_shift_map,
        hire_date_raw=emp.hire_date,
        resign_date_raw=resign_date,
    )
    attendance_dates = {a.attendance_date for a in attendances}
    leave_covered = set()
    for lv in approved_leaves:
        current = lv.start_date
        while current <= lv.end_date:
            if start_date <= current <= end_date:
                leave_covered.add(current)
            current += timedelta(days=1)
    absent_days = sorted(expected_workdays - attendance_dates - leave_covered)
    absent_count = len(absent_days)
    absence_deduction_amount = round(absent_count * daily_salary)

    ins_service = engine.insurance_service
    insured_salary_raw = (
        emp.insurance_salary_level if getattr(emp, "insurance_salary_level", None) else emp.base_salary
    ) or 0
    insured_salary = ins_service.get_bracket(insured_salary_raw)["amount"] if insured_salary_raw else 0
    ins = ins_service.calculate(
        insured_salary,
        emp.dependents or 0,
        pension_self_rate=emp.pension_self_rate or 0,
    )

    fixed_allowances_total = sum(
        [
            emp.supervisor_allowance or 0,
            emp.teacher_allowance or 0,
            emp.meal_allowance or 0,
            emp.transportation_allowance or 0,
            emp.other_allowance or 0,
        ]
    )
    extra_allowances_total = sum(item["amount"] for item in allowances)
    gross_salary = round(
        prorated_base
        + fixed_allowances_total
        + extra_allowances_total
        + supervisor_dividend
        + birthday_bonus
        + meeting_attended * per_meeting_pay
        + ot_pay
    )
    total_deduction = round(
        ins.labor_employee
        + ins.health_employee
        + ins.pension_employee
        + normal_late_deduction
        + early_deduction
        + leave_deduction_total
        + absence_deduction_amount
    )

    return {
        "employee": {
            "id": emp.id,
            "employee_id": emp.employee_id,
            "name": emp.name,
            "title": title_name,
            "position": emp.position,
            "supervisor_role": supervisor_role,
            "employee_type": emp.employee_type,
            "base_salary": base_salary,
            "hire_date": _to_iso(emp.hire_date),
            "birthday": _to_iso(emp.birthday),
            "classroom_id": emp.classroom_id,
            "insurance_salary_level": getattr(emp, "insurance_salary_level", None),
            "dependents": emp.dependents,
            "work_start_time": emp.work_start_time,
            "work_end_time": emp.work_end_time,
        },
        "period": {"year": year, "month": month, "is_bonus_month": is_bonus_month},
        "attendance_summary": {
            "total_records": len(attendances),
            "late_count": late_count,
            "early_leave_count": early_count,
            "missing_punch_in": missing_in,
            "missing_punch_out": missing_out,
            "total_late_minutes": total_late_min,
            "total_early_minutes": total_early_min,
            "late_details": late_details,
            "late_rows": late_rows,
            "early_rows": early_rows,
        },
        "deduction_calc": {
            "daily_salary": round(daily_salary),
            "per_minute_rate": round(per_minute_rate, 4),
            "late_deduction_detail": att_deduction_detail,
            "late_deduction": normal_late_deduction,
            "early_leave_deduction": early_deduction,
            "missing_punch_deduction": 0,
        },
        "leave_breakdown": leave_breakdown,
        "leave_deduction_total": leave_deduction_total,
        "overtime_pay": round(ot_pay),
        "overtime_rows": overtime_rows,
        "meeting": {
            "attended": meeting_attended,
            "absent_this_month": meeting_absent_current,
            "absent_period": absent_period,
            "meeting_absence_deduction": meeting_absence_deduction,
            "overtime_pay_per_session": per_meeting_pay,
            "absence_penalty_per_session": meeting_penalty,
            "rows": meeting_rows,
        },
        "allowances": allowances,
        "classroom_context": classroom_context,
        "festival_bonus_detail": festival_detail,
        "supervisor_dividend": round(supervisor_dividend),
        "insurance": {
            "insured_amount_raw": insured_salary_raw,
            "insured_amount": ins.insured_amount,
            "labor_employee": ins.labor_employee,
            "health_employee": ins.health_employee,
            "pension_employee": ins.pension_employee,
            "total_employee_deduction": ins.total_employee,
        },
        "salary_summary": {
            "prorated_base_salary": round(prorated_base),
            "proration_applied": round(prorated_base) != base_salary,
            "birthday_bonus": birthday_bonus,
            "meeting_overtime_pay": meeting_attended * per_meeting_pay,
            "fixed_allowances": round(fixed_allowances_total),
            "extra_allowances": round(extra_allowances_total),
            "absent_count": absent_count,
            "absent_days": [_to_iso(day) for day in absent_days],
            "absence_deduction": absence_deduction_amount,
            "personal_sick_leave_hours": personal_sick_leave_hours,
            "bonus_forfeited_by_leave": bonus_forfeited_by_leave,
            "gross_salary": gross_salary,
            "total_deduction": total_deduction,
            "net_salary": gross_salary - total_deduction,
        },
    }


def build_field_breakdown(record, emp: Employee, snapshot: dict, field: str) -> dict:
    """將 debug snapshot 轉成欄位明細 contract。"""
    if field not in FIELD_LABELS:
        raise ValueError("unsupported field")

    title = f"{FIELD_LABELS[field]}明細"
    employee = {
        "record_id": record.id,
        "employee_name": emp.name,
        "employee_code": emp.employee_id,
        "job_title": emp.job_title_rel.name if emp.job_title_rel else (emp.title or ""),
        "year": record.salary_year,
        "month": record.salary_month,
    }
    amount = round(getattr(record, field) or 0)
    data = {
        "title": title,
        "field": field,
        "employee": employee,
        "columns": [],
        "rows": [],
        "summary": {"amount": amount},
        "note": "",
    }

    if field == "festival_bonus":
        detail = snapshot["festival_bonus_detail"] or {}
        data["columns"] = [
            {"key": "name", "label": "姓名"},
            {"key": "category", "label": "類別"},
            {"key": "bonusBase", "label": "獎金基數"},
            {"key": "targetEnrollment", "label": "目標人數"},
            {"key": "currentEnrollment", "label": "在籍人數"},
            {"key": "ratio", "label": "達成率"},
            {"key": "result", "label": "獎金"},
            {"key": "remark", "label": "備註"},
        ]
        remark = ""
        if detail.get("forfeited_by_leave"):
            remark = "事假/病假超過 40 小時，全數取消"
        elif detail.get("shared_second_class"):
            remark = "兩班平均"
        elif not detail:
            remark = "無帶班/無設定"
        row = {
            "name": emp.name,
            "category": detail.get("category") or "其他",
            "bonusBase": detail.get("base", 0),
            "targetEnrollment": detail.get("target") or "-",
            "currentEnrollment": detail.get("enrollment") or "-",
            "ratio": f'{((detail.get("ratio") or 0) * 100):.1f}%',
            "result": amount,
            "remark": remark,
        }
        data["rows"] = [row]
        data["note"] = "節慶獎金扣減不包含在本明細，該欄位另有獨立明細。"
    elif field == "overtime_bonus":
        detail = snapshot["festival_bonus_detail"] or {}
        data["columns"] = [
            {"key": "grade", "label": "年級"},
            {"key": "target", "label": "超額目標"},
            {"key": "current", "label": "在籍人數"},
            {"key": "overflow", "label": "超額人數"},
            {"key": "perPerson", "label": "每人金額"},
            {"key": "result", "label": "結果"},
            {"key": "remark", "label": "備註"},
        ]
        data["rows"] = [
            {
                "grade": detail.get("grade") or "-",
                "target": detail.get("overtime_target") or "-",
                "current": detail.get("enrollment") or "-",
                "overflow": detail.get("overtime_count") or 0,
                "perPerson": detail.get("overtime_per_person") or 0,
                "result": amount,
                "remark": "與節慶獎金同月發放" if amount else "未達超額門檻或非發放月",
            }
        ]
        data["note"] = "超額獎金與節慶獎金同月獨立轉帳。"
    elif field == "overtime_pay":
        data["columns"] = [
            {"key": "date", "label": "日期"},
            {"key": "hours", "label": "時數"},
            {"key": "overtime_type", "label": "類型"},
            {"key": "pay", "label": "金額"},
            {"key": "remark", "label": "備註"},
        ]
        data["rows"] = snapshot["overtime_rows"] or [
            {"date": "-", "hours": 0, "overtime_type": "-", "pay": 0, "remark": "當月無核准加班紀錄"}
        ]
    elif field == "supervisor_dividend":
        forfeited = snapshot["salary_summary"]["bonus_forfeited_by_leave"]
        data["columns"] = [
            {"key": "role", "label": "主管角色"},
            {"key": "leaveHours", "label": "事病假時數"},
            {"key": "forfeited", "label": "是否歸零"},
            {"key": "result", "label": "最終金額"},
            {"key": "remark", "label": "備註"},
        ]
        data["rows"] = [
            {
                "role": emp.supervisor_role or emp.position or emp.title or "-",
                "leaveHours": snapshot["salary_summary"]["personal_sick_leave_hours"],
                "forfeited": "是" if forfeited else "否",
                "result": amount,
                "remark": "事假/病假 > 40 小時取消" if forfeited else "依主管紅利設定發放",
            }
        ]
    elif field == "meeting_overtime_pay":
        data["columns"] = [
            {"key": "date", "label": "會議日期"},
            {"key": "attended", "label": "出席"},
            {"key": "pay", "label": "單次金額"},
            {"key": "remark", "label": "備註"},
        ]
        data["rows"] = snapshot["meeting"]["rows"] or [
            {"date": "-", "attended": "-", "pay": 0, "remark": "當月無園務會議紀錄"}
        ]
    elif field == "birthday_bonus":
        birthday = snapshot["employee"]["birthday"]
        matched = bool(emp.birthday and emp.birthday.month == record.salary_month)
        data["columns"] = [
            {"key": "birthday", "label": "生日"},
            {"key": "salaryMonth", "label": "薪資月份"},
            {"key": "matched", "label": "是否命中"},
            {"key": "ruleAmount", "label": "規則金額"},
            {"key": "result", "label": "結果"},
        ]
        data["rows"] = [
            {
                "birthday": birthday or "未設定",
                "salaryMonth": f'{record.salary_year}-{record.salary_month:02d}',
                "matched": "是" if matched else "否",
                "ruleAmount": 500,
                "result": amount,
            }
        ]
        data["note"] = "只看生日月份，不看日期。"
    elif field == "leave_deduction":
        data["columns"] = [
            {"key": "type", "label": "假別"},
            {"key": "start", "label": "開始"},
            {"key": "end", "label": "結束"},
            {"key": "hours", "label": "時數"},
            {"key": "ratio", "label": "扣薪比例"},
            {"key": "deduction", "label": "扣款"},
        ]
        rows = snapshot["leave_breakdown"] or []
        data["rows"] = rows or [{"type": "-", "start": "-", "end": "-", "hours": 0, "ratio": 0, "deduction": 0}]
        data["note"] = "請假扣款依請假時數、日薪與假別扣薪比例計算。"
    elif field == "late_deduction":
        data["columns"] = [
            {"key": "date", "label": "日期"},
            {"key": "minutes", "label": "遲到分鐘"},
            {"key": "deduction", "label": "扣款"},
        ]
        rows = [
            {
                "date": row["date"],
                "minutes": row["minutes"],
                "deduction": round(row["minutes"] * snapshot["deduction_calc"]["per_minute_rate"]),
            }
            for row in snapshot["attendance_summary"]["late_rows"]
        ]
        data["rows"] = rows or [{"date": "-", "minutes": 0, "deduction": 0}]
        data["note"] = f'每分鐘費率：{snapshot["deduction_calc"]["per_minute_rate"]}'
    elif field == "early_leave_deduction":
        data["columns"] = [
            {"key": "date", "label": "日期"},
            {"key": "minutes", "label": "早退分鐘"},
            {"key": "deduction", "label": "扣款"},
        ]
        rows = [
            {
                "date": row["date"],
                "minutes": row["minutes"],
                "deduction": round(row["minutes"] * snapshot["deduction_calc"]["per_minute_rate"]),
            }
            for row in snapshot["attendance_summary"]["early_rows"]
        ]
        data["rows"] = rows or [{"date": "-", "minutes": 0, "deduction": 0}]
        data["note"] = f'每分鐘費率：{snapshot["deduction_calc"]["per_minute_rate"]}'
    elif field == "meeting_absence_deduction":
        data["columns"] = [
            {"key": "absentThisMonth", "label": "本月缺席"},
            {"key": "absentPeriod", "label": "發放期累積缺席"},
            {"key": "penalty", "label": "每次扣減"},
            {"key": "isBonusMonth", "label": "是否發放月"},
            {"key": "result", "label": "最終扣減"},
        ]
        data["rows"] = [
            {
                "absentThisMonth": snapshot["meeting"]["absent_this_month"],
                "absentPeriod": snapshot["meeting"]["absent_period"],
                "penalty": snapshot["meeting"]["absence_penalty_per_session"],
                "isBonusMonth": "是" if snapshot["period"]["is_bonus_month"] else "否",
                "result": amount,
            }
        ]
        data["note"] = "只從節慶獎金扣減，不進 total_deduction。"
    elif field == "absence_deduction":
        data["columns"] = [
            {"key": "date", "label": "曠職日期"},
            {"key": "dailySalary", "label": "日薪基數"},
            {"key": "remark", "label": "備註"},
        ]
        rows = [
            {
                "date": day,
                "dailySalary": snapshot["deduction_calc"]["daily_salary"],
                "remark": "預期上班日無打卡且無核准請假",
            }
            for day in snapshot["salary_summary"]["absent_days"]
        ]
        data["rows"] = rows or [
            {
                "date": "-",
                "dailySalary": snapshot["deduction_calc"]["daily_salary"],
                "remark": "當月無曠職",
            }
        ]

    return data
