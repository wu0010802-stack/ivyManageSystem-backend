"""
Salary calculation and management router
"""

import io
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.auth import require_permission
from utils.permissions import Permission
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

def _build_allowance_map(session):
    """預先抓取所有員工的津貼設定，依 employee_id 分組。"""
    all_allowances = session.query(EmployeeAllowance, AllowanceType).join(AllowanceType).filter(
        EmployeeAllowance.is_active == True
    ).all()
    allowance_map = {}
    for ea, at in all_allowances:
        allowance_map.setdefault(ea.employee_id, []).append({
            "name": at.name, "amount": ea.amount, "code": at.code
        })
    return allowance_map


def _build_classroom_info(session, enrollment_map):
    """建立班級詳細資訊對照表與員工角色對照表。"""
    classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()
    grade_map = {g.id: g.name for g in session.query(ClassGrade).all()}

    classroom_info_map = {}
    for c in classrooms:
        if c.id in enrollment_map:
            student_count = enrollment_map[c.id]
        else:
            student_count = session.query(Student).filter(
                Student.classroom_id == c.id, Student.is_active == True
            ).count()
        classroom_info_map[c.id] = {
            "id": c.id, "name": c.name,
            "grade_id": c.grade_id, "grade_name": grade_map.get(c.grade_id, ''),
            "head_teacher_id": c.head_teacher_id,
            "assistant_teacher_id": c.assistant_teacher_id,
            "art_teacher_id": c.art_teacher_id,
            "has_assistant": c.assistant_teacher_id is not None,
            "current_enrollment": student_count,
        }

    emp_role_map: Dict[int, list] = {}
    for c in classrooms:
        if c.head_teacher_id:
            emp_role_map.setdefault(c.head_teacher_id, []).append((c.id, 'head_teacher'))
        if c.assistant_teacher_id:
            emp_role_map.setdefault(c.assistant_teacher_id, []).append((c.id, 'assistant_teacher'))

    return classroom_info_map, emp_role_map


def _build_meeting_map(session, year, month):
    """批次載入該月所有園務會議記錄，依 employee_id 分組。"""
    from models.database import MeetingRecord
    import calendar
    _, last_day = calendar.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)
    all_meetings = session.query(MeetingRecord).filter(
        MeetingRecord.meeting_date >= start, MeetingRecord.meeting_date <= end
    ).all()
    meeting_by_emp = {}
    for m in all_meetings:
        meeting_by_emp.setdefault(m.employee_id, []).append(m)
    return meeting_by_emp


def _build_legacy_bonus_settings(request):
    """舊版獎金設定（相容性保留）。"""
    global_bonus_settings = None
    if request.bonus_settings:
        global_bonus_settings = {
            "target": request.bonus_settings.target_enrollment,
            "current": request.bonus_settings.current_enrollment,
            "festival_base": request.bonus_settings.festival_bonus_base,
            "overtime_per": request.bonus_settings.overtime_bonus_per_student,
        }
    return global_bonus_settings


def _calc_school_wide_bonus(emp, office_festival_base, total_enrollment, target):
    """計算全校比例節慶獎金（司機/美編/辦公室人員）。"""
    is_eligible = _salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
    if is_eligible and target > 0:
        school_ratio = total_enrollment / target
        return round(office_festival_base * school_ratio)
    return 0


def _resolve_bonus_for_employee(emp, emp_dict, emp_role_map, classroom_info_map,
                                total_school_enrollment, school_wide_overtime_target):
    """依員工角色與班級計算節慶獎金和超額獎金，回傳 classroom_context 或 None。"""
    is_office_staff = emp.is_office_staff or False
    classroom_context = None

    if emp.id in emp_role_map:
        roles = emp_role_map[emp.id]
        office_festival_base = _salary_engine.get_office_festival_bonus_base(emp.position or '', emp.title or '')

        if office_festival_base is not None:
            # 司機/美編
            emp_dict['_calculated_festival_bonus'] = _calc_school_wide_bonus(
                emp, office_festival_base, total_school_enrollment, school_wide_overtime_target)
            emp_dict['_calculated_overtime_bonus'] = 0

        elif is_office_staff and len(roles) > 0:
            # 辦公室人員有帶班
            is_eligible = _salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
            school_festival_bonus = 0
            total_overtime_bonus = 0
            if is_eligible:
                if school_wide_overtime_target > 0:
                    first_classroom_id, first_role = roles[0]
                    first_info = classroom_info_map.get(first_classroom_id)
                    if first_info:
                        role_for_bonus = first_role if first_role != 'art_teacher' else 'assistant_teacher'
                        bonus_base = _salary_engine.get_festival_bonus_base(emp.position or '', role_for_bonus)
                        school_festival_bonus = bonus_base * (total_school_enrollment / school_wide_overtime_target)
                for classroom_id, role in roles:
                    info = classroom_info_map.get(classroom_id)
                    if info:
                        result = _salary_engine.calculate_overtime_bonus(
                            role=role, grade_name=info['grade_name'],
                            current_enrollment=info['current_enrollment'],
                            has_assistant=info['has_assistant'],
                            is_shared_assistant=(role == 'art_teacher'))
                        total_overtime_bonus += result['overtime_bonus']
            emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
            emp_dict['_calculated_overtime_bonus'] = total_overtime_bonus

        elif len(roles) == 1:
            # 單一班級教師
            classroom_id, role = roles[0]
            info = classroom_info_map.get(classroom_id)
            if info:
                classroom_context = {
                    'role': role, 'grade_name': info['grade_name'],
                    'current_enrollment': info['current_enrollment'],
                    'has_assistant': info['has_assistant'], 'is_shared_assistant': False,
                }
        else:
            # 多班級（共用副班導等）
            assistant_count = sum(1 for _, r in roles if r == 'assistant_teacher')
            is_shared = assistant_count > 1
            total_festival = 0
            total_overtime = 0
            is_eligible = _salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
            if is_eligible:
                for classroom_id, role in roles:
                    info = classroom_info_map.get(classroom_id)
                    if info:
                        result = _salary_engine.calculate_festival_bonus_v2(
                            position=emp.position or '', role=role,
                            grade_name=info['grade_name'],
                            current_enrollment=info['current_enrollment'],
                            has_assistant=info['has_assistant'],
                            is_shared_assistant=(is_shared and role == 'assistant_teacher'))
                        total_festival += result['festival_bonus']
                        total_overtime += result['overtime_bonus']
            emp_dict['_calculated_festival_bonus'] = total_festival
            emp_dict['_calculated_overtime_bonus'] = total_overtime
    else:
        # 員工沒有帶班
        office_festival_base = _salary_engine.get_office_festival_bonus_base(emp.position or '', emp.title or '')
        if office_festival_base is not None:
            emp_dict['_calculated_festival_bonus'] = _calc_school_wide_bonus(
                emp, office_festival_base, total_school_enrollment, school_wide_overtime_target)
            emp_dict['_calculated_overtime_bonus'] = 0
        elif is_office_staff:
            bonus_base = _salary_engine.get_festival_bonus_base(emp.position or '', 'assistant_teacher')
            emp_dict['_calculated_festival_bonus'] = _calc_school_wide_bonus(
                emp, bonus_base, total_school_enrollment, school_wide_overtime_target)
            emp_dict['_calculated_overtime_bonus'] = 0

    return classroom_context


def _compute_salary_breakdown(emp, emp_dict, year, month, emp_allowances,
                              classroom_context, meeting_context, global_bonus_settings):
    """呼叫 salary_engine 計算薪資明細。"""
    if '_calculated_festival_bonus' in emp_dict:
        breakdown = _salary_engine.calculate_salary(
            emp_dict, year, month,
            bonus_settings=None, allowances=emp_allowances,
            classroom_context=None, meeting_context=meeting_context)
        breakdown.festival_bonus = emp_dict['_calculated_festival_bonus']
        breakdown.overtime_bonus = emp_dict.get('_calculated_overtime_bonus', 0)
        # 非發放月份不計節慶獎金（季度合併發放：2月、6月、9月、12月）
        if not _salary_engine.get_bonus_distribution_month(month):
            breakdown.festival_bonus = 0
        breakdown.supervisor_dividend = _salary_engine.get_supervisor_dividend(emp.title or '', emp.position or '')
        breakdown.gross_salary = (
            breakdown.base_salary + breakdown.supervisor_allowance +
            breakdown.teacher_allowance + breakdown.meal_allowance +
            breakdown.transportation_allowance + breakdown.other_allowance +
            breakdown.festival_bonus + breakdown.overtime_bonus +
            breakdown.performance_bonus + breakdown.special_bonus +
            breakdown.supervisor_dividend + breakdown.meeting_overtime_pay)
        breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction
    elif classroom_context:
        breakdown = _salary_engine.calculate_salary(
            emp_dict, year, month,
            bonus_settings=None, allowances=emp_allowances,
            classroom_context=classroom_context, meeting_context=meeting_context)
    else:
        breakdown = _salary_engine.calculate_salary(
            emp_dict, year, month,
            bonus_settings=global_bonus_settings, allowances=emp_allowances,
            meeting_context=meeting_context)
    return breakdown


@router.post("/salary/calculate")
async def calculate_salaries(request: CalculateSalaryRequest, current_user: dict = Depends(require_permission(Permission.SALARY_WRITE))):
    """一鍵結算薪資"""
    session = get_session()
    try:
        employees = session.query(Employee).filter(Employee.is_active == True).all()

        if request.bonus_config:
            bonus_config_dict = request.bonus_config.dict() if hasattr(request.bonus_config, 'dict') else request.bonus_config
            _salary_engine.set_bonus_config(bonus_config_dict)

        allowance_map = _build_allowance_map(session)

        enrollment_map = {}
        if request.class_enrollments:
            for ce in request.class_enrollments:
                enrollment_map[ce.classroom_id] = ce.current_enrollment

        classroom_info_map, emp_role_map = _build_classroom_info(session, enrollment_map)
        total_school_enrollment = sum(info['current_enrollment'] for info in classroom_info_map.values())
        meeting_by_emp = _build_meeting_map(session, request.year, request.month)
        global_bonus_settings = _build_legacy_bonus_settings(request)

        results = []
        for emp in employees:
            emp_dict = {
                "name": emp.name, "employee_id": emp.employee_id,
                "employee_type": emp.employee_type, "position": emp.position,
                "title": emp.title, "base_salary": emp.base_salary,
                "hourly_rate": emp.hourly_rate,
                "supervisor_allowance": emp.supervisor_allowance,
                "teacher_allowance": emp.teacher_allowance,
                "meal_allowance": emp.meal_allowance,
                "transportation_allowance": emp.transportation_allowance,
                "insurance_salary": emp.insurance_salary_level or emp.base_salary,
                "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
                "is_office_staff": emp.is_office_staff or False,
            }

            classroom_context = _resolve_bonus_for_employee(
                emp, emp_dict, emp_role_map, classroom_info_map,
                total_school_enrollment, request.school_wide_overtime_target)

            meeting_records = meeting_by_emp.get(emp.id, [])
            meeting_context = None
            if meeting_records:
                meeting_context = {
                    'attended': sum(1 for m in meeting_records if m.attended),
                    'absent': sum(1 for m in meeting_records if not m.attended),
                    'work_end_time': emp.work_end_time or '17:00',
                }

            breakdown = _compute_salary_breakdown(
                emp, emp_dict, request.year, request.month,
                allowance_map.get(emp.id, []), classroom_context,
                meeting_context, global_bonus_settings)

            results.append(breakdown.__dict__)

        return {"message": "薪資結算完成", "results": results}

    except Exception as e:
        session.rollback()
        logger.error(f"薪資批量計算失敗: {e}")
        raise HTTPException(status_code=500, detail=f"薪資計算失敗: {str(e)}")
    finally:
        session.close()


@router.post("/salaries/calculate")
def calculate_salaries_alt(
    current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)),
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
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
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
        return {"message": "薪資結算完成", "results": results}

    except Exception as e:
        logger.error(f"Error getting festival bonus: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/salaries/records")
def get_salary_records(
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
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
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
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
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
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
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
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
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
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
