"""
Employee management router
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from utils.errors import raise_safe_500
from pydantic import BaseModel, Field
from sqlalchemy.orm import joinedload

from models.database import get_session, session_scope, Employee, Classroom, JobTitle
from utils.auth import require_permission
from utils.error_messages import EMPLOYEE_NOT_FOUND
from utils.masking import mask_bank_account, mask_id_number
from utils.permissions import Permission, has_permission
from utils.validators import parse_optional_date

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["employees"])

_DATE_FIELDS = ('hire_date', 'probation_end_date', 'birthday')


_salary_engine = None

def init_employee_services(salary_engine):
    global _salary_engine
    _salary_engine = salary_engine


# ============ Helpers ============

def _format_employee_response(
    emp,
    can_view_full_account: bool,
    *,
    resign_fields: bool = False,
    include_transportation_allowance: bool = False,
    classroom_name: str | None = None,
) -> dict:
    """共用員工響應格式化，避免 GET /employees 與 GET /employees/{id} 重複組裝相同欄位。"""
    display_title = emp.job_title_rel.name if emp.job_title_rel else emp.title
    data = {
        "id": emp.id,
        "employee_id": emp.employee_id,
        "name": emp.name,
        "id_number": emp.id_number if can_view_full_account else mask_id_number(emp.id_number),
        "employee_type": emp.employee_type,
        "title": display_title,
        "job_title_id": emp.job_title_id,
        "position": emp.position,
        "supervisor_role": emp.supervisor_role,
        "bonus_grade": getattr(emp, 'bonus_grade', None),
        "classroom_id": emp.classroom_id,
        "base_salary": emp.base_salary,
        "hourly_rate": emp.hourly_rate,
        "supervisor_allowance": emp.supervisor_allowance,
        "teacher_allowance": emp.teacher_allowance,
        "meal_allowance": emp.meal_allowance,
        "other_allowance": emp.other_allowance,
        "bank_code": emp.bank_code if can_view_full_account else None,
        "bank_account": emp.bank_account if can_view_full_account else mask_bank_account(emp.bank_account),
        "bank_account_name": emp.bank_account_name if can_view_full_account else None,
        "insurance_salary_level": emp.insurance_salary_level,
        "pension_self_rate": emp.pension_self_rate,
        "work_start_time": emp.work_start_time,
        "work_end_time": emp.work_end_time,
        "is_active": emp.is_active,
        "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
        "probation_end_date": emp.probation_end_date.isoformat() if emp.probation_end_date else None,
        "birthday": emp.birthday.isoformat() if emp.birthday else None,
    }
    if resign_fields:
        data["resign_date"] = emp.resign_date.isoformat() if emp.resign_date else None
        data["resign_reason"] = getattr(emp, 'resign_reason', None)
    if include_transportation_allowance:
        data["transportation_allowance"] = emp.transportation_allowance
    if classroom_name is not None:
        data["classroom_name"] = classroom_name
    return data


# ============ Pydantic Models ============

class EmployeeCreate(BaseModel):
    employee_id: str
    name: str
    id_number: Optional[str] = None
    employee_type: str = "regular"
    title: Optional[str] = None  # Legacy/Display
    job_title_id: Optional[int] = None  # New FK
    position: Optional[str] = None
    supervisor_role: Optional[str] = Field(None, pattern="^(園長|主任|組長|副組長)$")
    bonus_grade: Optional[str] = Field(None, pattern="^[ABC]$")
    classroom_id: Optional[int] = None
    base_salary: float = Field(0, ge=0)
    hourly_rate: float = Field(0, ge=0)
    supervisor_allowance: float = Field(0, ge=0)
    teacher_allowance: float = Field(0, ge=0)
    meal_allowance: float = Field(0, ge=0)
    transportation_allowance: float = Field(0, ge=0)
    other_allowance: float = Field(0, ge=0)
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None
    insurance_salary_level: float = Field(0, ge=0)
    pension_self_rate: float = Field(0, ge=0, le=0.06)  # 勞退自提最高 6%
    work_start_time: str = "08:00"
    work_end_time: str = "17:00"
    hire_date: Optional[str] = None
    probation_end_date: Optional[str] = None
    birthday: Optional[str] = None
    dependents: int = Field(0, ge=0)


class EmployeeUpdate(BaseModel):
    employee_id: Optional[str] = None
    name: Optional[str] = None
    id_number: Optional[str] = None
    employee_type: Optional[str] = None
    title: Optional[str] = None
    job_title_id: Optional[int] = None
    position: Optional[str] = None
    supervisor_role: Optional[str] = Field(None, pattern="^(園長|主任|組長|副組長)$")
    bonus_grade: Optional[str] = Field(None, pattern="^[ABC]$")
    classroom_id: Optional[int] = None
    base_salary: Optional[float] = Field(None, ge=0)
    hourly_rate: Optional[float] = Field(None, ge=0)
    supervisor_allowance: Optional[float] = Field(None, ge=0)
    teacher_allowance: Optional[float] = Field(None, ge=0)
    meal_allowance: Optional[float] = Field(None, ge=0)
    transportation_allowance: Optional[float] = Field(None, ge=0)
    other_allowance: Optional[float] = Field(None, ge=0)
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None
    insurance_salary_level: Optional[float] = Field(None, ge=0)
    pension_self_rate: Optional[float] = Field(None, ge=0, le=0.06)  # 勞退自提最高 6%
    work_start_time: Optional[str] = None
    work_end_time: Optional[str] = None
    hire_date: Optional[str] = None
    probation_end_date: Optional[str] = None
    birthday: Optional[str] = None
    dependents: Optional[int] = Field(None, ge=0)


class OffboardRequest(BaseModel):
    resign_date: str   # ISO 格式，可為未來日期
    resign_reason: Optional[str] = None




# ============ Routes ============

@router.get("/employees")
def get_employees(
    skip: int = 0,
    limit: int = 100,
    search: Optional[str] = None,
    current_user: dict = Depends(require_permission(Permission.EMPLOYEES_READ)),
):
    session = get_session()
    try:
        q = session.query(Employee).options(joinedload(Employee.job_title_rel))
        if search:
            like = f"%{search}%"
            q = q.filter(Employee.name.ilike(like) | Employee.employee_id.ilike(like))
        employees = q.offset(skip).limit(limit).all()
        can_view_full_account = has_permission(current_user.get("permissions", 0), Permission.SALARY_WRITE)

        result = []
        for emp in employees:
            result.append(_format_employee_response(emp, can_view_full_account, resign_fields=True))
        return result
    finally:
        session.close()


@router.get("/employees/probation-alerts")
async def get_probation_alerts(
    days: int = 60,
    current_user: dict = Depends(require_permission(Permission.EMPLOYEES_READ)),
):
    """取得試用期即將到期的員工（預設 60 天內）"""
    session = get_session()
    try:
        today = date.today()
        deadline = today + timedelta(days=days)
        next_month_end = today + timedelta(days=30)

        employees = session.query(Employee).filter(
            Employee.is_active == True,
            Employee.probation_end_date != None,
            Employee.probation_end_date >= today,
            Employee.probation_end_date <= deadline,
        ).order_by(Employee.probation_end_date).all()

        result = []
        next_month_count = 0
        for emp in employees:
            days_remaining = (emp.probation_end_date - today).days
            result.append({
                "id": emp.id,
                "name": emp.name,
                "employee_id": emp.employee_id,
                "probation_end_date": emp.probation_end_date.isoformat(),
                "days_remaining": days_remaining,
            })
            if emp.probation_end_date <= next_month_end:
                next_month_count += 1

        return {
            "employees": result,
            "alerts": {
                "next_month": next_month_count,
            },
        }
    finally:
        session.close()


@router.get("/employees/{employee_id}")
async def get_employee(employee_id: int, current_user: dict = Depends(require_permission(Permission.EMPLOYEES_READ))):
    """取得單一員工詳細資料"""
    session = get_session()
    try:
        employee = session.query(Employee).options(joinedload(Employee.job_title_rel)).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail=EMPLOYEE_NOT_FOUND)
        can_view_full_account = has_permission(current_user.get("permissions", 0), Permission.SALARY_WRITE)

        # Get classroom name if assigned
        classroom_name = None
        if employee.classroom_id:
            classroom = session.query(Classroom).options(
                joinedload(Classroom.grade)
            ).filter(Classroom.id == employee.classroom_id).first()
            if classroom:
                classroom_name = f"{classroom.name} ({classroom.grade.name})" if classroom.grade else classroom.name

        return _format_employee_response(
            employee, can_view_full_account,
            resign_fields=True,
            include_transportation_allowance=True,
            classroom_name=classroom_name,
        )
    finally:
        session.close()


@router.post("/employees", status_code=201)
async def create_employee(emp: EmployeeCreate, current_user: dict = Depends(require_permission(Permission.EMPLOYEES_WRITE))):
    """新增員工"""
    session = get_session()
    try:
        # 檢查工號是否重複
        existing = session.query(Employee).filter(Employee.employee_id == emp.employee_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="工號已存在")

        emp_data = emp.model_dump()
        # 處理日期欄位
        for _field in _DATE_FIELDS:
            parsed = parse_optional_date(emp_data.get(_field))
            if parsed:
                emp_data[_field] = parsed
            else:
                emp_data.pop(_field, None)

        if not emp_data.get('supervisor_role'):
            emp_data['supervisor_role'] = None

        # Sync title string from job_title_id for safety/legacy
        if emp_data.get('job_title_id'):
            job_title = session.query(JobTitle).get(emp_data['job_title_id'])
            if job_title:
                emp_data['title'] = job_title.name
            else:
                raise HTTPException(status_code=400, detail="無效的職稱ID")
        elif 'title' in emp_data:  # If job_title_id is not provided, but title is, use it
            pass
        else:  # If neither is provided, set title to None
            emp_data['title'] = None

        employee = Employee(**emp_data)
        session.add(employee)
        session.commit()
        return {"message": "員工新增成功", "id": employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="新增失敗")
    finally:
        session.close()


@router.put("/employees/{employee_id}")
async def update_employee(employee_id: int, emp: EmployeeUpdate, current_user: dict = Depends(require_permission(Permission.EMPLOYEES_WRITE))):
    """更新員工資料"""
    session = get_session()
    try:
        db_employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not db_employee:
            raise HTTPException(status_code=404, detail=EMPLOYEE_NOT_FOUND)

        update_data = emp.model_dump(exclude_unset=True)

        # 處理日期欄位
        for _field in _DATE_FIELDS:
            if _field in update_data and update_data[_field]:
                update_data[_field] = parse_optional_date(update_data[_field])

        for key, value in update_data.items():
            if value is not None:
                if key == 'job_title_id':
                    setattr(db_employee, key, value)
                    # Sync legacy title
                    if value:
                        jt = session.query(JobTitle).get(value)
                        if jt:
                            db_employee.title = jt.name
                        else:
                            raise HTTPException(status_code=400, detail="無效的職稱ID")
                    else:
                        db_employee.title = None  # If job_title_id is set to None, clear title
                elif key != 'title':  # validation exclude manual title update
                    setattr(db_employee, key, value)
            elif key == 'job_title_id' and value is None:  # Allow explicitly setting job_title_id to None
                setattr(db_employee, key, None)
                db_employee.title = None  # Clear title if job_title_id is cleared
            elif key == 'classroom_id' and value is None:  # Allow explicitly removing from classroom
                setattr(db_employee, key, None)
            elif key == 'supervisor_role' and value is None:
                setattr(db_employee, key, None)

        session.commit()
        return {"message": "員工資料更新成功", "id": db_employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="更新失敗")
    finally:
        session.close()


@router.delete("/employees/{employee_id}")
async def delete_employee(employee_id: int, current_user: dict = Depends(require_permission(Permission.EMPLOYEES_WRITE))):
    """刪除員工（軟刪除，設為離職）"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail=EMPLOYEE_NOT_FOUND)

        employee.is_active = False
        if not employee.resign_date:
            employee.resign_date = date.today()
        session.commit()
        return {"message": "員工已設為離職", "id": employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="刪除失敗")
    finally:
        session.close()


@router.post("/employees/{employee_id}/offboard")
async def offboard_employee(
    employee_id: int,
    req: OffboardRequest,
    current_user: dict = Depends(require_permission(Permission.EMPLOYEES_WRITE)),
):
    """辦理離職：設定離職日與離職原因，若離職日 <= 今天則同步設 is_active = False"""
    try:
        resign_d = datetime.strptime(req.resign_date, '%Y-%m-%d').date()
    except ValueError:
        raise HTTPException(status_code=400, detail="resign_date 格式錯誤，請使用 YYYY-MM-DD")

    with session_scope() as session:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=EMPLOYEE_NOT_FOUND)

        emp.resign_date = resign_d
        emp.resign_reason = req.resign_reason

        today = date.today()
        if resign_d <= today:
            emp.is_active = False
        # 若 resign_date > today，保留 is_active = True（通知期）

        logger.warning(
            "辦理離職：employee_id=%s name=%s resign_date=%s operator=%s",
            emp.employee_id, emp.name, resign_d, current_user.get("sub"),
        )

        return {
            "message": "離職資料已更新",
            "id": emp.id,
            "name": emp.name,
            "resign_date": resign_d.isoformat(),
            "resign_reason": emp.resign_reason,
            "is_active": emp.is_active,
        }


@router.get("/employees/{employee_id}/final-salary-preview")
async def final_salary_preview(
    employee_id: int,
    year: int,
    month: int,
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
):
    """最終薪資預覽：呼叫薪資引擎計算指定員工指定月份薪資（含月中離職折算）"""
    if _salary_engine is None:
        raise HTTPException(status_code=503, detail="薪資引擎尚未初始化")
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month 必須介於 1–12")

    try:
        breakdown = _salary_engine.process_salary_calculation(employee_id, year, month)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("final-salary-preview 計算失敗：employee_id=%s", employee_id)
        raise_safe_500(e, context="計算失敗")

    import calendar as _cal
    with session_scope() as session:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=EMPLOYEE_NOT_FOUND)
        resign_d = emp.resign_date
        contracted_base = emp.base_salary or 0

    _, month_days = _cal.monthrange(year, month)
    proration_note = None
    is_mid_month_resignation = (
        resign_d is not None
        and resign_d.year == year
        and resign_d.month == month
        and resign_d.day < month_days
    )
    if is_mid_month_resignation:
        proration_note = f"在職 {resign_d.day} 天，折算後 NT${breakdown.base_salary:,.0f}"

    return {
        "year": year,
        "month": month,
        "contracted_base_salary": contracted_base,
        "base_salary": breakdown.base_salary,
        "proration_note": proration_note,
        "total_allowance": breakdown.total_allowance,
        "festival_bonus": breakdown.festival_bonus,
        "gross_salary": breakdown.gross_salary,
        "total_deduction": breakdown.total_deduction,
        "labor_insurance": breakdown.labor_insurance,
        "health_insurance": breakdown.health_insurance,
        "pension": breakdown.pension,
        "net_salary": breakdown.net_salary,
    }


@router.get("/teachers")
async def get_teachers(current_user: dict = Depends(require_permission(Permission.EMPLOYEES_READ))):
    """取得所有可作為老師的員工"""
    session = get_session()
    try:
        employees = session.query(
            Employee.id, Employee.employee_id, Employee.name, Employee.title
        ).filter(Employee.is_active == True).all()
        return [{
            "id": e.id,
            "employee_id": e.employee_id,
            "name": e.name,
            "title": e.title,
        } for e in employees]
    finally:
        session.close()
