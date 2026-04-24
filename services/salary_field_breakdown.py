"""薪資欄位明細與 debug snapshot 共用 service。"""

from __future__ import annotations

from datetime import date, timedelta
import calendar as cal_module

from sqlalchemy.orm import joinedload

from models.database import (
    Attendance,
    Classroom,
    DailyShift,
    Employee,
    LeaveRecord,
    MeetingRecord,
    OvertimeRecord,
    Student,
)
from services.salary.constants import (
    LEAVE_DEDUCTION_RULES,
    MONTHLY_BASE_DAYS,
    SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS,
)
from services.salary.proration import _build_expected_workdays, _prorate_for_period
from services.salary.utils import (
    get_bonus_distribution_month,
    get_meeting_deduction_period_start,
)
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


def _calc_attendance_stats(attendances: list) -> dict:
    """考勤統計：遲到/早退/未打卡次數、分鐘數、明細列表。"""
    late_count = sum(1 for a in attendances if a.is_late)
    early_count = sum(1 for a in attendances if a.is_early_leave)
    missing_in = sum(1 for a in attendances if a.is_missing_punch_in)
    missing_out = sum(1 for a in attendances if a.is_missing_punch_out)
    total_late_min = sum(a.late_minutes or 0 for a in attendances if a.is_late)
    total_early_min = sum(
        a.early_leave_minutes or 0 for a in attendances if a.is_early_leave
    )
    late_details = [
        a.late_minutes or 0
        for a in attendances
        if a.is_late and (a.late_minutes or 0) > 0
    ]
    early_rows = [
        {"date": _to_iso(a.attendance_date), "minutes": a.early_leave_minutes or 0}
        for a in attendances
        if a.is_early_leave and (a.early_leave_minutes or 0) > 0
    ]
    late_rows = [
        {"date": _to_iso(a.attendance_date), "minutes": a.late_minutes or 0}
        for a in attendances
        if a.is_late and (a.late_minutes or 0) > 0
    ]
    return {
        "late_count": late_count,
        "early_count": early_count,
        "missing_in": missing_in,
        "missing_out": missing_out,
        "total_late_min": total_late_min,
        "total_early_min": total_early_min,
        "late_details": late_details,
        "early_rows": early_rows,
        "late_rows": late_rows,
    }


def _calc_leave_deductions(
    approved_leaves: list,
    daily_salary: float,
    ytd_sick_hours_before_month: float = 0.0,
) -> dict:
    """請假扣款計算：逐筆計算扣款金額、統計事病假時數。

    病假套用勞基法第 43 條 30 日（240h）年度半薪上限；超過部分顯示為 ratio=1.0。
    """
    leave_deduction_total = 0
    leave_breakdown = []
    sick_used = float(ytd_sick_hours_before_month or 0.0)

    sick_leaves = sorted(
        [lv for lv in approved_leaves if lv.leave_type == "sick"],
        key=lambda lv: getattr(lv, "start_date", None) or date.min,
    )
    other_leaves = [lv for lv in approved_leaves if lv.leave_type != "sick"]
    standard_sick_ratio = LEAVE_DEDUCTION_RULES.get("sick", 0.5)

    for lv in sick_leaves:
        hours = lv.leave_hours or 0
        # 僅「明確偏離標準」才視為 HR 覆寫；核准流程會把 ratio 寫成標準值 0.5，
        # 這種情況仍要套用 240h 年度上限。
        is_genuine_override = (
            lv.deduction_ratio is not None and lv.deduction_ratio != standard_sick_ratio
        )
        if is_genuine_override:
            effective_ratio = lv.deduction_ratio
            deduction = round((hours / 8) * daily_salary * effective_ratio)
            display_ratio = effective_ratio
        else:
            half_paid = max(
                0.0, min(SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS - sick_used, hours)
            )
            unpaid = hours - half_paid
            deduction = round(
                (half_paid / 8) * daily_salary * 0.5 + (unpaid / 8) * daily_salary * 1.0
            )
            # 顯示用綜合 ratio：若全在上限內則 0.5、全超過則 1.0、混合時取加權平均
            if hours > 0:
                display_ratio = (half_paid * 0.5 + unpaid * 1.0) / hours
            else:
                display_ratio = 0.5
        sick_used += hours
        leave_deduction_total += deduction
        leave_breakdown.append(
            {
                "type": lv.leave_type,
                "start": _to_iso(lv.start_date),
                "end": _to_iso(lv.end_date),
                "hours": hours,
                "ratio": display_ratio,
                "deduction": deduction,
            }
        )

    for lv in other_leaves:
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
        lv.leave_hours or 0
        for lv in approved_leaves
        if lv.leave_type in ("personal", "sick")
    )
    return {
        "leave_deduction_total": leave_deduction_total,
        "leave_breakdown": leave_breakdown,
        "personal_sick_leave_hours": personal_sick_leave_hours,
        "bonus_forfeited_by_leave": personal_sick_leave_hours > 40,
    }


def _calc_overtime_details(approved_ot: list) -> dict:
    """加班計算：列表化加班記錄、加總加班費。"""
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
    return {"overtime_rows": overtime_rows, "ot_pay": ot_pay}


def _calc_insurance_details(emp: Employee, ins_service) -> dict:
    """保險計算：查詢投保級距並計算員工自付保費。"""
    insured_salary_raw = (
        emp.insurance_salary_level
        if getattr(emp, "insurance_salary_level", None)
        else emp.base_salary
    ) or 0
    insured_salary = (
        ins_service.get_bracket(insured_salary_raw)["amount"]
        if insured_salary_raw
        else 0
    )
    ins = ins_service.calculate(
        insured_salary,
        emp.dependents or 0,
        pension_self_rate=emp.pension_self_rate or 0,
    )
    return {"insured_salary_raw": insured_salary_raw, "ins": ins}


def _build_meeting_stats(
    session, engine, emp: Employee, start_date, end_date, is_bonus_month: bool
) -> dict:
    """查詢並計算會議出席/缺席統計、扣款與每次薪資。"""
    meetings = (
        session.query(MeetingRecord)
        .filter(
            MeetingRecord.employee_id == emp.id,
            MeetingRecord.meeting_date >= start_date,
            MeetingRecord.meeting_date <= end_date,
        )
        .all()
    )
    meeting_attended = sum(1 for m in meetings if m.attended)
    meeting_absent_current = sum(1 for m in meetings if not m.attended)
    absent_period = meeting_absent_current
    if is_bonus_month:
        period_start = get_meeting_deduction_period_start(
            start_date.year, start_date.month
        )
        if period_start is not None and period_start < start_date:
            prior_records = (
                session.query(MeetingRecord)
                .filter(
                    MeetingRecord.employee_id == emp.id,
                    MeetingRecord.meeting_date >= period_start,
                    MeetingRecord.meeting_date < start_date,
                )
                .all()
            )
            absent_period += sum(1 for m in prior_records if not m.attended)
    meeting_penalty = getattr(engine, "_meeting_absence_penalty", 100)
    meeting_absence_deduction = absent_period * meeting_penalty if is_bonus_month else 0
    meeting_overtime_pay_total = sum(
        m.overtime_pay or 0 for m in meetings if m.attended
    )
    meeting_rows = [
        {
            "date": _to_iso(m.meeting_date),
            "attended": "出席" if m.attended else "缺席",
            "pay": round(m.overtime_pay or 0) if m.attended else 0,
            "remark": m.remark or "",
        }
        for m in meetings
    ]
    return {
        "attended": meeting_attended,
        "absent_current": meeting_absent_current,
        "absent_period": absent_period,
        "meeting_penalty": meeting_penalty,
        "meeting_absence_deduction": meeting_absence_deduction,
        "meeting_overtime_pay_total": meeting_overtime_pay_total,
        "rows": meeting_rows,
    }


def _build_classroom_context(session, emp: Employee, end_date) -> dict | None:
    """建立班級脈絡資訊（角色、人數、是否共同助理）。"""
    if not emp.classroom_id:
        return None
    classroom = (
        session.query(Classroom)
        .options(joinedload(Classroom.grade))
        .filter(Classroom.id == emp.classroom_id)
        .first()
    )
    if not classroom:
        return None
    role = "assistant_teacher"
    if classroom.head_teacher_id == emp.id:
        role = "head_teacher"
    elif classroom.art_teacher_id == emp.id:
        role = "art_teacher"
    ctx = {
        "role": role,
        "grade_name": classroom.grade.name if classroom.grade else "",
        "current_enrollment": count_students_active_on(session, end_date, classroom.id),
        "has_assistant": bool(classroom.assistant_teacher_id),
        "is_shared_assistant": False,
    }
    if role == "assistant_teacher":
        shared_classes = (
            session.query(Classroom)
            .options(joinedload(Classroom.grade))
            .filter(Classroom.assistant_teacher_id == emp.id)
            .all()
        )
        if len(shared_classes) >= 2:
            ctx["is_shared_assistant"] = True
            others = [
                {
                    "grade_name": c.grade.name if c.grade else "",
                    "current_enrollment": count_students_active_on(
                        session, end_date, c.id
                    ),
                }
                for c in shared_classes
                if c.id != classroom.id
            ]
            if others:
                ctx["shared_other_classes"] = others
                # 向下相容：保留 shared_second_class（首個其他班）
                ctx["shared_second_class"] = others[0]
    return ctx


def _calc_festival_detail(
    engine,
    emp: Employee,
    effective_title: str,
    title_name: str,
    classroom_context: dict | None,
    office_staff_context: dict | None,
    supervisor_base,
    office_bonus_base,
    meeting_absence_deduction: int,
    is_bonus_month: bool,
) -> dict:
    """計算節慶獎金詳情，涵蓋主管、辦公室、帶班老師三種路徑。"""
    is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
    detail: dict = {}

    if supervisor_base is not None:
        school_enrollment = (
            office_staff_context["school_enrollment"] if office_staff_context else 0
        )
        school_target = getattr(engine, "_school_wide_target", 160) or 160
        ratio = school_enrollment / school_target if school_target > 0 else 0
        raw_result = round(supervisor_base * ratio) if is_eligible else 0
        detail = {
            "category": "主管",
            "base": supervisor_base,
            "enrollment": school_enrollment,
            "target": school_target,
            "ratio": round(ratio, 4),
            "eligible": is_eligible,
            "is_bonus_month": is_bonus_month,
            "result": raw_result,
            "result_after_penalty": (
                max(0, raw_result - meeting_absence_deduction) if is_bonus_month else 0
            ),
        }
    elif office_staff_context and emp.position and office_bonus_base:
        school_enrollment = office_staff_context["school_enrollment"]
        school_target = getattr(engine, "_school_wide_target", 160) or 160
        ratio = school_enrollment / school_target if school_target > 0 else 0
        raw_result = round(office_bonus_base * ratio) if is_eligible else 0
        detail = {
            "category": "辦公室",
            "base": office_bonus_base,
            "enrollment": school_enrollment,
            "target": school_target,
            "ratio": round(ratio, 4),
            "eligible": is_eligible,
            "is_bonus_month": is_bonus_month,
            "result": raw_result,
            "result_after_penalty": (
                max(0, raw_result - meeting_absence_deduction) if is_bonus_month else 0
            ),
        }
    elif classroom_context:
        cc = classroom_context
        base_amount = engine.get_festival_bonus_base(effective_title, cc["role"])
        target = engine.get_target_enrollment(
            cc["grade_name"], cc["has_assistant"], cc["is_shared_assistant"]
        )
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
        # 共用副班導：取 shared_other_classes（含 ≥ 3 班）；fallback 到舊的 shared_second_class
        other_classes = cc.get("shared_other_classes")
        if not other_classes:
            shared_second = cc.get("shared_second_class")
            other_classes = [shared_second] if shared_second else []
        if other_classes and is_eligible:
            festival_scores = [raw_festival]
            overtime_scores = [raw_overtime]
            for oc in other_classes:
                target_oc = engine.get_target_enrollment(oc["grade_name"], True, True)
                ratio_oc = oc["current_enrollment"] / target_oc if target_oc > 0 else 0
                festival_scores.append(round(base_amount * ratio_oc))
                overtime_target_oc = engine.get_overtime_target(
                    oc["grade_name"], True, True
                )
                overtime_count_oc = max(
                    0, oc["current_enrollment"] - overtime_target_oc
                )
                overtime_scores.append(round(overtime_count_oc * overtime_per_person))
            raw_festival = round(sum(festival_scores) / len(festival_scores))
            raw_overtime = round(sum(overtime_scores) / len(overtime_scores))
            # 維持舊欄位（前端可能讀取）：以首個 other class 為代表
            shared_second_repr = other_classes[0]
            target2 = engine.get_target_enrollment(
                shared_second_repr["grade_name"], True, True
            )
            ratio2 = (
                shared_second_repr["current_enrollment"] / target2 if target2 > 0 else 0
            )
            cc["shared_second_target"] = target2
            cc["shared_second_ratio"] = round(ratio2, 4)
        detail = {
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
            "festival_result_after_penalty": (
                max(0, raw_festival - meeting_absence_deduction)
                if is_bonus_month
                else 0
            ),
            "overtime_target": overtime_target,
            "overtime_count": overtime_count,
            "overtime_per_person": overtime_per_person,
            "overtime_result": raw_overtime if is_bonus_month else 0,
        }
        if other_classes:
            shared_second_repr = other_classes[0]
            detail["shared_second_class"] = {
                "grade": shared_second_repr["grade_name"],
                "enrollment": shared_second_repr["current_enrollment"],
                "target": cc.get("shared_second_target"),
                "ratio": cc.get("shared_second_ratio"),
            }
            if len(other_classes) >= 2:
                detail["shared_other_classes"] = other_classes
    return detail


def _calc_absence_days(
    session,
    emp: Employee,
    year: int,
    month: int,
    start_date,
    end_date,
    attendances: list,
    approved_leaves: list,
    resign_date,
) -> dict:
    """計算曠職天數與扣款金額。"""
    from models.database import Holiday, WorkdayOverride

    # 遺留的無效查詢（相容舊邏輯，確保 Holiday 被延遲載入）
    holidays_in_month = (
        session.query(DailyShift.date).filter(DailyShift.employee_id == -1).all()
    )
    del holidays_in_month
    holiday_set = {
        h.date
        for h in session.query(Holiday.date).filter(
            Holiday.date >= start_date,
            Holiday.date <= end_date,
            Holiday.is_active == True,
        )
    }
    makeup_set = {
        m.date
        for m in session.query(WorkdayOverride.date).filter(
            WorkdayOverride.date >= start_date,
            WorkdayOverride.date <= end_date,
            WorkdayOverride.is_active.is_(True),
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
        makeup_set=makeup_set,
    )
    attendance_dates = {a.attendance_date for a in attendances}
    leave_covered: set = set()
    for lv in approved_leaves:
        current = lv.start_date
        while current <= lv.end_date:
            if start_date <= current <= end_date:
                leave_covered.add(current)
            current += timedelta(days=1)
    absent_days = sorted(expected_workdays - attendance_dates - leave_covered)
    daily_salary = (emp.base_salary or 0) / MONTHLY_BASE_DAYS if emp.base_salary else 0
    return {
        "absent_days": absent_days,
        "absent_count": len(absent_days),
        "absence_deduction_amount": round(len(absent_days) * daily_salary),
    }


def build_salary_debug_snapshot(
    session, engine, emp: Employee, year: int, month: int
) -> dict:
    """建立單一員工當月薪資 debug snapshot。"""
    title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or "")
    grade_to_title = {"A": "幼兒園教師", "B": "教保員", "C": "助理教保員"}
    bonus_grade_override = getattr(emp, "bonus_grade", None)
    effective_title = (
        grade_to_title.get(bonus_grade_override, title_name)
        if bonus_grade_override
        else title_name
    )

    _, last_day = cal_module.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)
    is_bonus_month = get_bonus_distribution_month(month)
    base_salary = emp.base_salary or 0
    resign_date = getattr(emp, "resign_date", None)

    # ── 考勤 ──
    attendances = (
        session.query(Attendance)
        .filter(
            Attendance.employee_id == emp.id,
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date,
        )
        .all()
    )
    att = _calc_attendance_stats(attendances)

    # ── 請假 ──
    approved_leaves = (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.is_approved == True,
            LeaveRecord.start_date <= end_date,
            LeaveRecord.end_date >= start_date,
        )
        .all()
    )
    daily_salary = base_salary / MONTHLY_BASE_DAYS if base_salary else 0
    # 年度累計病假時數（用於 30 日半薪上限判斷）
    year_start = date(year, 1, 1)
    prior_sick_hours = 0.0
    if start_date > year_start:
        prior_sick_hours = float(
            sum(
                lv.leave_hours or 0
                for lv in session.query(LeaveRecord)
                .filter(
                    LeaveRecord.employee_id == emp.id,
                    LeaveRecord.is_approved == True,
                    LeaveRecord.leave_type == "sick",
                    LeaveRecord.start_date >= year_start,
                    LeaveRecord.end_date < start_date,
                )
                .all()
            )
        )
    lv_result = _calc_leave_deductions(
        approved_leaves, daily_salary, ytd_sick_hours_before_month=prior_sick_hours
    )

    # ── 加班 ──
    approved_ot = (
        session.query(OvertimeRecord)
        .filter(
            OvertimeRecord.employee_id == emp.id,
            OvertimeRecord.is_approved == True,
            OvertimeRecord.overtime_date >= start_date,
            OvertimeRecord.overtime_date <= end_date,
        )
        .all()
    )
    ot_result = _calc_overtime_details(approved_ot)

    # ── 會議 ──
    mtg = _build_meeting_stats(
        session, engine, emp, start_date, end_date, is_bonus_month
    )

    # ── 班級脈絡 ──
    classroom_context = _build_classroom_context(session, emp, end_date)

    # ── 節慶獎金 ──
    supervisor_role = emp.supervisor_role or ""
    supervisor_base = engine.get_supervisor_festival_bonus(
        title_name, emp.position or "", supervisor_role
    )
    office_bonus_base = engine.get_office_festival_bonus_base(
        emp.position or "", title_name
    )
    office_staff_context = None
    if supervisor_base is not None or (
        office_bonus_base is not None and not classroom_context
    ):
        office_staff_context = {
            "school_enrollment": count_students_active_on(session, end_date)
        }

    festival_detail = _calc_festival_detail(
        engine,
        emp,
        effective_title,
        title_name,
        classroom_context,
        office_staff_context,
        supervisor_base,
        office_bonus_base,
        mtg["meeting_absence_deduction"],
        is_bonus_month,
    )
    bonus_forfeited_by_leave = lv_result["bonus_forfeited_by_leave"]
    if bonus_forfeited_by_leave and festival_detail:
        festival_detail["forfeited_by_leave"] = True
        for key in (
            "result_after_penalty",
            "festival_result_after_penalty",
            "overtime_result",
        ):
            if key in festival_detail:
                festival_detail[key] = 0

    # 主管月紅利與職務掛鉤、不隨請假時數歸零（engine.calculate_salary line 1010-1015
    # 明確保留）。此處若再歸零，SalaryRecord.supervisor_dividend 會與明細頁不一致。
    supervisor_dividend = engine.get_supervisor_dividend(
        title_name, emp.position or "", supervisor_role
    )

    # ── 曠職 ──
    absence = _calc_absence_days(
        session,
        emp,
        year,
        month,
        start_date,
        end_date,
        attendances,
        approved_leaves,
        resign_date,
    )

    # ── 保險 ──
    ins_result = _calc_insurance_details(emp, engine.insurance_service)
    ins = ins_result["ins"]

    # ── 遲到扣款明細 ──
    prorated_base = _prorate_for_period(
        base_salary, emp.hire_date, resign_date, year, month
    )
    birthday_bonus = 500 if (emp.birthday and emp.birthday.month == month) else 0
    per_minute_rate = (
        base_salary / (MONTHLY_BASE_DAYS * 8 * 60) if base_salary > 0 else 0
    )
    att_deduction_detail = []
    normal_late_deduction = 0
    for minutes in att["late_details"]:
        deduction = round(minutes * per_minute_rate)
        att_deduction_detail.append(
            {"minutes": minutes, "type": "per_minute", "deduction": deduction}
        )
        normal_late_deduction += deduction
    early_deduction = round(att["total_early_min"] * per_minute_rate)

    gross_salary = round(
        prorated_base
        + supervisor_dividend
        + birthday_bonus
        + mtg["meeting_overtime_pay_total"]
        + ot_result["ot_pay"]
    )
    total_deduction = round(
        ins.labor_employee
        + ins.health_employee
        + ins.pension_employee
        + normal_late_deduction
        + early_deduction
        + lv_result["leave_deduction_total"]
        + absence["absence_deduction_amount"]
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
            "late_count": att["late_count"],
            "early_leave_count": att["early_count"],
            "missing_punch_in": att["missing_in"],
            "missing_punch_out": att["missing_out"],
            "total_late_minutes": att["total_late_min"],
            "total_early_minutes": att["total_early_min"],
            "late_details": att["late_details"],
            "late_rows": att["late_rows"],
            "early_rows": att["early_rows"],
        },
        "deduction_calc": {
            "daily_salary": round(daily_salary),
            "per_minute_rate": round(per_minute_rate, 4),
            "late_deduction_detail": att_deduction_detail,
            "late_deduction": normal_late_deduction,
            "early_leave_deduction": early_deduction,
            "missing_punch_deduction": 0,
        },
        "leave_breakdown": lv_result["leave_breakdown"],
        "leave_deduction_total": lv_result["leave_deduction_total"],
        "overtime_pay": round(ot_result["ot_pay"]),
        "overtime_rows": ot_result["overtime_rows"],
        "meeting": {
            "attended": mtg["attended"],
            "absent_this_month": mtg["absent_current"],
            "absent_period": mtg["absent_period"],
            "meeting_absence_deduction": mtg["meeting_absence_deduction"],
            "overtime_pay_total": mtg["meeting_overtime_pay_total"],
            "absence_penalty_per_session": mtg["meeting_penalty"],
            "rows": mtg["rows"],
        },
        "classroom_context": classroom_context,
        "festival_bonus_detail": festival_detail,
        "supervisor_dividend": round(supervisor_dividend),
        "insurance": {
            "insured_amount_raw": ins_result["insured_salary_raw"],
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
            "meeting_overtime_pay": mtg["meeting_overtime_pay_total"],
            "absent_count": absence["absent_count"],
            "absent_days": [_to_iso(day) for day in absence["absent_days"]],
            "absence_deduction": absence["absence_deduction_amount"],
            "personal_sick_leave_hours": lv_result["personal_sick_leave_hours"],
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
            {
                "date": "-",
                "hours": 0,
                "overtime_type": "-",
                "pay": 0,
                "remark": "當月無核准加班紀錄",
            }
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
                "remark": (
                    "事假/病假 > 40 小時取消" if forfeited else "依主管紅利設定發放"
                ),
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
                "salaryMonth": f"{record.salary_year}-{record.salary_month:02d}",
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
        data["rows"] = rows or [
            {
                "type": "-",
                "start": "-",
                "end": "-",
                "hours": 0,
                "ratio": 0,
                "deduction": 0,
            }
        ]
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
                "deduction": round(
                    row["minutes"] * snapshot["deduction_calc"]["per_minute_rate"]
                ),
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
                "deduction": round(
                    row["minutes"] * snapshot["deduction_calc"]["per_minute_rate"]
                ),
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
