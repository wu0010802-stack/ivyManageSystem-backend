"""
Student management router
"""

import logging
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from typing import Literal
from sqlalchemy import func, or_

from models.database import get_session, Student, Classroom, StudentClassroomTransfer
from models.dismissal import StudentDismissalCall
from utils.academic import resolve_current_academic_term, resolve_academic_term_filters
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)


def _cancel_active_dismissal_calls(session, student: Student) -> list[dict]:
    """取消學生所有進行中（pending/acknowledged）的接送通知。

    必須在 session.commit() 前呼叫（需要 student.name 還在 session 中）。
    回傳每筆被取消通知的 WS 廣播 payload，供呼叫端 commit 後廣播。
    """
    calls = session.query(StudentDismissalCall).filter(
        StudentDismissalCall.student_id == student.id,
        StudentDismissalCall.status.in_(["pending", "acknowledged"]),
    ).all()

    broadcasts = []
    for call in calls:
        call.status = "cancelled"
        broadcasts.append({
            "classroom_id": call.classroom_id,
            "event": {
                "type": "dismissal_call_cancelled",
                "payload": {
                    "id": call.id,
                    "student_id": call.student_id,
                    "student_name": student.name,
                    "classroom_id": call.classroom_id,
                    "status": "cancelled",
                    "requested_at": call.requested_at.isoformat(),
                },
            },
        })

    if broadcasts:
        logger.warning(
            "學生刪除/離園：自動取消 %d 筆進行中接送通知，student_id=%s name=%s",
            len(broadcasts), student.id, student.name,
        )
    return broadcasts


def get_classroom_student_ids_at_date(session, classroom_id: int, at_date: date) -> list[int]:
    """回傳在 at_date 當天歸屬於 classroom_id 的學生 ID 列表。

    查詢邏輯：
    1. 若學生有轉班記錄，取 at_date 當天或之前最後一筆，
       判斷其 to_classroom_id 是否為目標班級。
    2. 若學生從未轉班，直接以當前 classroom_id 判斷。

    班級統計報表（出席、事件等）呼叫此函式取得「當時」的學生列表，
    避免轉班後歷史記錄跟著學生跑到新班級。
    """
    at_dt = datetime.combine(at_date, datetime.max.time())

    # 找所有曾經有轉班記錄的學生
    transferred_student_ids_q = session.query(
        StudentClassroomTransfer.student_id
    ).distinct()

    # 子查詢：每個學生在 at_dt 之前的最後一次轉班時間
    latest_transfer_sq = (
        session.query(
            StudentClassroomTransfer.student_id,
            func.max(StudentClassroomTransfer.transferred_at).label("last_at"),
        )
        .filter(StudentClassroomTransfer.transferred_at <= at_dt)
        .group_by(StudentClassroomTransfer.student_id)
        .subquery()
    )

    # 在 at_dt 當天最後轉入 classroom_id 的學生
    ids_via_transfer = [
        row.student_id
        for row in session.query(StudentClassroomTransfer.student_id)
        .join(
            latest_transfer_sq,
            (StudentClassroomTransfer.student_id == latest_transfer_sq.c.student_id)
            & (StudentClassroomTransfer.transferred_at == latest_transfer_sq.c.last_at),
        )
        .filter(StudentClassroomTransfer.to_classroom_id == classroom_id)
        .all()
    ]

    # 從未轉班且當前 classroom_id 符合的學生
    ids_no_transfer = [
        row.id
        for row in session.query(Student.id)
        .filter(
            Student.classroom_id == classroom_id,
            Student.is_active == True,
            ~Student.id.in_(transferred_student_ids_q),
        )
        .all()
    ]

    return list(set(ids_via_transfer) | set(ids_no_transfer))


router = APIRouter(prefix="/api", tags=["students"])


# ============ Pydantic Models ============

class StudentCreate(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=50)
    gender: Optional[str] = None
    birthday: Optional[date] = None
    classroom_id: Optional[int] = None
    enrollment_date: Optional[date] = None
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

    @field_validator("student_id", mode="before")
    @classmethod
    def strip_student_id(cls, v):
        if isinstance(v, str):
            v = v.strip()
        if not v:
            raise ValueError("學號不可為空")
        return v

    @field_validator("parent_phone", "emergency_contact_phone", mode="before")
    @classmethod
    def validate_phone(cls, v):
        if v is None or v == "":
            return v
        if not re.match(r'^[\d\-\+\(\)\s]{7,20}$', v):
            raise ValueError("電話格式不正確（僅允許數字、-、+、()、空格，長度 7-20）")
        return v


class StudentUpdate(BaseModel):
    student_id: Optional[str] = Field(None, min_length=1, max_length=20)
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    gender: Optional[str] = None
    birthday: Optional[date] = None
    classroom_id: Optional[int] = None
    enrollment_date: Optional[date] = None
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

    @field_validator("student_id", mode="before")
    @classmethod
    def strip_student_id(cls, v):
        if v is None:
            return v
        if isinstance(v, str):
            v = v.strip()
        if not v:
            raise ValueError("學號不可為空")
        return v

    @field_validator("parent_phone", "emergency_contact_phone", mode="before")
    @classmethod
    def validate_phone(cls, v):
        if v is None or v == "":
            return v
        if not re.match(r'^[\d\-\+\(\)\s]{7,20}$', v):
            raise ValueError("電話格式不正確（僅允許數字、-、+、()、空格，長度 7-20）")
        return v


class StudentGraduate(BaseModel):
    graduation_date: str
    status: Literal['已畢業', '已轉出']


class StudentBulkTransfer(BaseModel):
    student_ids: list[int]
    target_classroom_id: int


# ============ Routes ============

@router.get("/students")
async def get_students(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    classroom_id: Optional[int] = None,
    school_year: Optional[int] = Query(None, ge=2020, le=2100),
    semester: Optional[int] = Query(None, ge=1, le=2),
    search: Optional[str] = None,
    is_active: Optional[bool] = Query(True),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """取得學生列表（分頁）。is_active=true 為在讀，is_active=false 為已離園"""
    session = get_session()
    try:
        q = session.query(Student).filter(Student.is_active == is_active)

        # 只有明確指定學年/學期時才套用學期過濾，避免預設行為隱性遮蔽資料
        if school_year is not None or semester is not None:
            resolved_school_year, resolved_semester = resolve_academic_term_filters(school_year, semester)
            q = q.outerjoin(
                Classroom,
                Student.classroom_id == Classroom.id,
            ).filter(
                or_(
                    Student.classroom_id.is_(None),
                    (
                        (Classroom.school_year == resolved_school_year)
                        & (Classroom.semester == resolved_semester)
                    ),
                ),
            )

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

        data = item.model_dump()

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
    """刪除學生（軟刪除）。同時取消該學生所有進行中的接送通知並推送 WS 事件。"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")

        # commit 前取消進行中通知，並收集廣播資料（需要 student.name）
        dismissal_broadcasts = _cancel_active_dismissal_calls(session, student)

        student.is_active = False
        student.status = "已刪除"
        session.commit()
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"刪除失敗: {str(e)}")
    finally:
        session.close()

    # WS 廣播在 session 關閉後執行，避免長時間佔用連線
    if dismissal_broadcasts:
        from api.dismissal_ws import manager as dismissal_manager
        for item in dismissal_broadcasts:
            await dismissal_manager.broadcast(item["classroom_id"], item["event"])

    return {"message": "學生已刪除", "id": student_id}


@router.post("/students/{student_id}/graduate")
async def graduate_student(
    student_id: int,
    item: StudentGraduate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """設定學生畢業或轉出，並標記為非在讀。同時取消進行中的接送通知並推送 WS 事件。"""
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

        # commit 前取消進行中通知（需要 student.name）
        dismissal_broadcasts = _cancel_active_dismissal_calls(session, student)

        student.graduation_date = graduation_date
        student.status = item.status
        student.is_active = False
        session.commit()
        logger.warning("學生離園：id=%s name=%s status=%s operator=%s",
                       student.id, student.name, item.status, current_user.get("username"))
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"操作失敗: {str(e)}")
    finally:
        session.close()

    if dismissal_broadcasts:
        from api.dismissal_ws import manager as dismissal_manager
        for broadcast_item in dismissal_broadcasts:
            await dismissal_manager.broadcast(broadcast_item["classroom_id"], broadcast_item["event"])

    return {"message": f"已設定為「{item.status}」", "id": student_id}


@router.post("/students/bulk-transfer")
async def bulk_transfer_students(
    item: StudentBulkTransfer,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """批次轉班。"""
    session = get_session()
    try:
        if not item.student_ids:
            raise HTTPException(status_code=400, detail="請先選擇學生")

        target_classroom = session.query(Classroom).filter(
            Classroom.id == item.target_classroom_id,
            Classroom.is_active == True,
        ).first()
        if not target_classroom:
            raise HTTPException(status_code=400, detail="班級不存在或已停用")

        students = session.query(Student).filter(
            Student.id.in_(item.student_ids),
            Student.is_active == True,
        ).all()
        existing_ids = {student.id for student in students}
        missing_ids = [student_id for student_id in item.student_ids if student_id not in existing_ids]
        if missing_ids:
            raise HTTPException(status_code=404, detail=f"找不到在讀學生：{missing_ids}")

        operator_id = current_user.get("user_id")
        now = datetime.now()
        moved_count = 0
        for student in students:
            if student.classroom_id == item.target_classroom_id:
                continue
            session.add(StudentClassroomTransfer(
                student_id=student.id,
                from_classroom_id=student.classroom_id,
                to_classroom_id=item.target_classroom_id,
                transferred_at=now,
                transferred_by=operator_id,
            ))
            student.classroom_id = item.target_classroom_id
            moved_count += 1

        session.commit()
        logger.info(
            "學生批次轉班：target_classroom_id=%s moved=%s operator=%s",
            item.target_classroom_id,
            moved_count,
            current_user.get("username"),
        )
        return {
            "message": "學生轉班成功",
            "moved_count": moved_count,
            "target_classroom_id": item.target_classroom_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"轉班失敗: {str(e)}")
    finally:
        session.close()
