"""
api/student_change_logs.py — 學生異動紀錄 CRUD 端點
"""

import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from models.database import get_session, Student, Classroom
from models.student_log import StudentChangeLog, CHANGE_LOG_REASON_OPTIONS, EVENT_TYPES
from utils.academic import resolve_academic_term_filters
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/students/change-logs", tags=["student-change-logs"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChangeLogCreate(BaseModel):
    student_id: int
    school_year: int
    semester: int
    event_type: str
    event_date: str  # "YYYY-MM-DD"
    classroom_id: Optional[int] = None
    from_classroom_id: Optional[int] = None
    to_classroom_id: Optional[int] = None
    reason: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v):
        if v not in EVENT_TYPES:
            raise ValueError(f"event_type 必須為 {EVENT_TYPES} 其中之一")
        return v

    @field_validator("event_date")
    @classmethod
    def validate_date(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("event_date 格式必須為 YYYY-MM-DD")
        return v

    @field_validator("school_year")
    @classmethod
    def validate_school_year(cls, v):
        if not (100 <= v <= 200):
            raise ValueError("school_year 應為民國年（100~200）")
        return v

    @field_validator("semester")
    @classmethod
    def validate_semester(cls, v):
        if v not in (1, 2):
            raise ValueError("semester 必須為 1 或 2")
        return v


class ChangeLogUpdate(BaseModel):
    event_type: Optional[str] = None
    event_date: Optional[str] = None
    classroom_id: Optional[int] = None
    from_classroom_id: Optional[int] = None
    to_classroom_id: Optional[int] = None
    reason: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v):
        if v is not None and v not in EVENT_TYPES:
            raise ValueError(f"event_type 必須為 {EVENT_TYPES} 其中之一")
        return v

    @field_validator("event_date")
    @classmethod
    def validate_date(cls, v):
        if v is not None:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                raise ValueError("event_date 格式必須為 YYYY-MM-DD")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_log(
    log: StudentChangeLog, student_name: str, classroom_name: str
) -> dict:
    return {
        "id": log.id,
        "student_id": log.student_id,
        "student_name": student_name,
        "school_year": log.school_year,
        "semester": log.semester,
        "event_type": log.event_type,
        "event_date": log.event_date.isoformat() if log.event_date else None,
        "classroom_id": log.classroom_id,
        "classroom_name": classroom_name,
        "from_classroom_id": log.from_classroom_id,
        "to_classroom_id": log.to_classroom_id,
        "reason": log.reason,
        "notes": log.notes,
        "recorded_by": log.recorded_by,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/options")
async def get_change_log_options(
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得 event_type 與對應 reason 選項（供前端下拉）"""
    return {
        "event_types": EVENT_TYPES,
        "reason_options": CHANGE_LOG_REASON_OPTIONS,
    }


@router.get("/summary")
async def get_change_logs_summary(
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得指定學期各異動類型數量統計"""
    resolved_year, resolved_semester = resolve_academic_term_filters(
        school_year, semester
    )
    session = get_session()
    try:
        logs = (
            session.query(StudentChangeLog)
            .filter(
                StudentChangeLog.school_year == resolved_year,
                StudentChangeLog.semester == resolved_semester,
            )
            .all()
        )
        summary = {et: 0 for et in EVENT_TYPES}
        for log in logs:
            if log.event_type in summary:
                summary[log.event_type] += 1

        return {
            "school_year": resolved_year,
            "semester": resolved_semester,
            "summary": summary,
            "total": len(logs),
        }
    finally:
        session.close()


@router.get("")
async def get_change_logs(
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    event_type: Optional[List[str]] = Query(None),
    classroom_id: Optional[int] = Query(None),
    student_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得學生異動紀錄列表（分頁）"""
    resolved_year, resolved_semester = resolve_academic_term_filters(
        school_year, semester
    )
    session = get_session()
    try:
        query = session.query(StudentChangeLog).filter(
            StudentChangeLog.school_year == resolved_year,
            StudentChangeLog.semester == resolved_semester,
        )
        if event_type:
            query = query.filter(StudentChangeLog.event_type.in_(event_type))
        if classroom_id:
            query = query.filter(StudentChangeLog.classroom_id == classroom_id)
        if student_id:
            query = query.filter(StudentChangeLog.student_id == student_id)

        total = query.count()
        logs = (
            query.order_by(
                StudentChangeLog.event_date.desc(), StudentChangeLog.id.desc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        # 批次取得學生姓名與班級名稱
        student_ids = {log.student_id for log in logs}
        classroom_ids = {log.classroom_id for log in logs if log.classroom_id}
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
        classrooms_map = (
            {
                c.id: c.name
                for c in session.query(Classroom)
                .filter(Classroom.id.in_(classroom_ids))
                .all()
            }
            if classroom_ids
            else {}
        )

        items = [
            _serialize_log(
                log,
                students_map.get(log.student_id, ""),
                classrooms_map.get(log.classroom_id, ""),
            )
            for log in logs
        ]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "school_year": resolved_year,
            "semester": resolved_semester,
        }
    finally:
        session.close()


@router.post("", status_code=201)
async def create_change_log(
    item: ChangeLogCreate,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """手動補登學生異動紀錄"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == item.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到學生")

        log = StudentChangeLog(
            student_id=item.student_id,
            school_year=item.school_year,
            semester=item.semester,
            event_type=item.event_type,
            event_date=datetime.strptime(item.event_date, "%Y-%m-%d").date(),
            classroom_id=item.classroom_id,
            from_classroom_id=item.from_classroom_id,
            to_classroom_id=item.to_classroom_id,
            reason=item.reason,
            notes=item.notes,
            recorded_by=current_user.get("user_id"),
        )
        session.add(log)
        session.commit()
        logger.info(
            "手動補登學生異動：student_id=%s event_type=%s operator=%s",
            item.student_id,
            item.event_type,
            current_user.get("username"),
        )
        return {"message": "異動紀錄新增成功", "id": log.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("建立異動紀錄失敗")
        raise HTTPException(status_code=500, detail="建立失敗，請稍後再試")
    finally:
        session.close()


@router.put("/{log_id}")
async def update_change_log(
    log_id: int,
    item: ChangeLogUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """編輯異動紀錄"""
    session = get_session()
    try:
        log = (
            session.query(StudentChangeLog)
            .filter(StudentChangeLog.id == log_id)
            .first()
        )
        if not log:
            raise HTTPException(status_code=404, detail="找不到異動紀錄")

        update_data = item.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if key == "event_date" and value:
                setattr(log, key, datetime.strptime(value, "%Y-%m-%d").date())
            else:
                setattr(log, key, value)

        session.commit()
        return {"message": "異動紀錄已更新", "id": log_id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("更新異動紀錄失敗")
        raise HTTPException(status_code=500, detail="更新失敗，請稍後再試")
    finally:
        session.close()


@router.delete("/{log_id}")
async def delete_change_log(
    log_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """刪除異動紀錄"""
    session = get_session()
    try:
        log = (
            session.query(StudentChangeLog)
            .filter(StudentChangeLog.id == log_id)
            .first()
        )
        if not log:
            raise HTTPException(status_code=404, detail="找不到異動紀錄")

        session.delete(log)
        session.commit()
        return {"message": "異動紀錄已刪除", "id": log_id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("刪除異動紀錄失敗")
        raise HTTPException(status_code=500, detail="刪除失敗，請稍後再試")
    finally:
        session.close()
