"""
Portal - profile endpoints
"""

from fastapi import APIRouter, Depends, HTTPException

from models.database import get_session, Classroom
from utils.auth import get_current_user
from ._shared import _get_employee, ProfileUpdate

router = APIRouter()


@router.get("/profile")
def get_profile(
    current_user: dict = Depends(get_current_user),
):
    """取得個人資料"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        job_title_name = None
        if emp.job_title_rel:
            job_title_name = emp.job_title_rel.name

        classroom_name = None
        classroom = session.query(Classroom).filter(
            Classroom.is_active == True,
            (Classroom.head_teacher_id == emp.id) |
            (Classroom.assistant_teacher_id == emp.id) |
            (Classroom.art_teacher_id == emp.id),
        ).first()
        if classroom:
            classroom_name = classroom.name

        return {
            "employee_id": emp.employee_id,
            "name": emp.name,
            "job_title": job_title_name,
            "position": emp.position,
            "classroom": classroom_name,
            "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
            "work_start_time": emp.work_start_time,
            "work_end_time": emp.work_end_time,
            "phone": emp.phone,
            "address": emp.address,
            "emergency_contact_name": emp.emergency_contact_name,
            "emergency_contact_phone": emp.emergency_contact_phone,
            "bank_code": emp.bank_code,
            "bank_account": emp.bank_account,
            "bank_account_name": emp.bank_account_name,
        }
    finally:
        session.close()


@router.put("/profile")
def update_profile(
    data: ProfileUpdate,
    current_user: dict = Depends(get_current_user),
):
    """更新個人資料（僅限允許欄位）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        allowed_fields = [
            "phone", "address",
            "emergency_contact_name", "emergency_contact_phone",
            "bank_code", "bank_account", "bank_account_name",
        ]

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if key in allowed_fields:
                setattr(emp, key, value)

        session.commit()
        return {"message": "個人資料已更新"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
