"""
api/student_communications.py — 家長溝通紀錄 CRUD 端點
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from models.database import get_session, Student
from models.student_log import ParentCommunicationLog, COMMUNICATION_TYPES
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/students/communications", tags=["student-communications"]
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CommunicationCreate(BaseModel):
    student_id: int
    communication_date: str  # YYYY-MM-DD
    communication_type: str
    topic: Optional[str] = None
    content: str
    follow_up: Optional[str] = None

    @field_validator("communication_date")
    @classmethod
    def validate_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("communication_date 格式必須為 YYYY-MM-DD")
        return v

    @field_validator("communication_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in COMMUNICATION_TYPES:
            raise ValueError(
                f"communication_type 必須為 {COMMUNICATION_TYPES} 其中之一"
            )
        return v

    @field_validator("content")
    @classmethod
    def validate_content(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("content 不得為空")
        return v.strip()


class CommunicationUpdate(BaseModel):
    communication_date: Optional[str] = None
    communication_type: Optional[str] = None
    topic: Optional[str] = None
    content: Optional[str] = None
    follow_up: Optional[str] = None

    @field_validator("communication_date")
    @classmethod
    def validate_date(cls, v):
        if v is not None:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                raise ValueError("communication_date 格式必須為 YYYY-MM-DD")
        return v

    @field_validator("communication_type")
    @classmethod
    def validate_type(cls, v):
        if v is not None and v not in COMMUNICATION_TYPES:
            raise ValueError(
                f"communication_type 必須為 {COMMUNICATION_TYPES} 其中之一"
            )
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(log: ParentCommunicationLog, student_name: str = "") -> dict:
    return {
        "id": log.id,
        "student_id": log.student_id,
        "student_name": student_name,
        "communication_date": (
            log.communication_date.isoformat() if log.communication_date else None
        ),
        "communication_type": log.communication_type,
        "topic": log.topic,
        "content": log.content,
        "follow_up": log.follow_up,
        "recorded_by": log.recorded_by,
        "created_at": log.created_at.isoformat() if log.created_at else None,
        "updated_at": log.updated_at.isoformat() if log.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/options")
async def get_options(
    _: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得溝通方式選項（供前端下拉）"""
    return {"communication_types": COMMUNICATION_TYPES}


@router.get("")
async def list_communications(
    student_id: Optional[int] = Query(None, gt=0),
    classroom_id: Optional[int] = Query(None, gt=0),
    communication_type: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """查詢家長溝通紀錄（分頁，支援 student_id / classroom_id 過濾）"""
    session = get_session()
    try:
        q = session.query(ParentCommunicationLog)
        if student_id:
            q = q.filter(ParentCommunicationLog.student_id == student_id)
        if classroom_id:
            student_ids_in_class = [
                sid
                for (sid,) in session.query(Student.id)
                .filter(Student.classroom_id == classroom_id)
                .all()
            ]
            if student_ids_in_class:
                q = q.filter(
                    ParentCommunicationLog.student_id.in_(student_ids_in_class)
                )
            else:
                q = q.filter(False)
        if communication_type:
            q = q.filter(
                ParentCommunicationLog.communication_type == communication_type
            )
        if date_from:
            try:
                df = datetime.strptime(date_from, "%Y-%m-%d").date()
                q = q.filter(ParentCommunicationLog.communication_date >= df)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="date_from 格式必須為 YYYY-MM-DD"
                )
        if date_to:
            try:
                dt = datetime.strptime(date_to, "%Y-%m-%d").date()
                q = q.filter(ParentCommunicationLog.communication_date <= dt)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="date_to 格式必須為 YYYY-MM-DD"
                )

        total = q.count()
        logs = (
            q.order_by(
                ParentCommunicationLog.communication_date.desc(),
                ParentCommunicationLog.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        student_ids = {log.student_id for log in logs}
        students_map = (
            {
                s.id: s.name
                for s in session.query(Student)
                .filter(Student.id.in_(student_ids))
                .all()
            }
            if student_ids
            else {}
        )

        return {
            "items": [
                _serialize(log, students_map.get(log.student_id, "")) for log in logs
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    finally:
        session.close()


@router.post("", status_code=201)
async def create_communication(
    item: CommunicationCreate,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """新增家長溝通紀錄"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == item.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到學生")

        log = ParentCommunicationLog(
            student_id=item.student_id,
            communication_date=datetime.strptime(
                item.communication_date, "%Y-%m-%d"
            ).date(),
            communication_type=item.communication_type,
            topic=item.topic,
            content=item.content,
            follow_up=item.follow_up,
            recorded_by=current_user.get("user_id"),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        logger.info(
            "新增家長溝通紀錄：student_id=%s type=%s operator=%s",
            item.student_id,
            item.communication_type,
            current_user.get("username"),
        )
        return _serialize(log, student.name)
    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception("建立家長溝通紀錄失敗")
        raise HTTPException(status_code=500, detail="建立失敗，請稍後再試")
    finally:
        session.close()


@router.put("/{log_id}")
async def update_communication(
    log_id: int,
    item: CommunicationUpdate,
    _: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """編輯家長溝通紀錄"""
    session = get_session()
    try:
        log = (
            session.query(ParentCommunicationLog)
            .filter(ParentCommunicationLog.id == log_id)
            .first()
        )
        if not log:
            raise HTTPException(status_code=404, detail="找不到紀錄")

        data = item.model_dump(exclude_unset=True)
        for key, value in data.items():
            if key == "communication_date" and value:
                setattr(log, key, datetime.strptime(value, "%Y-%m-%d").date())
            else:
                setattr(log, key, value)

        session.commit()
        session.refresh(log)
        student = session.query(Student).filter(Student.id == log.student_id).first()
        return _serialize(log, student.name if student else "")
    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception("更新家長溝通紀錄失敗")
        raise HTTPException(status_code=500, detail="更新失敗，請稍後再試")
    finally:
        session.close()


@router.delete("/{log_id}")
async def delete_communication(
    log_id: int,
    _: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """刪除家長溝通紀錄"""
    session = get_session()
    try:
        log = (
            session.query(ParentCommunicationLog)
            .filter(ParentCommunicationLog.id == log_id)
            .first()
        )
        if not log:
            raise HTTPException(status_code=404, detail="找不到紀錄")
        session.delete(log)
        session.commit()
        return {"message": "已刪除", "id": log_id}
    except HTTPException:
        raise
    except Exception:
        session.rollback()
        logger.exception("刪除家長溝通紀錄失敗")
        raise HTTPException(status_code=500, detail="刪除失敗，請稍後再試")
    finally:
        session.close()
