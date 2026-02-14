"""
Employee management router
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import joinedload

from models.database import get_session, Employee, Classroom, ClassGrade, JobTitle
from utils.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["employees"])


# ============ Pydantic Models ============

class EmployeeCreate(BaseModel):
    employee_id: str
    name: str
    id_number: Optional[str] = None
    employee_type: str = "regular"
    title: Optional[str] = None  # Legacy/Display
    job_title_id: Optional[int] = None  # New FK
    position: Optional[str] = None
    classroom_id: Optional[int] = None
    base_salary: float = 0
    hourly_rate: float = 0
    supervisor_allowance: float = 0
    teacher_allowance: float = 0
    meal_allowance: float = 0
    transportation_allowance: float = 0
    other_allowance: float = 0
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None
    insurance_salary_level: float = 0
    work_start_time: str = "08:00"
    work_end_time: str = "17:00"
    hire_date: Optional[str] = None
    is_office_staff: bool = False
    dependents: int = 0


class EmployeeUpdate(BaseModel):
    employee_id: Optional[str] = None
    name: Optional[str] = None
    id_number: Optional[str] = None
    employee_type: Optional[str] = None
    title: Optional[str] = None
    job_title_id: Optional[int] = None
    position: Optional[str] = None
    classroom_id: Optional[int] = None
    base_salary: Optional[float] = None
    hourly_rate: Optional[float] = None
    supervisor_allowance: Optional[float] = None
    teacher_allowance: Optional[float] = None
    meal_allowance: Optional[float] = None
    transportation_allowance: Optional[float] = None
    other_allowance: Optional[float] = None
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None
    insurance_salary_level: Optional[float] = None
    work_start_time: Optional[str] = None
    work_end_time: Optional[str] = None
    hire_date: Optional[str] = None
    is_office_staff: Optional[bool] = None
    dependents: Optional[int] = None


# ============ Routes ============

@router.get("/employees")
def get_employees(skip: int = 0, limit: int = 100, current_user: dict = Depends(get_current_user)):
    session = get_session()
    try:
        employees = session.query(Employee).options(joinedload(Employee.job_title_rel)).offset(skip).limit(limit).all()

        result = []
        for emp in employees:
            # Determine strict title to return (from relation)
            display_title = emp.job_title_rel.name if emp.job_title_rel else emp.title

            result.append({
                "id": emp.id,
                "employee_id": emp.employee_id,
                "name": emp.name,
                "id_number": emp.id_number,
                "employee_type": emp.employee_type,
                "title": display_title,  # Return real title name for frontend display compatibility
                "job_title_id": emp.job_title_id,
                "position": emp.position,
                "classroom_id": emp.classroom_id,
                "base_salary": emp.base_salary,
                "hourly_rate": emp.hourly_rate,
                "supervisor_allowance": emp.supervisor_allowance,
                "teacher_allowance": emp.teacher_allowance,
                "meal_allowance": emp.meal_allowance,
                "transportation_allowance": emp.transportation_allowance,
                "other_allowance": emp.other_allowance,
                "insurance_salary_level": emp.insurance_salary_level,
                "work_start_time": emp.work_start_time,
                "work_end_time": emp.work_end_time,
                "is_active": emp.is_active,
                "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
                "bank_code": emp.bank_code,
                "bank_account": emp.bank_account,
                "bank_account_name": emp.bank_account_name,
                "is_office_staff": emp.is_office_staff
            })
        return result
    finally:
        session.close()


@router.get("/employees/{employee_id}")
async def get_employee(employee_id: int, current_user: dict = Depends(get_current_user)):
    """取得單一員工詳細資料"""
    session = get_session()
    try:
        employee = session.query(Employee).options(joinedload(Employee.job_title_rel)).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到該員工")

        display_title = employee.job_title_rel.name if employee.job_title_rel else employee.title

        # Get classroom name if assigned
        classroom_name = None
        if employee.classroom_id:
            classroom = session.query(Classroom).get(employee.classroom_id)
            if classroom:
                grade = session.query(ClassGrade).get(classroom.grade_id) if classroom.grade_id else None
                classroom_name = f"{classroom.name} ({grade.name})" if grade else classroom.name

        return {
            "id": employee.id,
            "employee_id": employee.employee_id,
            "name": employee.name,
            "id_number": employee.id_number,
            "employee_type": employee.employee_type,
            "title": display_title,
            "job_title_id": employee.job_title_id,
            "position": employee.position,
            "classroom_id": employee.classroom_id,
            "classroom_name": classroom_name,
            "base_salary": employee.base_salary,
            "hourly_rate": employee.hourly_rate,
            "supervisor_allowance": employee.supervisor_allowance,
            "teacher_allowance": employee.teacher_allowance,
            "meal_allowance": employee.meal_allowance,
            "transportation_allowance": employee.transportation_allowance,
            "other_allowance": employee.other_allowance,
            "bank_code": employee.bank_code,
            "bank_account": employee.bank_account,
            "bank_account_name": employee.bank_account_name,
            "insurance_salary_level": employee.insurance_salary_level,
            "work_start_time": employee.work_start_time,
            "work_end_time": employee.work_end_time,
            "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
            "is_active": employee.is_active,
            "is_office_staff": employee.is_office_staff or False
        }
    finally:
        session.close()


@router.post("/employees", status_code=201)
async def create_employee(emp: EmployeeCreate, current_user: dict = Depends(require_admin)):
    """新增員工"""
    session = get_session()
    try:
        # 檢查工號是否重複
        existing = session.query(Employee).filter(Employee.employee_id == emp.employee_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="工號已存在")

        emp_data = emp.dict()
        # 處理日期欄位
        if emp_data.get('hire_date'):
            emp_data['hire_date'] = datetime.strptime(emp_data['hire_date'], '%Y-%m-%d').date()
        else:
            emp_data.pop('hire_date', None)

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
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()


@router.put("/employees/{employee_id}")
async def update_employee(employee_id: int, emp: EmployeeUpdate, current_user: dict = Depends(require_admin)):
    """更新員工資料"""
    session = get_session()
    try:
        db_employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not db_employee:
            raise HTTPException(status_code=404, detail="找不到該員工")

        update_data = emp.dict(exclude_unset=True)

        # 處理日期欄位
        if 'hire_date' in update_data and update_data['hire_date']:
            update_data['hire_date'] = datetime.strptime(update_data['hire_date'], '%Y-%m-%d').date()

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

        session.commit()
        return {"message": "員工資料更新成功", "id": db_employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@router.delete("/employees/{employee_id}")
async def delete_employee(employee_id: int, current_user: dict = Depends(require_admin)):
    """刪除員工（軟刪除，設為離職）"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到該員工")

        employee.is_active = False
        session.commit()
        return {"message": "員工已設為離職", "id": employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"刪除失敗: {str(e)}")
    finally:
        session.close()


@router.get("/teachers")
async def get_teachers(current_user: dict = Depends(get_current_user)):
    """取得所有可作為老師的員工"""
    session = get_session()
    try:
        employees = session.query(Employee).filter(Employee.is_active == True).all()
        return [{
            "id": e.id,
            "employee_id": e.employee_id,
            "name": e.name,
            "title": e.title if hasattr(e, 'title') else None
        } for e in employees]
    finally:
        session.close()
