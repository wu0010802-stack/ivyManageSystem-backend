"""
Portal - profile endpoints
"""

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from utils.errors import raise_safe_500

from models.database import get_session, Classroom
from models.auth import User
from utils.auth import get_current_user
from ._shared import _get_employee, ProfileUpdate, _mask_bank_account

_LINE_USER_ID_RE = re.compile(r"^U[0-9a-f]{32}$")


class LineBindingUpdate(BaseModel):
    line_user_id: str

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
            "bank_account": _mask_bank_account(emp.bank_account),
            "bank_account_name": emp.bank_account_name,
        }
    finally:
        session.close()


@router.get("/profile/line-binding")
def get_line_binding(
    current_user: dict = Depends(get_current_user),
):
    """取得目前 LINE 綁定狀態"""
    session = get_session()
    try:
        user_id = current_user.get("user_id")
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用戶不存在")
        return {"line_user_id": user.line_user_id}
    finally:
        session.close()


@router.put("/profile/line-binding")
def update_line_binding(
    data: LineBindingUpdate,
    current_user: dict = Depends(get_current_user),
):
    """綁定 LINE User ID（格式 ^U[0-9a-f]{32}$）"""
    if not _LINE_USER_ID_RE.match(data.line_user_id):
        raise HTTPException(status_code=400, detail="LINE User ID 格式不正確（應為 U 開頭後接 32 個小寫十六進位字元）")
    session = get_session()
    try:
        # 唯一性衝突檢查
        existing = session.query(User).filter(
            User.line_user_id == data.line_user_id,
            User.id != current_user.get("user_id"),
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="此 LINE 帳號已被其他用戶綁定")

        user_id = current_user.get("user_id")
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用戶不存在")
        user.line_user_id = data.line_user_id
        session.commit()
        return {"message": "LINE 綁定成功", "line_user_id": data.line_user_id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/profile/line-binding")
def delete_line_binding(
    current_user: dict = Depends(get_current_user),
):
    """解除 LINE 綁定"""
    session = get_session()
    try:
        user_id = current_user.get("user_id")
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="用戶不存在")
        user.line_user_id = None
        session.commit()
        return {"message": "LINE 綁定已解除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
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

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if key in allowed_fields:
                setattr(emp, key, value)

        session.commit()
        return {"message": "個人資料已更新"}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
