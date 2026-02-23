"""
開發用 API - 檢視薪資計算邏輯、出缺勤規則、系統設定
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, Query
from utils.auth import require_permission
from utils.permissions import Permission

from models.database import (
    get_session, Employee, Attendance, LeaveRecord, OvertimeRecord,
    Classroom, ClassGrade, Student, ShiftType, ShiftAssignment, DailyShift,
    AttendancePolicy, BonusConfig, GradeTarget, InsuranceRate,
    MeetingRecord, EmployeeAllowance, AllowanceType, JobTitle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dev", tags=["dev"])

_salary_engine = None


def init_dev_services(salary_engine):
    global _salary_engine
    _salary_engine = salary_engine


@router.get("/salary-logic")
def get_salary_logic(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    """傾印目前的薪資計算邏輯與所有參數設定"""
    session = get_session()
    try:
        engine = _salary_engine

        # 1. 考勤政策
        policy = session.query(AttendancePolicy).filter(AttendancePolicy.is_active == True).first()
        attendance_policy = None
        if policy:
            attendance_policy = {
                "default_work_start": policy.default_work_start,
                "default_work_end": policy.default_work_end,
                "grace_minutes": policy.grace_minutes,
                "late_threshold": policy.late_threshold,
                "late_deduction": policy.late_deduction,
                "early_leave_deduction": policy.early_leave_deduction,
                "missing_punch_deduction": policy.missing_punch_deduction,
                "festival_bonus_months": policy.festival_bonus_months,
            }

        # 2. 獎金設定
        bonus = session.query(BonusConfig).filter(BonusConfig.is_active == True).first()
        bonus_config = None
        if bonus:
            bonus_config = {
                "config_year": bonus.config_year,
                "head_teacher_ab": bonus.head_teacher_ab,
                "head_teacher_c": bonus.head_teacher_c,
                "assistant_teacher_ab": bonus.assistant_teacher_ab,
                "assistant_teacher_c": bonus.assistant_teacher_c,
                "principal_festival": bonus.principal_festival,
                "director_festival": bonus.director_festival,
                "leader_festival": bonus.leader_festival,
                "driver_festival": bonus.driver_festival,
                "designer_festival": bonus.designer_festival,
                "admin_festival": bonus.admin_festival,
                "principal_dividend": bonus.principal_dividend,
                "director_dividend": bonus.director_dividend,
                "leader_dividend": bonus.leader_dividend,
                "vice_leader_dividend": bonus.vice_leader_dividend,
                "overtime_head_normal": bonus.overtime_head_normal,
                "overtime_head_baby": bonus.overtime_head_baby,
                "overtime_assistant_normal": bonus.overtime_assistant_normal,
                "overtime_assistant_baby": bonus.overtime_assistant_baby,
                "school_wide_target": bonus.school_wide_target,
            }

        # 3. 年級目標
        targets = session.query(GradeTarget).order_by(GradeTarget.grade_name).all()
        grade_targets = [{
            "grade_name": t.grade_name,
            "festival_two_teachers": t.festival_two_teachers,
            "festival_one_teacher": t.festival_one_teacher,
            "festival_shared": t.festival_shared,
            "overtime_two_teachers": t.overtime_two_teachers,
            "overtime_one_teacher": t.overtime_one_teacher,
            "overtime_shared": t.overtime_shared,
        } for t in targets]

        # 4. 勞健保費率
        rate = session.query(InsuranceRate).filter(InsuranceRate.is_active == True).first()
        insurance_rate = None
        if rate:
            insurance_rate = {
                "rate_year": rate.rate_year,
                "labor_rate": rate.labor_rate,
                "labor_employee_ratio": rate.labor_employee_ratio,
                "labor_employer_ratio": rate.labor_employer_ratio,
                "health_rate": rate.health_rate,
                "health_employee_ratio": rate.health_employee_ratio,
                "health_employer_ratio": rate.health_employer_ratio,
                "pension_employer_rate": rate.pension_employer_rate,
                "average_dependents": rate.average_dependents,
            }

        # 5. Engine 內部運算參數
        engine_config = {}
        if engine:
            engine_config = {
                "deduction_rules": engine.deduction_rules,
                "attendance_policy": engine._attendance_policy,
                "school_wide_target": engine._school_wide_target,
                "pension_self_rate": engine._pension_self_rate,
                "meeting_pay": engine._meeting_pay,
                "meeting_pay_6pm": engine._meeting_pay_6pm,
                "meeting_absence_penalty": engine._meeting_absence_penalty,
                "bonus_base": engine._bonus_base,
                "target_enrollment": engine._target_enrollment,
                "overtime_target": engine._overtime_target,
                "overtime_per_person": engine._overtime_per_person,
                "supervisor_dividend": engine._supervisor_dividend,
                "supervisor_festival_bonus": engine._supervisor_festival_bonus,
                "office_festival_bonus_base": engine._office_festival_bonus_base,
                "position_grade_map": engine.POSITION_GRADE_MAP,
            }

        # 6. 班別設定
        shift_types = session.query(ShiftType).order_by(ShiftType.sort_order).all()
        shifts = [{
            "id": s.id,
            "name": s.name,
            "work_start": s.work_start,
            "work_end": s.work_end,
            "is_active": s.is_active,
        } for s in shift_types]

        # 7. 請假扣薪規則（硬編碼在 process_salary_calculation 中）
        leave_deduction_rules = {
            "personal": {"label": "事假", "ratio": 1.0, "note": "全額扣薪"},
            "sick": {"label": "病假", "ratio": 0.5, "note": "扣半薪"},
            "menstrual": {"label": "生理假", "ratio": 0.5, "note": "扣半薪"},
            "annual": {"label": "特休", "ratio": 0.0, "note": "不扣薪"},
            "maternity": {"label": "產假", "ratio": 0.0, "note": "不扣薪"},
            "paternity": {"label": "陪產假", "ratio": 0.0, "note": "不扣薪"},
        }

        # 8. 薪資公式說明
        salary_formula = {
            "gross_salary": "底薪 + 津貼(主管/導師/伙食/交通/其他) + 節慶獎金 + 超額獎金 + 績效獎金 + 特別獎金 + 主管紅利 + 加班費 + 園務會議加班費",
            "total_deduction": "勞保(員工) + 健保(員工) + 勞退自提 + 遲到扣款 + 早退扣款 + 遲到轉事假扣款 + 請假扣款 + 其他扣款",
            "net_salary": "gross_salary - total_deduction",
            "late_deduction_formula": "遲到分鐘 × (月薪 ÷ (當月工作日數 × 8 × 60))",
            "early_leave_deduction_formula": "早退分鐘 × (月薪 ÷ (當月工作日數 × 8 × 60))",
            "auto_leave_rule": "遲到 ≥ 120 分鐘 → 不扣分鐘費，改扣事假半天 (日薪 × 0.5)",
            "daily_salary": "月薪 ÷ 當月工作日數",
            "per_minute_rate": "月薪 ÷ (當月工作日數 × 8小時 × 60分鐘)",
            "leave_deduction": "請假天數 × 日薪 × 扣薪比例 (事假1.0 / 病假0.5 / 特休0.0)",
            "missing_punch": "不扣款，僅記錄次數",
            "festival_bonus_teacher": "獎金基數 × (班級在籍人數 ÷ 目標人數)",
            "festival_bonus_supervisor": "主管基數 × (全校在籍 ÷ 全校目標)",
            "festival_bonus_office": "辦公室基數 × (全校在籍 ÷ 全校目標)",
            "festival_bonus_eligibility": "入職滿 3 個月",
            "overtime_bonus": "(在籍人數 - 超額目標) × 每人金額 (超額才有)",
            "meeting_overtime_pay": "出席次數 × 每次金額 ($200/$100)",
            "meeting_absence_deduction": "缺席次數 × $100 (從節慶獎金扣)",
            "insurance_lookup": "依投保薪資級距表查表，非按比例計算",
            "health_insurance_dependents": "健保員工自付 × (1 + min(眷屬人數, 3))",
        }

        return {
            "attendance_policy_db": attendance_policy,
            "bonus_config_db": bonus_config,
            "grade_targets_db": grade_targets,
            "insurance_rate_db": insurance_rate,
            "engine_runtime_config": engine_config,
            "shift_types": shifts,
            "leave_deduction_rules": leave_deduction_rules,
            "salary_formula": salary_formula,
        }
    finally:
        session.close()


@router.get("/employee-salary-debug")
def debug_employee_salary(
    current_user: dict = Depends(require_permission(Permission.SETTINGS_READ)),
    employee_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(...),
):
    """模擬計算單一員工薪資並回傳完整明細（不存檔）"""
    import calendar as cal_module

    session = get_session()
    try:
        engine = _salary_engine
        if not engine:
            return {"error": "SalaryEngine not initialized"}

        emp = session.query(Employee).get(employee_id)
        if not emp:
            return {"error": f"Employee {employee_id} not found"}

        title_name = emp.job_title_rel.name if emp.job_title_rel else (emp.title or '')
        _, last_day = cal_module.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)

        # Attendance
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

        # Leaves
        LEAVE_DEDUCTION_RULES = {
            "personal": 1.0, "sick": 0.5, "menstrual": 0.5,
            "annual": 0.0, "maternity": 0.0, "paternity": 0.0,
        }
        approved_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.is_approved == True,
            LeaveRecord.start_date <= end_date,
            LeaveRecord.end_date >= start_date,
        ).all()
        from services.salary_engine import get_working_days
        wd = get_working_days(year, month, session)
        daily_salary = emp.base_salary / wd if emp.base_salary else 0
        leave_deduction_total = 0
        leave_breakdown = []
        for lv in approved_leaves:
            ratio = LEAVE_DEDUCTION_RULES.get(lv.leave_type, 1.0)
            deduction = round((lv.leave_hours / 8) * daily_salary * ratio)
            leave_deduction_total += deduction
            leave_breakdown.append({
                "type": lv.leave_type,
                "start": lv.start_date.isoformat(),
                "end": lv.end_date.isoformat(),
                "hours": lv.leave_hours,
                "ratio": ratio,
                "deduction": deduction,
            })

        # Overtime
        approved_ot = session.query(OvertimeRecord).filter(
            OvertimeRecord.employee_id == emp.id,
            OvertimeRecord.is_approved == True,
            OvertimeRecord.overtime_date >= start_date,
            OvertimeRecord.overtime_date <= end_date,
        ).all()
        ot_pay = sum(o.overtime_pay or 0 for o in approved_ot)

        # Meeting
        meetings = session.query(MeetingRecord).filter(
            MeetingRecord.employee_id == emp.id,
            MeetingRecord.meeting_date >= start_date,
            MeetingRecord.meeting_date <= end_date,
        ).all()
        meeting_attended = sum(1 for m in meetings if m.attended)
        meeting_absent = sum(1 for m in meetings if not m.attended)

        # Allowances
        emp_allowances = session.query(EmployeeAllowance).filter(
            EmployeeAllowance.employee_id == emp.id,
            EmployeeAllowance.is_active == True,
        ).all()
        allowances = []
        for ea in emp_allowances:
            a_type = session.query(AllowanceType).get(ea.allowance_type_id)
            if a_type:
                allowances.append({"name": a_type.name, "amount": ea.amount})

        # Classroom context
        classroom_context = None
        classroom = None
        if emp.classroom_id:
            classroom = session.query(Classroom).get(emp.classroom_id)
        if classroom:
            role = 'assistant_teacher'
            if classroom.head_teacher_id == emp.id:
                role = 'head_teacher'
            elif classroom.art_teacher_id == emp.id:
                role = 'art_teacher'
            has_assistant = classroom.assistant_teacher_id is not None and classroom.assistant_teacher_id > 0
            student_count = session.query(Student).filter(
                Student.classroom_id == classroom.id, Student.is_active == True
            ).count()
            grade_name = classroom.grade.name if classroom.grade else ''
            classroom_context = {
                "role": role,
                "grade_name": grade_name,
                "current_enrollment": student_count,
                "has_assistant": has_assistant,
                "is_shared_assistant": False,
            }

        # Office / Supervisor context
        office_staff_context = None
        is_supervisor = engine.get_supervisor_festival_bonus(title_name, emp.position) is not None
        if emp.is_office_staff or is_supervisor:
            total_students = session.query(Student).filter(Student.is_active == True).count()
            office_staff_context = {"school_enrollment": total_students}

        # Per minute rate
        base_sal = emp.base_salary or 0
        per_minute_rate = base_sal / (wd * 8 * 60) if base_sal > 0 else 0

        # Attendance deduction detail
        auto_leave_threshold = engine._attendance_policy.get('auto_leave_threshold', 120)
        att_deduction_detail = []
        normal_late_deduction = 0
        auto_leave_deduction = 0
        auto_leave_count = 0
        for minutes in late_details:
            if minutes >= auto_leave_threshold:
                d = round(daily_salary * 0.5)
                att_deduction_detail.append({"minutes": minutes, "type": "auto_leave_half_day", "deduction": d})
                auto_leave_deduction += d
                auto_leave_count += 1
            else:
                d = round(minutes * per_minute_rate)
                att_deduction_detail.append({"minutes": minutes, "type": "per_minute", "deduction": d})
                normal_late_deduction += d

        early_deduction = round(total_early_min * per_minute_rate)

        # Festival bonus detail
        festival_detail = {}
        supervisor_festival_base = engine.get_supervisor_festival_bonus(title_name, emp.position or '')
        if supervisor_festival_base is not None:
            school_enrollment = office_staff_context['school_enrollment'] if office_staff_context else 0
            school_target = engine._school_wide_target or 160
            ratio = school_enrollment / school_target if school_target > 0 else 0
            is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
            festival_detail = {
                "category": "主管",
                "base": supervisor_festival_base,
                "enrollment": school_enrollment,
                "target": school_target,
                "ratio": round(ratio, 4),
                "eligible": is_eligible,
                "result": round(supervisor_festival_base * ratio) if is_eligible else 0,
            }
        elif office_staff_context and emp.position:
            office_base = engine.get_office_festival_bonus_base(emp.position or '', title_name)
            if office_base:
                school_enrollment = office_staff_context['school_enrollment']
                school_target = engine._school_wide_target or 160
                ratio = school_enrollment / school_target if school_target > 0 else 0
                is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
                festival_detail = {
                    "category": "辦公室",
                    "base": office_base,
                    "enrollment": school_enrollment,
                    "target": school_target,
                    "ratio": round(ratio, 4),
                    "eligible": is_eligible,
                    "result": round(office_base * ratio) if is_eligible else 0,
                }
        elif classroom_context:
            cc = classroom_context
            base_amount = engine.get_festival_bonus_base(emp.position or '', cc['role'] if cc['role'] != 'art_teacher' else 'assistant_teacher')
            target = engine.get_target_enrollment(cc['grade_name'], cc['has_assistant'], cc['is_shared_assistant'])
            ratio = cc['current_enrollment'] / target if target > 0 else 0
            is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
            ot_target = engine.get_overtime_target(cc['grade_name'], cc['has_assistant'], cc['is_shared_assistant'])
            ot_count = max(0, cc['current_enrollment'] - ot_target)
            ot_per = engine.get_overtime_per_person(cc['role'] if cc['role'] != 'art_teacher' else 'assistant_teacher', cc['grade_name'])
            festival_detail = {
                "category": "帶班老師",
                "role": cc['role'],
                "grade": cc['grade_name'],
                "base": base_amount,
                "enrollment": cc['current_enrollment'],
                "target": target,
                "ratio": round(ratio, 4),
                "eligible": is_eligible,
                "festival_result": round(base_amount * ratio) if is_eligible else 0,
                "overtime_target": ot_target,
                "overtime_count": ot_count,
                "overtime_per_person": ot_per,
                "overtime_result": round(ot_count * ot_per) if is_eligible else 0,
            }

        # Supervisor dividend
        supervisor_dividend = engine.get_supervisor_dividend(title_name, emp.position or '') if emp.position else 0

        # Insurance
        from services.insurance_service import InsuranceService
        ins_service = InsuranceService()
        ins_salary = emp.insurance_salary_level if emp.insurance_salary_level and emp.insurance_salary_level > 0 else base_sal
        ins = ins_service.calculate(ins_salary, emp.dependents or 0, pension_self_rate=engine._pension_self_rate)

        return {
            "employee": {
                "id": emp.id,
                "employee_id": emp.employee_id,
                "name": emp.name,
                "title": title_name,
                "position": emp.position,
                "employee_type": emp.employee_type,
                "base_salary": base_sal,
                "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
                "is_office_staff": emp.is_office_staff,
                "classroom_id": emp.classroom_id,
                "insurance_salary_level": emp.insurance_salary_level,
                "dependents": emp.dependents,
                "work_start_time": emp.work_start_time,
                "work_end_time": emp.work_end_time,
            },
            "period": {"year": year, "month": month},
            "attendance_summary": {
                "total_records": len(attendances),
                "late_count": late_count,
                "early_leave_count": early_count,
                "missing_punch_in": missing_in,
                "missing_punch_out": missing_out,
                "total_late_minutes": total_late_min,
                "total_early_minutes": total_early_min,
                "late_details": late_details,
            },
            "deduction_calc": {
                "daily_salary": round(daily_salary),
                "per_minute_rate": round(per_minute_rate, 4),
                "auto_leave_threshold_minutes": auto_leave_threshold,
                "late_deduction_detail": att_deduction_detail,
                "normal_late_deduction": normal_late_deduction,
                "auto_leave_deduction": auto_leave_deduction,
                "auto_leave_count": auto_leave_count,
                "early_leave_deduction": early_deduction,
                "missing_punch_deduction": 0,
            },
            "leave_breakdown": leave_breakdown,
            "leave_deduction_total": leave_deduction_total,
            "overtime_pay": ot_pay,
            "meeting": {
                "attended": meeting_attended,
                "absent": meeting_absent,
                "overtime_pay_per_session": engine._meeting_pay if (emp.work_end_time or '17:00') != '18:00' else engine._meeting_pay_6pm,
                "absence_penalty_per_session": engine._meeting_absence_penalty,
            },
            "allowances": allowances,
            "classroom_context": classroom_context,
            "festival_bonus_detail": festival_detail,
            "supervisor_dividend": supervisor_dividend,
            "insurance": {
                "insured_amount": ins.insured_amount,
                "labor_employee": ins.labor_employee,
                "labor_employer": ins.labor_employer,
                "health_employee": ins.health_employee,
                "health_employer": ins.health_employer,
                "pension_employee": ins.pension_employee,
                "pension_employer": ins.pension_employer,
                "total_employee_deduction": ins.total_employee,
            },
        }
    finally:
        session.close()
