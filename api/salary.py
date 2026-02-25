"""
Salary calculation and management router
"""

import io
import logging
from collections import defaultdict
from datetime import date, datetime, time
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.auth import require_permission
from utils.permissions import Permission
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from sqlalchemy import and_, or_
from sqlalchemy.orm import joinedload
import calendar as _cal

from models.database import (
    get_session, Employee, Classroom, ClassGrade, Student,
    SalaryRecord, EmployeeAllowance, AllowanceType, Attendance,
)

logger = logging.getLogger(__name__)

MAX_DAILY_WORK_HOURS = 12.0  # 時薪制每日工時上限（正常 8H + 最高加班 4H，防止打卡異常灌水）

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
    classroom_id: int = Field(..., ge=1)
    target_enrollment: int = Field(0, ge=0)
    current_enrollment: int = Field(0, ge=0)


class BonusSettings(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    target_enrollment: int = Field(160, ge=0)
    current_enrollment: int = Field(133, ge=0)
    festival_bonus_base: float = Field(0, ge=0)
    overtime_bonus_per_student: float = Field(500, ge=0)
    class_params: List[ClassBonusParam] = []
    position_bonus_base: Optional[Dict[str, float]] = None


class BonusBaseConfig(BaseModel):
    """獎金基數設定"""
    headTeacherAB: float = Field(2000, ge=0)
    headTeacherC: float = Field(1500, ge=0)
    assistantTeacherAB: float = Field(1200, ge=0)
    assistantTeacherC: float = Field(1200, ge=0)


class GradeTargetConfig(BaseModel):
    """單一年級目標人數設定"""
    twoTeachers: int = Field(0, ge=0)
    oneTeacher: int = Field(0, ge=0)
    sharedAssistant: int = Field(0, ge=0)


class OfficeFestivalBonusBase(BaseModel):
    """司機/美編/行政節慶獎金基數"""
    driver: float = Field(1000, ge=0)    # 司機
    designer: float = Field(1000, ge=0)  # 美編
    admin: float = Field(2000, ge=0)     # 行政


class SupervisorFestivalBonusConfig(BaseModel):
    """主管節慶獎金基數設定"""
    principal: float = Field(6500, ge=0)   # 園長
    director: float = Field(3500, ge=0)    # 主任
    leader: float = Field(2000, ge=0)      # 組長


class SupervisorDividendConfig(BaseModel):
    """主管紅利設定"""
    principal: float = Field(5000, ge=0)   # 園長
    director: float = Field(4000, ge=0)    # 主任
    leader: float = Field(3000, ge=0)      # 組長
    viceLeader: float = Field(1500, ge=0)  # 副組長


class OvertimePerPersonConfig(BaseModel):
    """超額獎金每人金額設定"""
    headBig: float = Field(400, ge=0)
    headMid: float = Field(400, ge=0)
    headSmall: float = Field(400, ge=0)
    headBaby: float = Field(450, ge=0)
    assistantBig: float = Field(100, ge=0)
    assistantMid: float = Field(100, ge=0)
    assistantSmall: float = Field(100, ge=0)
    assistantBaby: float = Field(150, ge=0)


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
    classroom_id: int = Field(..., ge=1)
    current_enrollment: int = Field(0, ge=0)


class CalculateSalaryRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    bonus_settings: Optional[BonusSettings] = None
    # 新版設定
    bonus_config: Optional[BonusConfigSchema] = None
    class_enrollments: Optional[List[ClassEnrollment]] = None
    overtime_bonus_per_student: float = Field(400, ge=0)
    # 辦公室人員用全校超額目標
    school_wide_overtime_target: int = Field(0, ge=0)


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


def _meeting_deduction_period_start(year: int, month: int):
    """
    返回發放月的會議缺席扣款起算日（與 SalaryEngine 同步，避免循環匯入）。
    計算範圍 = 上次發放月（不含）至當發放月（含）之間所有非發放月。
    非發放月返回 None。
    """
    if month == 2:
        return date(year, 1, 1)
    elif month == 6:
        return date(year, 3, 1)
    elif month == 9:
        return date(year, 7, 1)
    elif month == 12:
        return date(year, 10, 1)
    return None


def _build_meeting_map(session, year, month):
    """
    批次載入該月所有園務會議記錄，依 employee_id 分組。
    同時回傳發放月前幾個非發放月的缺席次數（用於補扣）。

    Returns:
        meeting_by_emp: {employee_id: [MeetingRecord, ...]}（當月，用於加班費）
        prior_absent_by_emp: {employee_id: int}（前幾個非發放月缺席次數，發放月才有值）
    """
    from models.database import MeetingRecord
    import calendar
    _, last_day = calendar.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)
    all_meetings = session.query(MeetingRecord).filter(
        MeetingRecord.meeting_date >= start, MeetingRecord.meeting_date <= end
    ).all()
    meeting_by_emp: dict = {}
    for m in all_meetings:
        meeting_by_emp.setdefault(m.employee_id, []).append(m)

    # 發放月：補查前幾個非發放月的缺席記錄，確保不漏扣
    prior_absent_by_emp: dict = {}
    period_start = _meeting_deduction_period_start(year, month)
    if period_start is not None and period_start < start:
        prior_records = session.query(MeetingRecord).filter(
            MeetingRecord.meeting_date >= period_start,
            MeetingRecord.meeting_date < start,
        ).all()
        for m in prior_records:
            if not m.attended:
                prior_absent_by_emp[m.employee_id] = prior_absent_by_emp.get(m.employee_id, 0) + 1

    return meeting_by_emp, prior_absent_by_emp


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


def _calc_school_wide_bonus(engine, emp, office_festival_base, total_enrollment, target):
    """計算全校比例節慶獎金（司機/美編/辦公室人員）。"""
    is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
    if is_eligible and target > 0:
        school_ratio = total_enrollment / target
        return round(office_festival_base * school_ratio)
    return 0


def _resolve_bonus_for_employee(engine, emp, emp_dict, emp_role_map, classroom_info_map,
                                total_school_enrollment, school_wide_overtime_target):
    """依員工角色與班級計算節慶獎金和超額獎金，回傳 classroom_context 或 None。"""
    is_office_staff = emp.is_office_staff or False
    classroom_context = None

    if emp.id in emp_role_map:
        roles = emp_role_map[emp.id]
        office_festival_base = engine.get_office_festival_bonus_base(emp.position or '', emp.title_name)

        if office_festival_base is not None:
            # 司機/美編
            emp_dict['_calculated_festival_bonus'] = _calc_school_wide_bonus(
                engine, emp, office_festival_base, total_school_enrollment, school_wide_overtime_target)
            emp_dict['_calculated_overtime_bonus'] = 0

        elif is_office_staff and len(roles) > 0:
            # 辦公室人員有帶班
            is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
            school_festival_bonus = 0
            total_overtime_bonus = 0
            if is_eligible:
                if school_wide_overtime_target > 0:
                    first_classroom_id, first_role = roles[0]
                    first_info = classroom_info_map.get(first_classroom_id)
                    if first_info:
                        role_for_bonus = first_role if first_role != 'art_teacher' else 'assistant_teacher'
                        bonus_base = engine.get_festival_bonus_base(emp.position or '', role_for_bonus)
                        school_festival_bonus = bonus_base * (total_school_enrollment / school_wide_overtime_target)
                for classroom_id, role in roles:
                    info = classroom_info_map.get(classroom_id)
                    if info:
                        result = engine.calculate_overtime_bonus(
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
            is_eligible = engine.is_eligible_for_festival_bonus(emp.hire_date)
            if is_eligible:
                for classroom_id, role in roles:
                    info = classroom_info_map.get(classroom_id)
                    if info:
                        result = engine.calculate_festival_bonus_v2(
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
        office_festival_base = engine.get_office_festival_bonus_base(emp.position or '', emp.title_name)
        if office_festival_base is not None:
            emp_dict['_calculated_festival_bonus'] = _calc_school_wide_bonus(
                engine, emp, office_festival_base, total_school_enrollment, school_wide_overtime_target)
            emp_dict['_calculated_overtime_bonus'] = 0
        elif is_office_staff:
            bonus_base = engine.get_festival_bonus_base(emp.position or '', 'assistant_teacher')
            emp_dict['_calculated_festival_bonus'] = _calc_school_wide_bonus(
                engine, emp, bonus_base, total_school_enrollment, school_wide_overtime_target)
            emp_dict['_calculated_overtime_bonus'] = 0

    return classroom_context


def _compute_salary_breakdown(engine, emp, emp_dict, year, month, emp_allowances,
                              classroom_context, meeting_context, global_bonus_settings):
    """呼叫 salary_engine 計算薪資明細。"""
    if '_calculated_festival_bonus' in emp_dict:
        breakdown = engine.calculate_salary(
            emp_dict, year, month,
            bonus_settings=None, allowances=emp_allowances,
            classroom_context=None, meeting_context=meeting_context)
        breakdown.festival_bonus = emp_dict['_calculated_festival_bonus']
        breakdown.overtime_bonus = emp_dict.get('_calculated_overtime_bonus', 0)
        # 非發放月份不計節慶獎金（季度合併發放：2月、6月、9月、12月）
        if not engine.get_bonus_distribution_month(month):
            breakdown.festival_bonus = 0
        breakdown.supervisor_dividend = engine.get_supervisor_dividend(emp.title_name, emp.position or '')
        # 時薪制：gross_salary 已由 calculate_salary() 正確設為 hourly_total，不可覆蓋
        if emp_dict.get('employee_type') != 'hourly':
            breakdown.gross_salary = (
                breakdown.base_salary + breakdown.supervisor_allowance +
                breakdown.teacher_allowance + breakdown.meal_allowance +
                breakdown.transportation_allowance + breakdown.other_allowance +
                breakdown.performance_bonus + breakdown.special_bonus +
                breakdown.supervisor_dividend + breakdown.meeting_overtime_pay +
                breakdown.birthday_bonus)
        breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction
    elif classroom_context:
        breakdown = engine.calculate_salary(
            emp_dict, year, month,
            bonus_settings=None, allowances=emp_allowances,
            classroom_context=classroom_context, meeting_context=meeting_context)
    else:
        breakdown = engine.calculate_salary(
            emp_dict, year, month,
            bonus_settings=global_bonus_settings, allowances=emp_allowances,
            meeting_context=meeting_context)
    return breakdown


@router.post("/salary/calculate")
async def calculate_salaries(request: CalculateSalaryRequest, current_user: dict = Depends(require_permission(Permission.SALARY_WRITE))):
    """一鍵結算薪資"""
    from services.salary_engine import SalaryEngine
    session = get_session()
    try:
        # 建立本次批次專用的引擎實例，與全域 singleton 完全隔離，
        # 確保：① 批次中途設定變更不影響本次計算；② bonus_config 覆蓋不污染其他並發請求
        engine = SalaryEngine(load_from_db=True)

        if request.bonus_config:
            bonus_config_dict = request.bonus_config.dict() if hasattr(request.bonus_config, 'dict') else request.bonus_config
            engine.set_bonus_config(bonus_config_dict)

        allowance_map = _build_allowance_map(session)

        enrollment_map = {}
        if request.class_enrollments:
            for ce in request.class_enrollments:
                enrollment_map[ce.classroom_id] = ce.current_enrollment

        classroom_info_map, emp_role_map = _build_classroom_info(session, enrollment_map)
        total_school_enrollment = sum(info['current_enrollment'] for info in classroom_info_map.values())
        meeting_by_emp, prior_absent_by_emp = _build_meeting_map(session, request.year, request.month)
        global_bonus_settings = _build_legacy_bonus_settings(request)

        _, _last_day = _cal.monthrange(request.year, request.month)
        _month_start = date(request.year, request.month, 1)
        _month_end = date(request.year, request.month, _last_day)

        # 包含在職員工，以及當月才離職的員工（保留最後一個月薪資結算）
        employees = session.query(Employee).filter(
            or_(
                Employee.is_active == True,
                and_(
                    Employee.is_active == False,
                    Employee.resign_date >= _month_start,
                    Employee.resign_date <= _month_end,
                ),
            )
        ).all()

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
                "birthday": emp.birthday.isoformat() if emp.birthday else None,
                "is_office_staff": emp.is_office_staff or False,
            }

            # 時薪制：從考勤記錄計算當月實際工時，以免 hourly_total 為 0
            if emp.employee_type == 'hourly':
                att_records = session.query(Attendance).filter(
                    Attendance.employee_id == emp.id,
                    Attendance.attendance_date >= _month_start,
                    Attendance.attendance_date <= _month_end,
                ).all()
                _work_end_t = datetime.strptime(emp.work_end_time or "17:00", "%H:%M").time()
                total_hours = 0.0
                for a in att_records:
                    if not a.punch_in_time:
                        continue
                    if a.punch_out_time:
                        effective_out = a.punch_out_time
                    else:
                        # 缺下班打卡：以排班下班時間代入，避免員工工時歸零
                        effective_out = datetime.combine(a.punch_in_time.date(), _work_end_t)
                        if effective_out <= a.punch_in_time:
                            continue
                    diff = (effective_out - a.punch_in_time).total_seconds() / 3600
                    # 扣除午休（12:00–13:00），若工時跨越此區間則扣除重疊時數
                    _d = a.punch_in_time.date()
                    _lunch_s = datetime.combine(_d, time(12, 0))
                    _lunch_e = datetime.combine(_d, time(13, 0))
                    _overlap = max(0.0, (min(effective_out, _lunch_e) - max(a.punch_in_time, _lunch_s)).total_seconds() / 3600)
                    diff -= _overlap
                    # 每日工時上限，防止打卡資料異常（手動修改）導致薪資灌水
                    total_hours += min(diff, MAX_DAILY_WORK_HOURS)
                emp_dict['work_hours'] = round(total_hours, 2)

            classroom_context = _resolve_bonus_for_employee(
                engine, emp, emp_dict, emp_role_map, classroom_info_map,
                total_school_enrollment, request.school_wide_overtime_target)

            meeting_records = meeting_by_emp.get(emp.id, [])
            _absent_current = sum(1 for m in meeting_records if not m.attended)
            _absent_period = _absent_current + prior_absent_by_emp.get(emp.id, 0)
            meeting_context = None
            if meeting_records or _absent_period > 0:
                meeting_context = {
                    'attended': sum(1 for m in meeting_records if m.attended),
                    'absent': _absent_current,
                    'absent_period': _absent_period,
                    'work_end_time': emp.work_end_time or '17:00',
                }

            breakdown = _compute_salary_breakdown(
                engine, emp, emp_dict, request.year, request.month,
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
        # ── 封存前置檢查：只要該月有任何已封存薪資，整批拒絕 ──────────────────
        # 理由：部分封存 + 部分重算 會讓帳冊出現新舊混合的狀態，更難稽核。
        # 應讓管理員先到薪資頁面確認並解除整月封存後，再執行重算。
        finalized_records = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .filter(
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
                SalaryRecord.is_finalized == True,
            )
            .all()
        )
        if finalized_records:
            names = "、".join(r.name for _, r in finalized_records)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{year} 年 {month} 月已有 {len(finalized_records)} 筆薪資封存，"
                    f"無法整批重算（{names}）。"
                    "請先至薪資管理頁面解除整月封存後再重試。"
                ),
            )
        # ─────────────────────────────────────────────────────────────────────────

        from services.salary_engine import SalaryEngine as Engine
        engine = Engine(load_from_db=True)

        # 1. 包含在職員工，以及當月才離職的員工（保留最後一個月薪資結算）
        _, _alt_last = _cal.monthrange(year, month)
        _alt_start = date(year, month, 1)
        _alt_end = date(year, month, _alt_last)
        employees = session.query(Employee).filter(
            or_(
                Employee.is_active == True,
                and_(
                    Employee.is_active == False,
                    Employee.resign_date >= _alt_start,
                    Employee.resign_date <= _alt_end,
                ),
            )
        ).all()

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
                    "absence_deduction": salary_record.absence_deduction or 0,
                    "attendance_deduction": (salary_record.late_deduction or 0) + (salary_record.early_leave_deduction or 0) + (salary_record.missing_punch_deduction or 0),
                    "meeting_overtime_pay": salary_record.meeting_overtime_pay or 0,
                    "meeting_absence_deduction": salary_record.meeting_absence_deduction or 0,
                    "birthday_bonus": salary_record.birthday_bonus or 0,
                    "pension_self": salary_record.pension_self or 0,
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

        # 包含在職員工，以及當月才離職的員工（保留節慶獎金結算）
        _, _fb_last = _cal.monthrange(year, month)
        _fb_start = date(year, month, 1)
        _fb_end = date(year, month, _fb_last)
        employees = session.query(Employee).filter(
            or_(
                Employee.is_active == True,
                and_(
                    Employee.is_active == False,
                    Employee.resign_date >= _fb_start,
                    Employee.resign_date <= _fb_end,
                ),
            )
        ).all()
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
                "birthday_bonus": record.birthday_bonus or 0,
                "performance_bonus": record.performance_bonus,
                "special_bonus": record.special_bonus,
                "supervisor_dividend": record.bonus_amount or 0,
                "labor_insurance": record.labor_insurance_employee,
                "health_insurance": record.health_insurance_employee,
                "pension": record.pension_employee,
                "late_deduction": record.late_deduction,
                "early_leave_deduction": record.early_leave_deduction,
                "missing_punch_deduction": record.missing_punch_deduction,
                "absence_deduction": record.absence_deduction or 0,
                "attendance_deduction": (record.late_deduction or 0) + (record.early_leave_deduction or 0) + (record.missing_punch_deduction or 0),
                "leave_deduction": record.leave_deduction,
                "other_deduction": record.other_deduction,
                "gross_salary": record.gross_salary,
                "total_deduction": record.total_deduction,
                "net_salary": record.net_salary,
                "is_finalized": record.is_finalized,
                "finalized_at": record.finalized_at.isoformat() if record.finalized_at else None,
                "finalized_by": record.finalized_by,
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


# ============ 薪資封存管理 ============

class FinalizeMonthRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)


@router.post("/salaries/finalize-month")
def finalize_salary_month(
    data: FinalizeMonthRequest,
    current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)),
):
    """封存整月薪資（封存後禁止重新計算，需手動解封才能修改）"""
    session = get_session()
    try:
        records = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.salary_year == data.year,
                SalaryRecord.salary_month == data.month,
                SalaryRecord.is_finalized != True,
            )
            .all()
        )
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"{data.year} 年 {data.month} 月無可封存的薪資記錄（可能尚未計算，或全部已封存）",
            )
        now = datetime.now()
        operator = current_user.get("username") or current_user.get("name") or "管理員"
        for r in records:
            r.is_finalized = True
            r.finalized_at = now
            r.finalized_by = operator
        session.commit()
        logger.info(
            "整月薪資封存：%d/%d，共 %d 筆，操作者=%s",
            data.year, data.month, len(records), operator,
        )
        return {
            "message": f"已封存 {data.year} 年 {data.month} 月共 {len(records)} 筆薪資記錄",
            "count": len(records),
            "finalized_by": operator,
            "finalized_at": now.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/salaries/{record_id}/finalize")
def unfinalize_salary(
    record_id: int,
    current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)),
):
    """解除單筆薪資封存（危險操作，會記錄稽核備註）"""
    session = get_session()
    try:
        record = session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="薪資記錄不存在")
        if not record.is_finalized:
            raise HTTPException(status_code=409, detail="此筆薪資尚未封存，無需解封")
        operator = current_user.get("username") or current_user.get("name") or "管理員"
        logger.warning(
            "薪資封存解除！record_id=%d，employee_id=%d，%d/%d，操作者=%s",
            record_id, record.employee_id, record.salary_year, record.salary_month, operator,
        )
        record.is_finalized = False
        audit_note = f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 封存解除，操作者：{operator}"
        record.remark = (record.remark or "") + audit_note
        session.commit()
        return {"message": "已解除封存，操作記錄已寫入備註欄位"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
