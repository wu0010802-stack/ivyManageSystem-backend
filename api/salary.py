"""
Salary calculation and management router
"""

import io
import logging
from datetime import date, datetime, time
from typing import Dict, List, Optional

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from utils.auth import require_permission
from utils.error_messages import SALARY_RECORD_NOT_FOUND
from utils.permissions import Permission
from utils.rate_limit import SlidingWindowLimiter

# 薪資計算為高 CPU 操作，每 IP 每小時最多 20 次（批次計算）
_salary_calc_limiter = SlidingWindowLimiter(
    max_calls=20,
    window_seconds=3600,
    name="salary_calculate",
    error_detail="薪資計算操作過於頻繁，請稍後再試",
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from sqlalchemy import and_, or_
from sqlalchemy.orm import joinedload
import calendar as _cal

from models.database import (
    get_session, Employee, Classroom, ClassGrade, Student,
    SalaryRecord, EmployeeAllowance, AllowanceType, Attendance, OvertimeRecord,
)
from services.salary_engine import _compute_hourly_daily_hours
from services.salary.utils import get_meeting_deduction_period_start, calc_daily_salary
from services.salary.engine import SalaryEngine as RuntimeSalaryEngine
from services.salary_field_breakdown import FIELD_LABELS, build_field_breakdown, build_salary_debug_snapshot
from services.student_enrollment import classroom_student_count_map, count_students_active_on
from api.salary_fields import calculate_display_bonus_total, calculate_total_allowances

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["salary"])

# 津貼設定很少變更，快取 10 分鐘避免薪資重算時重複查詢
_allowance_cache: TTLCache = TTLCache(maxsize=1, ttl=600)


# ============ Service Init ============

_salary_engine = None
_insurance_service = None
_line_service = None


def init_salary_services(salary_engine, insurance_service, line_service=None):
    global _salary_engine, _insurance_service, _line_service
    _salary_engine = salary_engine
    _insurance_service = insurance_service
    if line_service is not None:
        _line_service = line_service


# ============ Pydantic Models ============

class SalarySimulateOverride(BaseModel):
    """薪資試算覆蓋參數（None = 使用 DB 實際資料）"""
    late_count: Optional[int] = Field(None, ge=0)
    early_leave_count: Optional[int] = Field(None, ge=0)
    missing_punch_count: Optional[int] = Field(None, ge=0)
    total_late_minutes: Optional[float] = Field(None, ge=0)
    total_early_minutes: Optional[float] = Field(None, ge=0)
    work_days: Optional[int] = Field(None, ge=0, le=31)
    extra_personal_leave_hours: float = Field(0, ge=0)
    extra_sick_leave_hours: float = Field(0, ge=0)
    enrollment_override: Optional[int] = Field(None, ge=0)
    extra_overtime_pay: float = Field(0, ge=0)


class SalarySimulateRequest(BaseModel):
    employee_id: int = Field(..., ge=1)
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    overrides: SalarySimulateOverride = SalarySimulateOverride()


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
    # 全校比例獎金用的目標人數
    school_wide_overtime_target: int = Field(0, ge=0)


# ============ Routes ============

def _build_allowance_map(session):
    """預先抓取所有員工的津貼設定，依 employee_id 分組（快取 10 分鐘）。"""
    cached = _allowance_cache.get("allowance_map")
    if cached is not None:
        return cached
    all_allowances = session.query(EmployeeAllowance, AllowanceType).join(AllowanceType).filter(
        EmployeeAllowance.is_active == True
    ).all()
    allowance_map = {}
    for ea, at in all_allowances:
        allowance_map.setdefault(ea.employee_id, []).append({
            "name": at.name, "amount": ea.amount, "code": at.code
        })
    _allowance_cache["allowance_map"] = allowance_map
    return allowance_map


def _build_classroom_info(session, enrollment_map, year: int, month: int):
    """建立班級詳細資訊對照表與員工角色對照表。"""
    classrooms = session.query(Classroom).options(joinedload(Classroom.grade)).filter(Classroom.is_active == True).all()
    grade_map = {c.grade_id: c.grade.name for c in classrooms if c.grade_id and c.grade}
    _, last_day = _cal.monthrange(year, month)
    month_end = date(year, month, last_day)

    # 批次查詢所有班級學生數，避免 N+1
    db_count_map = classroom_student_count_map(session, month_end)

    classroom_info_map = {}
    for c in classrooms:
        # enrollment_map 優先（使用者本次提交的覆寫），其次用 DB 統計，找不到則 0
        student_count = enrollment_map.get(c.id, db_count_map.get(c.id, 0))
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
    period_start = get_meeting_deduction_period_start(year, month)
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


def _safe_divide(a: float, b: float, default: float = 0.0) -> float:
    """安全除法：分母為 0 時回傳 default，避免 ZeroDivisionError。"""
    return a / b if b else default


def _calc_school_wide_bonus(engine, emp, office_festival_base, total_enrollment, target):
    """計算全校比例節慶獎金（主管或特定非帶班職位）。"""
    if not engine.is_eligible_for_festival_bonus(emp.hire_date):
        return 0
    return round(office_festival_base * _safe_divide(total_enrollment, target))


def _resolve_bonus_for_employee(engine, emp, emp_dict, emp_role_map, classroom_info_map,
                                total_school_enrollment, school_wide_overtime_target):
    """依員工角色與班級計算節慶獎金和超額獎金，回傳 classroom_context 或 None。"""
    classroom_context = None

    if emp.id in emp_role_map:
        roles = emp_role_map[emp.id]
        office_festival_base = engine.get_office_festival_bonus_base(emp.position or '', emp.title_name)

        if office_festival_base is not None:
            # 司機/美編
            emp_dict['_calculated_festival_bonus'] = _calc_school_wide_bonus(
                engine, emp, office_festival_base, total_school_enrollment, school_wide_overtime_target)
            emp_dict['_calculated_overtime_bonus'] = 0

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

    return classroom_context


def _apply_precalculated_bonus(engine, emp, emp_dict, breakdown, month) -> None:
    """預算獎金路徑的後處理：將事先計算好的節慶獎金/加班獎金寫回 breakdown，並修正 gross_salary。

    僅在 emp_dict 含 '_calculated_festival_bonus' 時呼叫。
    """
    breakdown.festival_bonus = emp_dict['_calculated_festival_bonus']
    breakdown.overtime_bonus = emp_dict.get('_calculated_overtime_bonus', 0)
    # 非發放月份不計節慶獎金（季度合併發放：2月、6月、9月、12月）
    if not engine.get_bonus_distribution_month(month):
        breakdown.festival_bonus = 0
    breakdown.supervisor_dividend = engine.get_supervisor_dividend(
        emp.title_name, emp.position or '', emp.supervisor_role or ''
    )
    # 時薪制：gross_salary 已由 calculate_salary() 正確設為 hourly_total，不可覆蓋
    if emp_dict.get('employee_type') != 'hourly':
        breakdown.gross_salary = (
            breakdown.base_salary + breakdown.supervisor_allowance +
            breakdown.teacher_allowance + breakdown.meal_allowance +
            breakdown.transportation_allowance + breakdown.other_allowance +
            breakdown.performance_bonus + breakdown.special_bonus +
            breakdown.supervisor_dividend + breakdown.meeting_overtime_pay +
            breakdown.birthday_bonus + breakdown.overtime_work_pay)
    breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction


def _compute_salary_breakdown(engine, emp, emp_dict, year, month, emp_allowances,
                              classroom_context, meeting_context, global_bonus_settings,
                              overtime_work_pay: float = 0):
    """呼叫 salary_engine 計算薪資明細，依獎金路徑分三條分支。"""
    use_precalculated = '_calculated_festival_bonus' in emp_dict
    effective_classroom = None if use_precalculated else classroom_context
    effective_bonus_settings = None if (use_precalculated or classroom_context) else global_bonus_settings

    breakdown = engine.calculate_salary(
        emp_dict, year, month,
        bonus_settings=effective_bonus_settings,
        allowances=emp_allowances,
        classroom_context=effective_classroom,
        meeting_context=meeting_context,
        overtime_work_pay=overtime_work_pay)

    if use_precalculated:
        _apply_precalculated_bonus(engine, emp, emp_dict, breakdown, month)
    return breakdown


@router.post("/salary/calculate")
async def calculate_salaries(request: CalculateSalaryRequest, current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)), _rl: None = Depends(_salary_calc_limiter.as_dependency())):
    """一鍵結算薪資"""
    from services.salary_engine import SalaryEngine
    session = get_session()
    try:
        # 建立本次批次專用的引擎實例，與全域 singleton 完全隔離，
        # 確保：① 批次中途設定變更不影響本次計算；② bonus_config 覆蓋不污染其他並發請求
        engine = SalaryEngine(load_from_db=True)

        if request.bonus_config:
            bonus_config_dict = request.bonus_config.model_dump() if hasattr(request.bonus_config, 'model_dump') else request.bonus_config
            engine.set_bonus_config(bonus_config_dict)

        allowance_map = _build_allowance_map(session)

        enrollment_map = {}
        if request.class_enrollments:
            for ce in request.class_enrollments:
                enrollment_map[ce.classroom_id] = ce.current_enrollment

        classroom_info_map, emp_role_map = _build_classroom_info(session, enrollment_map, request.year, request.month)
        total_school_enrollment = sum(info['current_enrollment'] for info in classroom_info_map.values())
        meeting_by_emp, prior_absent_by_emp = _build_meeting_map(session, request.year, request.month)
        global_bonus_settings = _build_legacy_bonus_settings(request)

        _, _last_day = _cal.monthrange(request.year, request.month)
        _month_start = date(request.year, request.month, 1)
        _month_end = date(request.year, request.month, _last_day)

        # 批次查詢當月已核准加班記錄，依 employee_id 分組（避免迴圈內 N+1）
        _ot_records = session.query(OvertimeRecord).filter(
            OvertimeRecord.is_approved == True,
            OvertimeRecord.overtime_date >= _month_start,
            OvertimeRecord.overtime_date <= _month_end,
        ).all()
        _overtime_pay_map: dict = {}
        for _ot in _ot_records:
            _overtime_pay_map[_ot.employee_id] = (
                _overtime_pay_map.get(_ot.employee_id, 0) + (_ot.overtime_pay or 0)
            )

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

        # 批次查詢時薪員工當月出勤，避免迴圈內 N+1（每個時薪員工一條 SQL → 一次批次查詢）
        _hourly_emp_ids = [e.id for e in employees if e.employee_type == 'hourly']
        _hourly_att_map: dict = {}
        if _hourly_emp_ids:
            _hourly_att_records = session.query(Attendance).filter(
                Attendance.employee_id.in_(_hourly_emp_ids),
                Attendance.attendance_date >= _month_start,
                Attendance.attendance_date <= _month_end,
            ).all()
            for _a in _hourly_att_records:
                _hourly_att_map.setdefault(_a.employee_id, []).append(_a)

        # 預建薪資→投保金額快取，避免迴圈內重複線性掃描 insurance_table
        _unique_salary_levels = {
            (emp.insurance_salary_level or emp.base_salary)
            for emp in employees
            if (emp.insurance_salary_level or emp.base_salary)
        }
        _bracket_cache = {
            sal: engine.insurance_service.get_bracket(sal)["amount"]
            for sal in _unique_salary_levels
        }

        results = []
        for emp in employees:
            _ins_base = emp.insurance_salary_level or emp.base_salary
            emp_dict = {
                "name": emp.name, "employee_id": emp.employee_id,
                "employee_type": emp.employee_type, "position": emp.position,
                "supervisor_role": emp.supervisor_role,
                "title": emp.title, "base_salary": emp.base_salary,
                "hourly_rate": emp.hourly_rate,
                "supervisor_allowance": emp.supervisor_allowance,
                "teacher_allowance": emp.teacher_allowance,
                "meal_allowance": emp.meal_allowance,
                "transportation_allowance": emp.transportation_allowance,
                "insurance_salary": _bracket_cache.get(_ins_base, 0) if _ins_base else 0,
                "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
                "birthday": emp.birthday.isoformat() if emp.birthday else None,
            }

            # 時薪制：從批次查詢結果取得當月出勤，計算實際工時
            if emp.employee_type == 'hourly':
                att_records = _hourly_att_map.get(emp.id, [])
                _work_end_t = datetime.strptime(emp.work_end_time or "17:00", "%H:%M").time()
                total_hours = 0.0
                for a in att_records:
                    if not a.punch_in_time:
                        continue
                    total_hours += _compute_hourly_daily_hours(
                        a.punch_in_time, a.punch_out_time, _work_end_t
                    )
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
                meeting_context, global_bonus_settings,
                overtime_work_pay=_overtime_pay_map.get(emp.id, 0))

            results.append(breakdown.__dict__)

        return {"message": "薪資結算完成", "results": results}

    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="薪資批量計算失敗")
    finally:
        session.close()


@router.post("/salaries/calculate")
def calculate_salaries_alt(
    current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)),
    year: int = Query(..., ge=2000, le=2100, description="Calculate for which year"),
    month: int = Query(..., ge=1, le=12, description="Calculate for which month"),
    _rl: None = Depends(_salary_calc_limiter.as_dependency()),
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

        from services.salary.engine import SalaryEngine as Engine
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
        employee_ids = [emp.id for emp in employees]
        emp_name_map = {emp.id: emp.name for emp in employees}

    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e)
    finally:
        session.close()

    # 2. 批次計算（process_bulk_salary_calculation 自行管理 session）
    try:
        bulk_results, errors = engine.process_bulk_salary_calculation(employee_ids, year, month)
    except Exception as e:
        raise_safe_500(e)

    results = []
    for emp, breakdown in bulk_results:
        results.append({
            "employee_id": emp.id,
            "employee_name": emp.name,
            "base_salary": breakdown.base_salary,
            "total_allowances": calculate_total_allowances(breakdown),
            "festival_bonus": breakdown.festival_bonus,
            "overtime_bonus": breakdown.overtime_bonus,
            "overtime_pay": breakdown.overtime_work_pay,
            "supervisor_dividend": breakdown.supervisor_dividend,
            "labor_insurance": breakdown.labor_insurance,
            "health_insurance": breakdown.health_insurance,
            "late_deduction": breakdown.late_deduction,
            "early_leave_deduction": breakdown.early_leave_deduction,
            "missing_punch_deduction": breakdown.missing_punch_deduction,
            "leave_deduction": breakdown.leave_deduction,
            "absence_deduction": breakdown.absence_deduction or 0,
            "attendance_deduction": (
                (breakdown.late_deduction or 0)
                + (breakdown.early_leave_deduction or 0)
                + (breakdown.missing_punch_deduction or 0)
            ),
            "meeting_overtime_pay": breakdown.meeting_overtime_pay or 0,
            "meeting_absence_deduction": breakdown.meeting_absence_deduction or 0,
            "birthday_bonus": breakdown.birthday_bonus or 0,
            "pension_self": breakdown.pension_self or 0,
            "total_deductions": breakdown.total_deduction,
            "net_pay": breakdown.net_salary,
        })

    # LINE 群組推播（薪資批次計算完成）
    if _line_service is not None and results:
        try:
            total_net = sum(r["net_pay"] for r in results)
            _line_service.notify_salary_batch_complete(year, month, len(results), int(total_net))
        except Exception as _le:
            logger.warning("薪資批次計算 LINE 推播失敗: %s", _le)

    return {"results": results, "errors": errors}


@router.get("/salaries/festival-bonus")
def get_festival_bonus(
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12)
):
    """
    Return breakdown of festival bonus calculation
    """
    session = get_session()
    try:
        # 使用啟動時已載入設定的 singleton，避免每次請求重跑 4 次 DB 查詢
        engine = _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=True)

        _, _fb_last = _cal.monthrange(year, month)
        _fb_start = date(year, month, 1)
        _fb_end = date(year, month, _fb_last)
        month_end = _fb_end

        # 批次預先查詢共用資料，避免 N+1
        school_active = count_students_active_on(session, month_end)
        cls_count_map = classroom_student_count_map(session, month_end)
        classroom_map = {
            c.id: c
            for c in session.query(Classroom).options(joinedload(Classroom.grade)).all()
        }

        # 包含在職員工，以及當月才離職的員工（保留節慶獎金結算）
        employees = session.query(Employee).options(
            joinedload(Employee.job_title_rel)
        ).filter(
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
            ctx = {
                "session": session,
                "employee": emp,
                "classroom": classroom_map.get(emp.classroom_id) if emp.classroom_id else None,
                "school_active_students": school_active,
                "classroom_count_map": cls_count_map,
            }
            bonus_data = engine.calculate_festival_bonus_breakdown(emp.id, year, month, _ctx=ctx)
            results.append(bonus_data)

        return results

    except Exception as e:
        logger.error(f"Error getting festival bonus: {e}")
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/salaries/records")
def get_salary_records(
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
):
    """查詢某月薪資記錄（支援分頁，預設一次最多 500 筆）"""
    session = get_session()
    try:
        role = current_user.get("role", "")
        FULL_SALARY_ROLES = {"admin", "hr"}
        viewer_employee_id = None if role in FULL_SALARY_ROLES else current_user.get("employee_id")
        if viewer_employee_id is None and role not in FULL_SALARY_ROLES:
            raise HTTPException(status_code=403, detail="無法識別員工身分，禁止查詢薪資")

        query = session.query(SalaryRecord, Employee).join(
            Employee, SalaryRecord.employee_id == Employee.id
        ).options(
            joinedload(Employee.job_title_rel)
        ).filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month
        )
        if viewer_employee_id is not None:
            query = query.filter(SalaryRecord.employee_id == viewer_employee_id)
        records = query.order_by(Employee.name).offset(skip).limit(limit).all()

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
                "supervisor_dividend": record.supervisor_dividend or 0,
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
                "remark": record.remark,
                "calculated_at": record.updated_at.isoformat() if record.updated_at else None,
                # 前端 salaryResults 使用的欄位別名（供頁面重整後重建計算結果列表）
                "total_allowances": calculate_total_allowances(record),
                "pension_self": record.pension_employee or 0,
                "total_deductions": record.total_deduction or 0,
                "net_pay": record.net_salary or 0,
            })

        return results
    finally:
        session.close()


@router.put("/salaries/{record_id}/manual-adjust")
def manual_adjust_salary(
    record_id: int,
    data: SalaryManualAdjustRequest,
    current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)),
):
    """手動調整單筆薪資記錄。"""
    session = get_session()
    try:
        record = session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        if not record:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
        if record.is_finalized:
            raise HTTPException(status_code=409, detail="此筆薪資已封存，請先解除封存再編輯")

        payload = data.model_dump(exclude_unset=True)
        if not payload:
            raise HTTPException(status_code=400, detail="至少需要提供一個調整欄位")

        changed_parts = []
        for field, value in payload.items():
            if field not in EDITABLE_SALARY_FIELDS:
                continue
            old_value = round(getattr(record, field) or 0)
            new_value = round(value or 0)
            if old_value == new_value:
                continue
            setattr(record, field, new_value)
            changed_parts.append(f"{EDITABLE_SALARY_FIELDS[field]} {old_value}→{new_value}")

        if not changed_parts:
            raise HTTPException(status_code=400, detail="沒有實際變更")

        _recalculate_salary_record_totals(record)

        if (record.net_salary or 0) < 0:
            raise HTTPException(
                status_code=400,
                detail=f"調整後淨薪資為負數（{record.net_salary} 元），請確認扣款設定是否正確",
            )

        operator = current_user.get("username") or current_user.get("name") or "管理員"
        audit_note = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 手動編輯："
            + "；".join(changed_parts)
            + f"；操作者：{operator}"
        )
        record.remark = f"{(record.remark or '').strip()}\n{audit_note}".strip()
        session.commit()

        logger.warning(
            "手動調整薪資：record_id=%s employee_id=%s fields=%s operator=%s",
            record.id,
            record.employee_id,
            ",".join(payload.keys()),
            operator,
        )

        return {
            "message": "薪資金額已更新",
            "record": {
                "id": record.id,
                "festival_bonus": record.festival_bonus or 0,
                "overtime_bonus": record.overtime_bonus or 0,
                "overtime_pay": record.overtime_pay or 0,
                "supervisor_dividend": record.supervisor_dividend or 0,
                "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                "birthday_bonus": record.birthday_bonus or 0,
                "leave_deduction": record.leave_deduction or 0,
                "late_deduction": record.late_deduction or 0,
                "early_leave_deduction": record.early_leave_deduction or 0,
                "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                "absence_deduction": record.absence_deduction or 0,
                "gross_salary": record.gross_salary or 0,
                "total_deduction": record.total_deduction or 0,
                "net_salary": record.net_salary or 0,
                "bonus_amount": record.bonus_amount or 0,
                "bonus_separate": bool(record.bonus_separate),
                "remark": record.remark,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/salaries/{record_id}/breakdown")
def get_salary_breakdown(
    record_id: int,
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資明細。"""
    session = get_session()
    try:
        record, emp = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .options(joinedload(Employee.job_title_rel))
            .filter(SalaryRecord.id == record_id)
            .first()
            or (None, None)
        )
        if not record or not emp:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        job_title = ""
        if emp.job_title_rel:
            job_title = emp.job_title_rel.name
        elif emp.title:
            job_title = emp.title

        return {
            "employee": {
                "record_id": record.id,
                "employee_name": emp.name,
                "employee_code": emp.employee_id,
                "job_title": job_title,
                "year": record.salary_year,
                "month": record.salary_month,
            },
            "earnings": {
                "base_salary": record.base_salary or 0,
                "total_allowances": calculate_total_allowances(record),
                "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                "overtime_pay": record.overtime_pay or 0,
                "gross_salary": record.gross_salary or 0,
            },
            "bonuses": {
                "festival_bonus": record.festival_bonus or 0,
                "overtime_bonus": record.overtime_bonus or 0,
                "supervisor_dividend": record.supervisor_dividend or 0,
                "birthday_bonus": record.birthday_bonus or 0,
            },
            "deductions": {
                "leave_deduction": record.leave_deduction or 0,
                "late_deduction": record.late_deduction or 0,
                "early_leave_deduction": record.early_leave_deduction or 0,
                "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                "absence_deduction": record.absence_deduction or 0,
                "labor_insurance": record.labor_insurance_employee or 0,
                "health_insurance": record.health_insurance_employee or 0,
                "pension": record.pension_employee or 0,
                "total_deduction": record.total_deduction or 0,
            },
            "summary": {
                "net_salary": record.net_salary or 0,
                "bonus_separate": bool(record.bonus_separate),
                "bonus_amount": record.bonus_amount or 0,
            },
        }
    finally:
        session.close()


@router.get("/salaries/{record_id}/field-breakdown")
def get_salary_field_breakdown(
    record_id: int,
    field: str = Query(...),
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資指定欄位明細。"""
    if field not in FIELD_LABELS:
        raise HTTPException(status_code=400, detail="不支援的明細欄位")

    session = get_session()
    try:
        record, emp = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .options(joinedload(Employee.job_title_rel))
            .filter(SalaryRecord.id == record_id)
            .first()
            or (None, None)
        )
        if not record or not emp:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        engine = _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=False)
        snapshot = build_salary_debug_snapshot(session, engine, emp, record.salary_year, record.salary_month)
        return build_field_breakdown(record, emp, snapshot, field)
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
        result = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .filter(SalaryRecord.id == record_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
        record, emp = result

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
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    format: str = Query("xlsx", pattern="^(xlsx|pdf)$")
):
    """匯出全部員工薪資（xlsx 或 pdf）"""
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

        if format == "pdf":
            from services.salary_slip import generate_salary_all_pdf
            pdf_bytes = generate_salary_all_pdf(records, year, month)
            filename = f"salary_all_{year}_{month:02d}.pdf"
            return StreamingResponse(
                io.BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
            )

        from services.salary_slip import generate_salary_excel
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
            total_allowances = calculate_total_allowances(r)
            total_bonus = calculate_display_bonus_total(r)
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
    year: int = Query(..., ge=2000, le=2100),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """查詢全部員工年度薪資概覽（支援分頁，依員工分組）"""
    session = get_session()
    try:
        # 先取得符合條件的員工 id 清單（分頁依員工為單位）
        emp_subq = (
            session.query(Employee.id, Employee.name)
            .join(SalaryRecord, SalaryRecord.employee_id == Employee.id)
            .filter(SalaryRecord.salary_year == year)
            .group_by(Employee.id, Employee.name)
            .order_by(Employee.name)
        )
        total = emp_subq.count()
        emp_page = emp_subq.offset(skip).limit(limit).all()
        emp_ids = [e.id for e in emp_page]
        emp_name_map = {e.id: e.name for e in emp_page}

        if not emp_ids:
            return {"items": [], "total": total, "skip": skip, "limit": limit}

        records = session.query(SalaryRecord).filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.employee_id.in_(emp_ids),
        ).order_by(SalaryRecord.salary_month).all()

        grouped: dict = {eid: [] for eid in emp_ids}
        for r in records:
            grouped[r.employee_id].append({
                "month": r.salary_month,
                "net_salary": r.net_salary,
                "gross_salary": r.gross_salary,
            })

        results = [
            {
                "employee_id": eid,
                "employee_name": emp_name_map[eid],
                "months": grouped[eid],
            }
            for eid in emp_ids
        ]
        return {"items": results, "total": total, "skip": skip, "limit": limit}
    finally:
        session.close()


# ============ 薪資封存管理 ============

class FinalizeMonthRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)


class SalaryManualAdjustRequest(BaseModel):
    base_salary: Optional[float] = Field(None, ge=0)
    supervisor_allowance: Optional[float] = Field(None, ge=0)
    teacher_allowance: Optional[float] = Field(None, ge=0)
    meal_allowance: Optional[float] = Field(None, ge=0)
    transportation_allowance: Optional[float] = Field(None, ge=0)
    other_allowance: Optional[float] = Field(None, ge=0)
    performance_bonus: Optional[float] = Field(None, ge=0)
    special_bonus: Optional[float] = Field(None, ge=0)
    festival_bonus: Optional[float] = Field(None, ge=0)
    overtime_bonus: Optional[float] = Field(None, ge=0)
    overtime_pay: Optional[float] = Field(None, ge=0)
    supervisor_dividend: Optional[float] = Field(None, ge=0)
    meeting_overtime_pay: Optional[float] = Field(None, ge=0)
    birthday_bonus: Optional[float] = Field(None, ge=0)
    labor_insurance_employee: Optional[float] = Field(None, ge=0)
    health_insurance_employee: Optional[float] = Field(None, ge=0)
    pension_employee: Optional[float] = Field(None, ge=0)
    leave_deduction: Optional[float] = Field(None, ge=0)
    late_deduction: Optional[float] = Field(None, ge=0)
    early_leave_deduction: Optional[float] = Field(None, ge=0)
    missing_punch_deduction: Optional[float] = Field(None, ge=0)
    meeting_absence_deduction: Optional[float] = Field(None, ge=0)
    absence_deduction: Optional[float] = Field(None, ge=0)
    other_deduction: Optional[float] = Field(None, ge=0)


EDITABLE_SALARY_FIELDS = {
    "base_salary": "底薪",
    "supervisor_allowance": "主管加給",
    "teacher_allowance": "導師津貼",
    "meal_allowance": "伙食津貼",
    "transportation_allowance": "交通津貼",
    "other_allowance": "其他津貼",
    "performance_bonus": "績效獎金",
    "special_bonus": "特別獎金",
    "festival_bonus": "節慶獎金",
    "overtime_bonus": "超額獎金",
    "overtime_pay": "加班津貼",
    "supervisor_dividend": "主管紅利",
    "meeting_overtime_pay": "會議加班",
    "birthday_bonus": "生日禮金",
    "labor_insurance_employee": "勞保",
    "health_insurance_employee": "健保",
    "pension_employee": "勞退自提",
    "leave_deduction": "請假扣款",
    "late_deduction": "遲到扣款",
    "early_leave_deduction": "早退扣款",
    "missing_punch_deduction": "未打卡扣款",
    "meeting_absence_deduction": "節慶獎金扣減",
    "absence_deduction": "曠職扣款",
    "other_deduction": "其他扣款",
}


def _recalculate_salary_record_totals(record: SalaryRecord):
    record.gross_salary = round(
        (record.base_salary or 0)
        + (record.supervisor_allowance or 0)
        + (record.teacher_allowance or 0)
        + (record.meal_allowance or 0)
        + (record.transportation_allowance or 0)
        + (record.other_allowance or 0)
        + (record.performance_bonus or 0)
        + (record.special_bonus or 0)
        + (record.supervisor_dividend or 0)
        + (record.meeting_overtime_pay or 0)
        + (record.birthday_bonus or 0)
        + (record.overtime_pay or 0)
    )
    record.total_deduction = round(
        (record.labor_insurance_employee or 0)
        + (record.health_insurance_employee or 0)
        + (record.pension_employee or 0)
        + (record.late_deduction or 0)
        + (record.early_leave_deduction or 0)
        + (record.missing_punch_deduction or 0)
        + (record.leave_deduction or 0)
        + (record.absence_deduction or 0)
        + (record.other_deduction or 0)
    )
    record.bonus_amount = round(
        (record.festival_bonus or 0)
        + (record.overtime_bonus or 0)
        + (record.supervisor_dividend or 0)
    )
    record.bonus_separate = record.bonus_amount > 0
    record.net_salary = round((record.gross_salary or 0) - (record.total_deduction or 0))


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
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/salaries/{record_id}/finalize")
def unfinalize_salary(
    record_id: int,
    current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)),
):
    """解除單筆薪資封存（危險操作，僅限 admin/hr，會記錄稽核備註）"""
    if current_user.get("role") not in ("admin", "hr"):
        raise HTTPException(status_code=403, detail="薪資封存解除僅限系統管理員或人事主管操作")
    session = get_session()
    try:
        record = session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        if not record:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
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
        raise_safe_500(e)
    finally:
        session.close()


# ============ Salary Simulation ============

_SIMULATE_COMPARE_KEYS = [
    "base_salary", "festival_bonus", "overtime_bonus", "overtime_pay",
    "late_deduction", "early_leave_deduction", "leave_deduction",
    "absence_deduction", "gross_salary", "total_deductions", "net_pay",
]


def _build_salary_display_dict(**kwargs) -> dict:
    """共用薪資欄位 dict 建構器，確保兩個轉換函式的欄位名稱與順序一致。"""
    keys = [
        "base_salary", "total_allowances", "festival_bonus", "overtime_bonus", "overtime_pay",
        "meeting_overtime_pay", "birthday_bonus", "supervisor_dividend",
        "labor_insurance", "health_insurance", "pension_self",
        "late_deduction", "early_leave_deduction", "missing_punch_deduction",
        "leave_deduction", "absence_deduction", "meeting_absence_deduction",
        "gross_salary", "total_deductions", "net_pay",
        "late_count", "early_leave_count", "missing_punch_count",
    ]
    return {k: kwargs.get(k, 0) for k in keys}


def _breakdown_to_simulate_dict(breakdown, late_count: int, early_count: int, missing_count: int) -> dict:
    return _build_salary_display_dict(
        base_salary=breakdown.base_salary,
        total_allowances=calculate_total_allowances(breakdown),
        festival_bonus=breakdown.festival_bonus,
        overtime_bonus=breakdown.overtime_bonus,
        overtime_pay=breakdown.overtime_work_pay,
        meeting_overtime_pay=breakdown.meeting_overtime_pay or 0,
        birthday_bonus=breakdown.birthday_bonus or 0,
        supervisor_dividend=breakdown.supervisor_dividend,
        labor_insurance=breakdown.labor_insurance,
        health_insurance=breakdown.health_insurance,
        pension_self=breakdown.pension_self or 0,
        late_deduction=breakdown.late_deduction,
        early_leave_deduction=breakdown.early_leave_deduction,
        missing_punch_deduction=breakdown.missing_punch_deduction,
        leave_deduction=breakdown.leave_deduction,
        absence_deduction=breakdown.absence_deduction or 0,
        meeting_absence_deduction=breakdown.meeting_absence_deduction or 0,
        gross_salary=breakdown.gross_salary,
        total_deductions=breakdown.total_deduction,
        net_pay=breakdown.net_salary,
        late_count=late_count,
        early_leave_count=early_count,
        missing_punch_count=missing_count,
    )


def _record_to_actual_dict(record) -> dict:
    return _build_salary_display_dict(
        base_salary=record.base_salary or 0,
        total_allowances=(
            (record.supervisor_allowance or 0)
            + (record.teacher_allowance or 0)
            + (record.meal_allowance or 0)
            + (record.transportation_allowance or 0)
            + (record.other_allowance or 0)
        ),
        festival_bonus=record.festival_bonus or 0,
        overtime_bonus=record.overtime_bonus or 0,
        overtime_pay=record.overtime_pay or 0,
        meeting_overtime_pay=record.meeting_overtime_pay or 0,
        birthday_bonus=record.birthday_bonus or 0,
        supervisor_dividend=record.supervisor_dividend or 0,
        labor_insurance=record.labor_insurance_employee or 0,
        health_insurance=record.health_insurance_employee or 0,
        pension_self=record.pension_employee or 0,
        late_deduction=record.late_deduction or 0,
        early_leave_deduction=record.early_leave_deduction or 0,
        missing_punch_deduction=record.missing_punch_deduction or 0,
        leave_deduction=record.leave_deduction or 0,
        absence_deduction=record.absence_deduction or 0,
        meeting_absence_deduction=record.meeting_absence_deduction or 0,
        gross_salary=record.gross_salary or 0,
        total_deductions=record.total_deduction or 0,
        net_pay=record.net_salary or 0,
        late_count=record.late_count or 0,
        early_leave_count=record.early_leave_count or 0,
        missing_punch_count=record.missing_punch_count or 0,
    )


@router.post("/salaries/simulate")
def simulate_salary(
    req: SalarySimulateRequest,
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
):
    """薪資試算（沙盒模式，不寫入 DB）——套用覆蓋參數後回傳計算結果與實際紀錄對比。"""
    if not _salary_engine:
        raise HTTPException(status_code=503, detail="薪資引擎尚未初始化")

    engine: RuntimeSalaryEngine = _salary_engine
    ov = req.overrides
    year, month = req.year, req.month

    import calendar as _calendar
    _, last_day = _calendar.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    session = get_session()
    try:
        emp = (
            session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(Employee.id == req.employee_id)
            .first()
        )
        if not emp:
            raise HTTPException(status_code=404, detail=f"員工 id={req.employee_id} 不存在")
        if emp.employee_type == "hourly":
            raise HTTPException(status_code=422, detail="時薪制員工不支援試算功能")

        emp_dict = engine._load_emp_dict(emp)

        # 查詢實際考勤
        attendances = session.query(Attendance).filter(
            Attendance.employee_id == emp.id,
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date,
        ).all()

        # 實際考勤統計
        actual_late_count = sum(1 for a in attendances if a.is_late)
        actual_early_count = sum(1 for a in attendances if a.is_early_leave)
        actual_missing = sum(
            1 for a in attendances if a.is_missing_punch_in or a.is_missing_punch_out
        )
        actual_late_min = sum(a.late_minutes or 0 for a in attendances if a.is_late)
        actual_early_min = sum(a.early_leave_minutes or 0 for a in attendances if a.is_early_leave)

        # 套用覆蓋
        sim_late_count = ov.late_count if ov.late_count is not None else actual_late_count
        sim_early_count = ov.early_leave_count if ov.early_leave_count is not None else actual_early_count
        sim_missing = ov.missing_punch_count if ov.missing_punch_count is not None else actual_missing
        sim_late_min = ov.total_late_minutes if ov.total_late_minutes is not None else actual_late_min
        sim_early_min = ov.total_early_minutes if ov.total_early_minutes is not None else actual_early_min
        sim_work_days = ov.work_days if ov.work_days is not None else len(attendances)

        # 將分鐘數均分給每次遲到（給 _calculate_deductions 用）
        emp_dict["_late_details"] = (
            [sim_late_min / sim_late_count] * sim_late_count if sim_late_count > 0 else []
        )

        from services.attendance_parser import AttendanceResult
        attendance_result = AttendanceResult(
            employee_name=emp.name,
            total_days=sim_work_days,
            normal_days=max(0, sim_work_days - sim_late_count - sim_early_count),
            late_count=sim_late_count,
            early_leave_count=sim_early_count,
            missing_punch_in_count=sim_missing,
            missing_punch_out_count=0,
            total_late_minutes=int(sim_late_min),
            total_early_minutes=int(sim_early_min),
            details=[],
        )

        allowances = engine._load_allowances_list(session, emp)

        classroom_context, office_staff_context = engine._build_contexts(session, emp, end_date)
        if ov.enrollment_override is not None:
            if classroom_context:
                classroom_context["current_enrollment"] = ov.enrollment_override
            if office_staff_context:
                office_staff_context["school_enrollment"] = ov.enrollment_override

        daily_salary = calc_daily_salary(emp.base_salary)
        period_records = engine._load_period_records(
            session, emp, start_date, end_date, year, month, daily_salary
        )

        extra_leave_hours = ov.extra_personal_leave_hours + ov.extra_sick_leave_hours
        hourly_rate = calc_daily_salary(emp.base_salary) / 8
        leave_deduction = period_records["leave_deduction"] + extra_leave_hours * hourly_rate
        personal_sick_hours = period_records["personal_sick_leave_hours"] + extra_leave_hours
        overtime_pay = period_records["overtime_work_pay"] + ov.extra_overtime_pay

        absent_count, absence_amount = engine._detect_absences(
            session, emp, attendances, period_records["approved_leaves"],
            start_date, end_date, year, month,
        )

        breakdown = engine.calculate_salary(
            employee=emp_dict,
            year=year,
            month=month,
            attendance=attendance_result,
            leave_deduction=leave_deduction,
            allowances=allowances,
            classroom_context=classroom_context,
            office_staff_context=office_staff_context,
            meeting_context=period_records["meeting_context"],
            overtime_work_pay=overtime_pay,
            personal_sick_leave_hours=personal_sick_hours,
        )
        breakdown.absent_count = absent_count
        breakdown.absence_deduction = round(absence_amount)
        breakdown.total_deduction = round(breakdown.total_deduction + absence_amount)
        breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction

        actual_record = session.query(SalaryRecord).filter(
            SalaryRecord.employee_id == emp.id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        ).first()

        simulated = _breakdown_to_simulate_dict(breakdown, sim_late_count, sim_early_count, sim_missing)
        actual = _record_to_actual_dict(actual_record) if actual_record else None
        diff = (
            {k: round(simulated.get(k, 0) - actual.get(k, 0)) for k in _SIMULATE_COMPARE_KEYS}
            if actual else None
        )

        overrides_active = [
            k for k, v in ov.model_dump().items()
            if v is not None and v != 0
        ]

        return {
            "employee": {
                "id": emp.id,
                "name": emp.name,
                "employee_id": emp.employee_id,
                "job_title": emp.title_name,
            },
            "period": {"year": year, "month": month},
            "overrides_active": overrides_active,
            "simulated": simulated,
            "actual": actual,
            "diff": diff,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e)
    finally:
        session.close()
