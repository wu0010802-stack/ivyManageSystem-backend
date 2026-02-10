"""
Authentication & user management router
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from models.database import get_session, User, Employee
from utils.auth import (
    hash_password, verify_password, create_access_token, get_current_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ============ Pydantic Models ============

class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    employee_id: int
    username: str
    password: str
    role: str = "teacher"


class ResetPasswordRequest(BaseModel):
    new_password: str


# ============ Public Routes ============

@router.post("/login")
def login(data: LoginRequest):
    """教師/管理員登入"""
    session = get_session()
    try:
        user = session.query(User).filter(
            User.username == data.username,
            User.is_active == True,
        ).first()

        if not user or not verify_password(data.password, user.password_hash):
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()

        user.last_login = datetime.now()
        session.commit()

        token = create_access_token({
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": emp.name if emp else "",
        })

        return {
            "token": token,
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "employee_id": user.employee_id,
                "name": emp.name if emp else "",
                "title": (emp.job_title_rel.name if emp and emp.job_title_rel else (emp.title if emp else "")),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============ Protected Routes ============

@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    """取得目前登入者資訊"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")
        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "employee_id": user.employee_id,
            "name": emp.name if emp else "",
            "title": (emp.job_title_rel.name if emp and emp.job_title_rel else (emp.title if emp else "")),
        }
    finally:
        session.close()


@router.post("/change-password")
def change_password(data: ChangePasswordRequest, current_user: dict = Depends(get_current_user)):
    """修改密碼"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")
        if not verify_password(data.old_password, user.password_hash):
            raise HTTPException(status_code=400, detail="舊密碼錯誤")
        user.password_hash = hash_password(data.new_password)
        session.commit()
        return {"message": "密碼修改成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============ Admin Routes ============

@router.get("/users")
def list_users(current_user: dict = Depends(get_current_user)):
    """列出所有使用者（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")
    session = get_session()
    try:
        users = session.query(User, Employee).outerjoin(
            Employee, User.employee_id == Employee.id
        ).all()
        return [{
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "is_active": u.is_active,
            "employee_id": u.employee_id,
            "employee_name": emp.name if emp else "",
            "last_login": u.last_login.isoformat() if u.last_login else None,
        } for u, emp in users]
    finally:
        session.close()


@router.post("/users")
def create_user(data: CreateUserRequest, current_user: dict = Depends(get_current_user)):
    """建立使用者帳號（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")
    session = get_session()
    try:
        if session.query(User).filter(User.username == data.username).first():
            raise HTTPException(status_code=400, detail="帳號已存在")
        if session.query(User).filter(User.employee_id == data.employee_id).first():
            raise HTTPException(status_code=400, detail="該員工已有帳號")
        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        user = User(
            employee_id=data.employee_id,
            username=data.username,
            password_hash=hash_password(data.password),
            role=data.role,
        )
        session.add(user)
        session.commit()
        return {"message": "帳號建立成功", "id": user.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/users/{user_id}/reset-password")
def reset_password(user_id: int, data: ResetPasswordRequest, current_user: dict = Depends(get_current_user)):
    """重設密碼（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")
        user.password_hash = hash_password(data.new_password)
        session.commit()
        return {"message": "密碼重設成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/users/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(get_current_user)):
    """刪除使用者帳號（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")
        session.delete(user)
        session.commit()
        return {"message": "帳號已刪除"}
    finally:
        session.close()
