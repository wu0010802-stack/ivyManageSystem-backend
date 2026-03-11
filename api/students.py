"""
Student management router
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Literal
from sqlalchemy import func

from models.database import get_session, Student
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["students"])


# ============ Pydantic Models ============

class StudentCreate(BaseModel):
    student_id: str
    name: str
    gender: Optional[str] = None
    birthday: Optional[str] = None
    classroom_id: Optional[int] = None
    enrollment_date: Optional[str] = None
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    status_tag: Optional[str] = None
    allergy: Optional[str] = None
    medication: Optional[str] = None
    special_needs: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relation: Optional[str] = None


class StudentUpdate(BaseModel):
    student_id: Optional[str] = None
    name: Optional[str] = None
    gender: Optional[str] = None
    birthday: Optional[str] = None
    classroom_id: Optional[int] = None
    enrollment_date: Optional[str] = None
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    status_tag: Optional[str] = None
    allergy: Optional[str] = None
    medication: Optional[str] = None
    special_needs: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relation: Optional[str] = None


class StudentGraduate(BaseModel):
    graduation_date: str
    status: Literal['已畢業', '已轉出']


# ============ Routes ============

@router.get("/students")
async def get_students(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    classroom_id: Optional[int] = None,
    search: Optional[str] = None,
    is_active: Optional[bool] = Query(True),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """取得學生列表（分頁）。is_active=true 為在讀，is_active=false 為已離園"""
    session = get_session()
    try:
        q = session.query(Student).filter(Student.is_active == is_active)

        if classroom_id is not None:
            q = q.filter(Student.classroom_id == classroom_id)
        if search:
            like = f"%{search}%"
            q = q.filter(
                (Student.name.ilike(like)) | (Student.student_id.ilike(like))
            )

        total = q.count()
        students = q.order_by(Student.id).offset(skip).limit(limit).all()

        items = []
        for s in students:
            items.append({
                "id": s.id,
                "student_id": s.student_id,
                "name": s.name,
                "gender": s.gender,
                "birthday": s.birthday.isoformat() if s.birthday else None,
                "classroom_id": s.classroom_id,
                "enrollment_date": s.enrollment_date.isoformat() if s.enrollment_date else None,
                "graduation_date": s.graduation_date.isoformat() if s.graduation_date else None,
                "status": s.status,
                "parent_name": s.parent_name,
                "parent_phone": s.parent_phone,
                "address": s.address,
                "status_tag": s.status_tag,
                "allergy": s.allergy,
                "medication": s.medication,
                "special_needs": s.special_needs,
                "emergency_contact_name": s.emergency_contact_name,
                "emergency_contact_phone": s.emergency_contact_phone,
                "emergency_contact_relation": s.emergency_contact_relation,
                "is_active": s.is_active
            })
        return {"items": items, "total": total, "skip": skip, "limit": limit}
    finally:
        session.close()


@router.get("/students/{student_id}")
async def get_student(student_id: int, current_user: dict = Depends(require_permission(Permission.STUDENTS_READ))):
    """取得單一學生詳細資料"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")
        return {
            "id": student.id,
            "student_id": student.student_id,
            "name": student.name,
            "gender": student.gender,
            "birthday": student.birthday.isoformat() if student.birthday else None,
            "classroom_id": student.classroom_id,
            "enrollment_date": student.enrollment_date.isoformat() if student.enrollment_date else None,
            "parent_name": student.parent_name,
            "parent_phone": student.parent_phone,
            "address": student.address,
            "notes": student.notes,
            "allergy": student.allergy,
            "medication": student.medication,
            "special_needs": student.special_needs,
            "emergency_contact_name": student.emergency_contact_name,
            "emergency_contact_phone": student.emergency_contact_phone,
            "emergency_contact_relation": student.emergency_contact_relation,
            "is_active": student.is_active
        }
    finally:
        session.close()


@router.post("/students", status_code=201)
async def create_student(item: StudentCreate, current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE))):
    """新增學生"""
    session = get_session()
    try:
        # 檢查學號是否重複
        existing = session.query(Student).filter(Student.student_id == item.student_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="學號已存在")

        data = item.dict()
        # 處理日期欄位
        if data.get('birthday'):
            data['birthday'] = datetime.strptime(data['birthday'], '%Y-%m-%d').date()
        else:
            data.pop('birthday', None)

        if data.get('enrollment_date'):
            data['enrollment_date'] = datetime.strptime(data['enrollment_date'], '%Y-%m-%d').date()
        else:
            data.pop('enrollment_date', None)

        student = Student(**data)
        session.add(student)
        session.commit()
        return {"message": "學生新增成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()


@router.put("/students/{student_id}")
async def update_student(student_id: int, item: StudentUpdate, current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE))):
    """更新學生資料"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")

        update_data = item.dict(exclude_unset=True)

        # 處理日期欄位
        if 'birthday' in update_data and update_data['birthday']:
            update_data['birthday'] = datetime.strptime(update_data['birthday'], '%Y-%m-%d').date()

        if 'enrollment_date' in update_data and update_data['enrollment_date']:
            update_data['enrollment_date'] = datetime.strptime(update_data['enrollment_date'], '%Y-%m-%d').date()

        NULLABLE_FK_FIELDS = {'classroom_id'}
        for key, value in update_data.items():
            if value is not None or key in NULLABLE_FK_FIELDS:
                setattr(student, key, value)

        session.commit()
        return {"message": "學生資料更新成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@router.delete("/students/{student_id}")
async def delete_student(student_id: int, current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE))):
    """刪除學生（軟刪除）"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")

        student.is_active = False
        session.commit()
        return {"message": "學生已刪除", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"刪除失敗: {str(e)}")
    finally:
        session.close()


@router.post("/students/{student_id}/graduate")
async def graduate_student(
    student_id: int,
    item: StudentGraduate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """設定學生畢業或轉出，並標記為非在讀"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")
        if not student.is_active:
            raise HTTPException(status_code=400, detail="該學生已非在讀狀態")

        graduation_date = datetime.strptime(item.graduation_date, '%Y-%m-%d').date()
        if student.enrollment_date and graduation_date < student.enrollment_date:
            raise HTTPException(status_code=400, detail="離園日期不可早於入學日期")

        student.graduation_date = graduation_date
        student.status = item.status
        student.is_active = False
        session.commit()
        logger.warning("學生離園：id=%s name=%s status=%s operator=%s",
                       student.id, student.name, item.status, current_user.get("username"))
        return {"message": f"已設定為「{item.status}」", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"操作失敗: {str(e)}")
    finally:
        session.close()
