"""
Salary calculation and management router
"""

import io
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sqlalchemy.orm import joinedload
from models.database import (
    get_session, Employee, Classroom, ClassGrade, Student,
    SalaryRecord, EmployeeAllowance, AllowanceType
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["salary"])


# ============ Service Init ============

_salary_engine = None
_insurance_service = None


def init_salary_services(salary_engine, insurance_service):
    global _salary_engine, _insurance_service
    _salary_engine = salary_engine
    _insurance_service = insurance_service


# ============ Pydantic Models ============

class ClassBonusParam(BaseModel):
    classroom_id: int
    target_enrollment: int = 0
    current_enrollment: int = 0


class BonusSettings(BaseModel):
    year: int
    month: int
    target_enrollment: int = 160  # Default global target
    current_enrollment: int = 133  # Default global current
    festival_bonus_base: float = 0
    overtime_bonus_per_student: float = 500
    class_params: List[ClassBonusParam] = []
    position_bonus_base: Optional[Dict[str, float]] = None


class BonusBaseConfig(BaseModel):
    """獎金基數設定"""
    headTeacherAB: float = 2000
    headTeacherC: float = 1500
    assistantTeacherAB: float = 1200
    assistantTeacherC: float = 1200


class GradeTargetConfig(BaseModel):
    """單一年級目標人數設定"""
    twoTeachers: int = 0
    oneTeacher: int = 0
    sharedAssistant: int = 0


class OfficeFestivalBonusBase(BaseModel):
    """司機/美編/行政節慶獎金基數"""
    driver: float = 1000    # 司機
    designer: float = 1000  # 美編
    admin: float = 2000     # 行政


class SupervisorFestivalBonusConfig(BaseModel):
    """主管節慶獎金基數設定"""
    principal: float = 6500   # 園長
    director: float = 3500    # 主任
    leader: float = 2000      # 組長


class SupervisorDividendConfig(BaseModel):
    """主管紅利設定"""
    principal: float = 5000   # 園長
    director: float = 4000    # 主任
    leader: float = 3000      # 組長
    viceLeader: float = 1500  # 副組長


class OvertimePerPersonConfig(BaseModel):
    """超額獎金每人金額設定"""
    headBig: float = 400
    headMid: float = 400
    headSmall: float = 400
    headBaby: float = 450
    assistantBig: float = 100
    assistantMid: float = 100
    assistantSmall: float = 100
    assistantBaby: float = 150


class BonusConfigSchema(BaseModel):
    """完整獎金設定"""
    bonusBase: BonusBaseConfig = BonusBaseConfig()
    targetEnrollment: Dict[str, GradeTargetConfig] = {}
    officeFestivalBonusBase: Optional[OfficeFestivalBonusBase] = None
    supervisorFestivalBonus: Optional[SupervisorFestivalBonusConfig] = None
    supervisorDividend: Optional[SupervisorDividendConfig] = None
    overtimePerPerson: Optional[OvertimePerPersonConfig] = None
    overtimeTarget: Optional[Dict[str, GradeTargetConfig]] = None


class ClassEnrollment(BaseModel):
    """班級在籍人數"""
    classroom_id: int
    current_enrollment: int = 0


class CalculateSalaryRequest(BaseModel):
    year: int
    month: int
    bonus_settings: Optional[BonusSettings] = None
    # 新版設定
    bonus_config: Optional[BonusConfigSchema] = None
    class_enrollments: Optional[List[ClassEnrollment]] = None
    overtime_bonus_per_student: float = 400
    # 辦公室人員用全校超額目標
    school_wide_overtime_target: int = 0


# ============ Routes ============

@router.post("/salary/calculate")
async def calculate_salaries(request: CalculateSalaryRequest):
    """一鍵結算薪資"""
    session = get_session()
    employees = session.query(Employee).filter(Employee.is_active == True).all()

    # 如果有新版獎金設定，先套用到 salary_engine
    if request.bonus_config:
        bonus_config_dict = request.bonus_config.dict() if hasattr(request.bonus_config, 'dict') else request.bonus_config
        _salary_engine.set_bonus_config(bonus_config_dict)

    # 預先抓取所有員工的津貼設定
    all_allowances = session.query(EmployeeAllowance, AllowanceType).join(AllowanceType).filter(
        EmployeeAllowance.is_active == True
    ).all()

    # 將津貼依照 employee_id 分組
    allowance_map = {}
    for ea, at in all_allowances:
        if ea.employee_id not in allowance_map:
            allowance_map[ea.employee_id] = []
        allowance_map[ea.employee_id].append({
            "name": at.name,
            "amount": ea.amount,
            "code": at.code
        })

    # 取得班級資料（含年級）
    classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()

    # 取得年級對照表
    grades = session.query(ClassGrade).all()
    grade_map = {g.id: g.name for g in grades}

    # 建立班級在籍人數對照表（從前端傳入）
    enrollment_map = {}
    if request.class_enrollments:
        for ce in request.class_enrollments:
            enrollment_map[ce.classroom_id] = ce.current_enrollment

    # 建立班級詳細資訊對照表
    classroom_info_map = {}  # classroom_id -> classroom info
    for c in classrooms:
        # 優先使用前端傳入的在籍人數，否則從資料庫計算
        if c.id in enrollment_map:
            student_count = enrollment_map[c.id]
        else:
            student_count = session.query(Student).filter(
                Student.classroom_id == c.id,
                Student.is_active == True
            ).count()

        classroom_info_map[c.id] = {
            "id": c.id,
            "name": c.name,
            "grade_id": c.grade_id,
            "grade_name": grade_map.get(c.grade_id, ''),
            "head_teacher_id": c.head_teacher_id,
            "assistant_teacher_id": c.assistant_teacher_id,
            "art_teacher_id": c.art_teacher_id,
            "has_assistant": c.assistant_teacher_id is not None,
            "current_enrollment": student_count
        }

    # 建立員工角色對照表: emp_id -> [(classroom_id, role), ...]
    # 一個員工可能在多個班級擔任不同角色（如共用副班導跨班）
    # 注意：美師是 part-time，不參與節慶獎金計算
    emp_role_map: Dict[int, list] = {}
    for c in classrooms:
        if c.head_teacher_id:
            if c.head_teacher_id not in emp_role_map:
                emp_role_map[c.head_teacher_id] = []
            emp_role_map[c.head_teacher_id].append((c.id, 'head_teacher'))
        if c.assistant_teacher_id:
            if c.assistant_teacher_id not in emp_role_map:
                emp_role_map[c.assistant_teacher_id] = []
            emp_role_map[c.assistant_teacher_id].append((c.id, 'assistant_teacher'))
        # 美師是 part-time，不加入角色對照表，不參與獎金計算

    # 計算全校在籍人數（用於辦公室人員）
    total_school_enrollment = sum(info['current_enrollment'] for info in classroom_info_map.values())
    school_wide_overtime_target = request.school_wide_overtime_target

    results = []

    # 舊版相容：建立班級參數對照表
    class_bonus_map = {}
    if request.bonus_settings and request.bonus_settings.class_params:
        for p in request.bonus_settings.class_params:
            class_bonus_map[p.classroom_id] = {
                "target": p.target_enrollment,
                "current": p.current_enrollment
            }

    # 舊版獎金設定 (相容性保留)
    global_bonus_settings = None
    if request.bonus_settings:
        global_bonus_settings = {
            "target": request.bonus_settings.target_enrollment,
            "current": request.bonus_settings.current_enrollment,
            "festival_base": request.bonus_settings.festival_bonus_base,
            "overtime_per": request.bonus_settings.overtime_bonus_per_student
        }

    # 批次載入所有園務會議記錄，避免 N+1
    from models.database import MeetingRecord
    import calendar
    _, last_day = calendar.monthrange(request.year, request.month)
    salary_start = date(request.year, request.month, 1)
    salary_end = date(request.year, request.month, last_day)

    all_meetings = session.query(MeetingRecord).filter(
        MeetingRecord.meeting_date >= salary_start,
        MeetingRecord.meeting_date <= salary_end
    ).all()

    meeting_by_emp = {}
    for m in all_meetings:
        meeting_by_emp.setdefault(m.employee_id, []).append(m)

    for emp in employees:
        emp_dict = {
            "name": emp.name,
            "employee_id": emp.employee_id,
            "employee_type": emp.employee_type,
            "position": emp.position,
            "title": emp.title,
            "base_salary": emp.base_salary,
            "hourly_rate": emp.hourly_rate,
            "supervisor_allowance": emp.supervisor_allowance,
            "teacher_allowance": emp.teacher_allowance,
            "meal_allowance": emp.meal_allowance,
            "transportation_allowance": emp.transportation_allowance,
            "insurance_salary": emp.insurance_salary_level or emp.base_salary,
            "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
            "is_office_staff": emp.is_office_staff or False
        }

        # 取得該員工的津貼列表
        emp_allowances = allowance_map.get(emp.id, [])

        # 建立 classroom_context（新版節慶獎金計算）
        classroom_context = None
        is_office_staff = emp.is_office_staff or False

        if emp.id in emp_role_map:
            roles = emp_role_map[emp.id]

            # 檢查是否為司機或美編（特殊處理：節慶獎金用全校比例，無超額獎金）
            # 同時檢查 position 和 title
            office_festival_base = _salary_engine.get_office_festival_bonus_base(emp.position or '', emp.title or '')

            if office_festival_base is not None:
                # 司機/美編：節慶獎金用全校比例計算，無超額獎金
                is_eligible = _salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
                school_festival_bonus = 0

                if is_eligible and school_wide_overtime_target > 0:
                    school_ratio = total_school_enrollment / school_wide_overtime_target
                    school_festival_bonus = office_festival_base * school_ratio

                emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
                emp_dict['_calculated_overtime_bonus'] = 0  # 司機/美編無超額獎金

            # 辦公室人員有帶班：節慶獎金用全校計算，超額獎金用班級計算
            elif is_office_staff and len(roles) > 0:
                # 檢查是否符合領取節慶獎金資格（入職滿3個月）
                is_eligible = _salary_engine.is_eligible_for_festival_bonus(emp.hire_date)

                school_festival_bonus = 0
                total_overtime_bonus = 0

                if is_eligible:
                    # 節慶獎金：用全校人數計算
                    if school_wide_overtime_target > 0:
                        # 取得獎金基數（依職位和角色）
                        first_classroom_id, first_role = roles[0]
                        first_classroom_info = classroom_info_map.get(first_classroom_id)
                        if first_classroom_info:
                            role_for_bonus = first_role if first_role != 'art_teacher' else 'assistant_teacher'
                            bonus_base = _salary_engine.get_festival_bonus_base(emp.position or '', role_for_bonus)
                            # 全校比例 = 全校在籍 / 全校目標
                            school_ratio = total_school_enrollment / school_wide_overtime_target
                            school_festival_bonus = bonus_base * school_ratio

                    # 超額獎金：依各班計算後加總
                    for classroom_id, role in roles:
                        classroom_info = classroom_info_map.get(classroom_id)
                        if classroom_info:
                            overtime_result = _salary_engine.calculate_overtime_bonus(
                                role=role,
                                grade_name=classroom_info['grade_name'],
                                current_enrollment=classroom_info['current_enrollment'],
                                has_assistant=classroom_info['has_assistant'],
                                is_shared_assistant=(role == 'art_teacher')
                            )
                            total_overtime_bonus += overtime_result['overtime_bonus']

                emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
                emp_dict['_calculated_overtime_bonus'] = total_overtime_bonus

            # 美師可能跨多個班級，需要累計多班獎金
            # 其他角色通常只有一個班級
            elif len(roles) == 1:
                # 單一班級
                classroom_id, role = roles[0]
                classroom_info = classroom_info_map.get(classroom_id)
                if classroom_info:
                    classroom_context = {
                        'role': role,
                        'grade_name': classroom_info['grade_name'],
                        'current_enrollment': classroom_info['current_enrollment'],
                        'has_assistant': classroom_info['has_assistant'],
                        'is_shared_assistant': False
                    }
            else:
                # 多班級：依各班計算後加總
                # 判斷是否為「2班共用副班導」- assistant_teacher 跨多班
                assistant_class_count = sum(1 for _, r in roles if r == 'assistant_teacher')
                is_shared_assistant = assistant_class_count > 1

                total_festival_bonus = 0
                total_overtime_bonus = 0

                # 檢查是否符合領取節慶獎金資格（入職滿3個月）
                is_eligible = _salary_engine.is_eligible_for_festival_bonus(emp.hire_date)

                if is_eligible:
                    for classroom_id, role in roles:
                        classroom_info = classroom_info_map.get(classroom_id)
                        if classroom_info:
                            # 使用 salary_engine 計算單班獎金
                            # 共用副班導使用 shared_assistant 目標人數
                            bonus_result = _salary_engine.calculate_festival_bonus_v2(
                                position=emp.position or '',
                                role=role,
                                grade_name=classroom_info['grade_name'],
                                current_enrollment=classroom_info['current_enrollment'],
                                has_assistant=classroom_info['has_assistant'],
                                is_shared_assistant=(is_shared_assistant and role == 'assistant_teacher')
                            )
                            total_festival_bonus += bonus_result['festival_bonus']
                            total_overtime_bonus += bonus_result['overtime_bonus']

                # 對於多班員工，不使用 classroom_context，直接設定獎金
                emp_dict['_calculated_festival_bonus'] = total_festival_bonus
                emp_dict['_calculated_overtime_bonus'] = total_overtime_bonus

        else:
            # 員工沒有帶班
            # 檢查是否為司機或美編（特殊處理：節慶獎金用全校比例，無超額獎金）
            # 同時檢查 position 和 title
            office_festival_base = _salary_engine.get_office_festival_bonus_base(emp.position or '', emp.title or '')

            if office_festival_base is not None:
                # 司機/美編：節慶獎金用全校比例計算，無超額獎金
                is_eligible = _salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
                school_festival_bonus = 0

                if is_eligible and school_wide_overtime_target > 0:
                    school_ratio = total_school_enrollment / school_wide_overtime_target
                    school_festival_bonus = office_festival_base * school_ratio

                emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
                emp_dict['_calculated_overtime_bonus'] = 0  # 司機/美編無超額獎金

            elif is_office_staff:
                # 辦公室人員沒有帶班，但仍用全校比例計算節慶獎金
                is_eligible = _salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
                school_festival_bonus = 0

                if is_eligible and school_wide_overtime_target > 0:
                    # 使用副班導的獎金基數
                    bonus_base = _salary_engine.get_festival_bonus_base(emp.position or '', 'assistant_teacher')
                    school_ratio = total_school_enrollment / school_wide_overtime_target
                    school_festival_bonus = bonus_base * school_ratio

                emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
                emp_dict['_calculated_overtime_bonus'] = 0  # 沒有帶班，無超額獎金

        # 從預載入的會議記錄取得該員工的資料
        meeting_records = meeting_by_emp.get(emp.id, [])

        meeting_context = None
        if meeting_records:
            meeting_attended = sum(1 for m in meeting_records if m.attended)
            meeting_absent = sum(1 for m in meeting_records if not m.attended)
            meeting_context = {
                'attended': meeting_attended,
                'absent': meeting_absent,
                'work_end_time': emp.work_end_time or '17:00'
            }

        # 決定獎金設定方式
        if '_calculated_festival_bonus' in emp_dict:
            # 多班員工（共用副班導/美師等）：直接使用已計算的獎金
            breakdown = _salary_engine.calculate_salary(
                emp_dict,
                request.year,
                request.month,
                bonus_settings=None,
                allowances=emp_allowances,
                classroom_context=None,
                meeting_context=meeting_context
            )
            breakdown.festival_bonus = emp_dict['_calculated_festival_bonus']
            breakdown.overtime_bonus = emp_dict.get('_calculated_overtime_bonus', 0)
            # 計算主管紅利（同時檢查 title 和 position）
            breakdown.supervisor_dividend = _salary_engine.get_supervisor_dividend(emp.title or '', emp.position or '')
            # 重新計算應發總額
            breakdown.gross_salary = (
                breakdown.base_salary +
                breakdown.supervisor_allowance +
                breakdown.teacher_allowance +
                breakdown.meal_allowance +
                breakdown.transportation_allowance +
                breakdown.other_allowance +
                breakdown.festival_bonus +
                breakdown.overtime_bonus +
                breakdown.performance_bonus +
                breakdown.special_bonus +
                breakdown.supervisor_dividend +
                breakdown.meeting_overtime_pay
            )
            breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction
        elif classroom_context:
            # 使用新版計算（有 classroom_context）
            breakdown = _salary_engine.calculate_salary(
                emp_dict,
                request.year,
                request.month,
                bonus_settings=None,
                allowances=emp_allowances,
                classroom_context=classroom_context,
                meeting_context=meeting_context
            )
        else:
            # 使用舊版計算（沒有班級角色，如園長、行政等）
            breakdown = _salary_engine.calculate_salary(
                emp_dict,
                request.year,
                request.month,
                bonus_settings=global_bonus_settings,
                allowances=emp_allowances,
                meeting_context=meeting_context
            )

        results.append(breakdown.__dict__)

    session.close()
    return {"message": "薪資結算完成", "results": results}


@router.post("/salaries/calculate")
def calculate_salaries_alt(
    year: int = Query(..., description="Calculate for which year"),
    month: int = Query(..., description="Calculate for which month")
):
    """
    Calculate or Recalculate salaries for all employees for a given month.
    """
    session = get_session()
    try:
        from services.salary_engine import SalaryEngine as Engine
        engine = Engine(load_from_db=True)

        # 1. Fetch all active employees
        employees = session.query(Employee).filter(Employee.is_active == True).all()

        results = []
        for emp in employees:
            try:
                # Calculate salary for this employee using new process method
                salary_record = engine.process_salary_calculation(emp.id, year, month)

                # Convert to dict for response
                results.append({
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "base_salary": salary_record.base_salary,
                    "total_allowances": salary_record.total_allowances,
                    "festival_bonus": salary_record.festival_bonus,
                    "overtime_bonus": salary_record.overtime_bonus,
                    "overtime_pay": salary_record.overtime_work_pay,
                    "supervisor_dividend": salary_record.supervisor_dividend,
                    "labor_insurance": salary_record.labor_insurance,
                    "health_insurance": salary_record.health_insurance,
                    "late_deduction": salary_record.late_deduction,
                    "early_leave_deduction": salary_record.early_leave_deduction,
                    "missing_punch_deduction": salary_record.missing_punch_deduction,
                    "leave_deduction": salary_record.leave_deduction,
                    "attendance_deduction": (salary_record.late_deduction or 0) + (salary_record.early_leave_deduction or 0) + (salary_record.missing_punch_deduction or 0),
                    "meeting_overtime_pay": salary_record.meeting_overtime_pay or 0,
                    "meeting_absence_deduction": salary_record.meeting_absence_deduction or 0,
                    "total_deductions": salary_record.total_deduction,
                    "net_pay": salary_record.net_salary
                })
            except Exception as e:
                logger.error(f"Error calculating for {emp.name}: {e}")
                # Log error but continue

        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/salaries/festival-bonus")
def get_festival_bonus(
    year: int = Query(...),
    month: int = Query(...)
):
    """
    Return breakdown of festival bonus calculation
    """
    session = get_session()
    try:
        from services.salary_engine import SalaryEngine as Engine
        engine = Engine(load_from_db=True)

        employees = session.query(Employee).filter(Employee.is_active == True).all()
        results = []

        for emp in employees:
            bonus_data = engine.calculate_festival_bonus_breakdown(emp.id, year, month)
            results.append(bonus_data)

        # Sort by category/name
        return results
    except Exception as e:
        logger.error(f"Error getting festival bonus: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/salaries/records")
def get_salary_records(
    year: int = Query(...),
    month: int = Query(...)
):
    """查詢某月薪資記錄"""
    session = get_session()
    try:
        records = session.query(SalaryRecord, Employee).join(
            Employee, SalaryRecord.employee_id == Employee.id
        ).options(
            joinedload(Employee.job_title_rel)
        ).filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month
        ).order_by(Employee.name).all()

        results = []
        for record, emp in records:
            job_title = ''
            if emp.job_title_rel:
                job_title = emp.job_title_rel.name
            elif emp.title:
                job_title = emp.title

            results.append({
                "id": record.id,
                "employee_id": emp.id,
                "employee_code": emp.employee_id,
                "employee_name": emp.name,
                "job_title": job_title,
                "base_salary": record.base_salary,
                "supervisor_allowance": record.supervisor_allowance,
                "teacher_allowance": record.teacher_allowance,
                "meal_allowance": record.meal_allowance,
                "transportation_allowance": record.transportation_allowance,
                "other_allowance": record.other_allowance,
                "festival_bonus": record.festival_bonus,
                "overtime_bonus": record.overtime_bonus,
                "overtime_pay": record.overtime_pay,
                "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                "performance_bonus": record.performance_bonus,
                "special_bonus": record.special_bonus,
                "supervisor_dividend": record.bonus_amount or 0,
                "labor_insurance": record.labor_insurance_employee,
                "health_insurance": record.health_insurance_employee,
                "pension": record.pension_employee,
                "late_deduction": record.late_deduction,
                "early_leave_deduction": record.early_leave_deduction,
                "missing_punch_deduction": record.missing_punch_deduction,
                "attendance_deduction": (record.late_deduction or 0) + (record.early_leave_deduction or 0) + (record.missing_punch_deduction or 0),
                "leave_deduction": record.leave_deduction,
                "other_deduction": record.other_deduction,
                "gross_salary": record.gross_salary,
                "total_deduction": record.total_deduction,
                "net_salary": record.net_salary,
                "is_finalized": record.is_finalized,
            })

        return results
    finally:
        session.close()


@router.get("/salaries/{record_id}/export")
def export_salary_slip(
    record_id: int,
    format: str = Query("pdf", pattern="^(pdf)$")
):
    """匯出單人薪資單 PDF"""
    from services.salary_slip import generate_salary_pdf

    session = get_session()
    try:
        record = session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="薪資記錄不存在")

        emp = session.query(Employee).filter(Employee.id == record.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        pdf_bytes = generate_salary_pdf(record, emp, record.salary_year, record.salary_month)

        filename = f"salary_{emp.name}_{record.salary_year}_{record.salary_month:02d}.pdf"
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
        )
    finally:
        session.close()


@router.get("/salaries/export-all")
def export_all_salaries(
    year: int = Query(...),
    month: int = Query(...),
    format: str = Query("xlsx", pattern="^(xlsx)$")
):
    """匯出全部員工薪資 Excel"""
    from services.salary_slip import generate_salary_excel

    session = get_session()
    try:
        records = session.query(SalaryRecord, Employee).join(
            Employee, SalaryRecord.employee_id == Employee.id
        ).filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month
        ).order_by(Employee.name).all()

        if not records:
            raise HTTPException(status_code=404, detail="該月份無薪資記錄")

        excel_bytes = generate_salary_excel(records, year, month)

        filename = f"salary_all_{year}_{month:02d}.xlsx"
        return StreamingResponse(
            io.BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
        )
    finally:
        session.close()


@router.get("/salaries/history")
def get_salary_history(
    employee_id: int = Query(...),
    months: int = Query(12, ge=1, le=60)
):
    """查詢員工歷史薪資"""
    session = get_session()
    try:
        records = session.query(SalaryRecord).filter(
            SalaryRecord.employee_id == employee_id
        ).order_by(
            SalaryRecord.salary_year.desc(),
            SalaryRecord.salary_month.desc()
        ).limit(months).all()

        results = []
        for r in records:
            total_allowances = (
                (r.supervisor_allowance or 0) +
                (r.teacher_allowance or 0) +
                (r.meal_allowance or 0) +
                (r.transportation_allowance or 0) +
                (r.other_allowance or 0)
            )
            total_bonus = (
                (r.festival_bonus or 0) +
                (r.overtime_bonus or 0) +
                (r.performance_bonus or 0) +
                (r.special_bonus or 0) +
                (r.bonus_amount or 0)
            )
            results.append({
                "id": r.id,
                "year": r.salary_year,
                "month": r.salary_month,
                "base_salary": r.base_salary,
                "total_allowances": total_allowances,
                "total_bonus": total_bonus,
                "labor_insurance": r.labor_insurance_employee,
                "health_insurance": r.health_insurance_employee,
                "attendance_deduction": (
                    (r.late_deduction or 0) +
                    (r.early_leave_deduction or 0) +
                    (r.missing_punch_deduction or 0) +
                    (r.leave_deduction or 0)
                ),
                "gross_salary": r.gross_salary,
                "total_deduction": r.total_deduction,
                "net_salary": r.net_salary,
            })

        return results
    finally:
        session.close()


@router.get("/salaries/history-all")
def get_salary_history_all(
    year: int = Query(...)
):
    """查詢全部員工年度薪資概覽"""
    session = get_session()
    try:
        records = session.query(SalaryRecord, Employee).join(
            Employee, SalaryRecord.employee_id == Employee.id
        ).filter(
            SalaryRecord.salary_year == year
        ).order_by(
            Employee.name,
            SalaryRecord.salary_month
        ).all()

        # Group by employee
        grouped = defaultdict(list)
        emp_names = {}
        for r, emp in records:
            grouped[emp.id].append({
                "month": r.salary_month,
                "net_salary": r.net_salary,
                "gross_salary": r.gross_salary,
            })
            emp_names[emp.id] = emp.name

        results = []
        for emp_id, months_data in grouped.items():
            results.append({
                "employee_id": emp_id,
                "employee_name": emp_names[emp_id],
                "months": months_data
            })

        return results
    finally:
        session.close()
