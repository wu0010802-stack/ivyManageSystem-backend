"""
Student incidents router — 學生事件紀錄（管理端）
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel

from models.database import session_scope, Student, StudentIncident, Classroom
from utils.auth import require_permission
from utils.error_messages import STUDENT_NOT_FOUND
from utils.permissions import Permission
from utils.portfolio_access import is_unrestricted, student_ids_in_scope
from utils.record_formatters import incident_to_dict
from utils.validators import validate_incident_fields, parse_date_range_params

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["student-incidents"])


# ============ Pydantic Models ============


class IncidentCreate(BaseModel):
    student_id: int
    incident_type: str
    severity: Optional[str] = None
    occurred_at: datetime
    description: str
    action_taken: Optional[str] = None
    parent_notified: bool = False


class IncidentUpdate(BaseModel):
    incident_type: Optional[str] = None
    severity: Optional[str] = None
    occurred_at: Optional[datetime] = None
    description: Optional[str] = None
    action_taken: Optional[str] = None
    parent_notified: Optional[bool] = None
    parent_notified_at: Optional[datetime] = None


_INCIDENT_SIMPLE_FIELDS = frozenset(
    {"incident_type", "severity", "occurred_at", "description", "action_taken"}
)


# ============ Helpers ============


def _require_classroom_access(session, current_user: dict, classroom_id: int) -> None:
    """確認操作者有權存取指定班級的學生記錄。

    admin/hr/supervisor 不受限制；其他角色只能存取自己擔任
    正/副/英語教師的班級。
    """
    role = current_user.get("role", "")
    if role in ("admin", "hr", "supervisor"):
        return
    emp_id = current_user.get("employee_id")
    if not emp_id:
        raise HTTPException(status_code=403, detail="您無權存取此班級的學生記錄")
    cls = (
        session.query(Classroom)
        .filter(
            Classroom.id == classroom_id,
            (Classroom.head_teacher_id == emp_id)
            | (Classroom.assistant_teacher_id == emp_id)
            | (Classroom.art_teacher_id == emp_id),
        )
        .first()
    )
    if not cls:
        raise HTTPException(status_code=403, detail="您無權存取此班級的學生記錄")


# ============ Routes ============


@router.get("/student-incidents")
async def list_incidents(
    student_id: Optional[int] = Query(None),
    classroom_id: Optional[int] = Query(None),
    incident_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """列表查詢學生事件紀錄"""
    with session_scope() as session:
        query = session.query(StudentIncident, Student).join(
            Student, StudentIncident.student_id == Student.id
        )

        # F-023：student_id / classroom_id 都未帶時，非 is_unrestricted caller
        # 自動限縮至自己班級的學生（雙重防線：保留下方既有 helper 呼叫）
        if not student_id and not classroom_id and not is_unrestricted(current_user):
            scope = student_ids_in_scope(session, current_user)
            if not scope:
                return {"total": 0, "items": []}
            query = query.filter(StudentIncident.student_id.in_(scope))

        if student_id:
            # NV1：驗證操作者有權存取該學生所屬班級
            role = current_user.get("role", "")
            if role not in ("admin", "hr", "supervisor"):
                stu = session.query(Student).filter(Student.id == student_id).first()
                if stu is None or stu.classroom_id is None:
                    raise HTTPException(
                        status_code=403, detail="您無權存取此學生的事件紀錄"
                    )
                _require_classroom_access(session, current_user, stu.classroom_id)
            query = query.filter(StudentIncident.student_id == student_id)

        if classroom_id:
            query = query.filter(Student.classroom_id == classroom_id)
            _require_classroom_access(session, current_user, classroom_id)

        if incident_type:
            query = query.filter(StudentIncident.incident_type == incident_type)

        start_dt, end_dt = parse_date_range_params(start_date, end_date)
        if start_dt:
            query = query.filter(StudentIncident.occurred_at >= start_dt)
        if end_dt:
            query = query.filter(StudentIncident.occurred_at <= end_dt)

        total = query.count()
        rows = (
            query.order_by(StudentIncident.occurred_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        return {
            "total": total,
            "items": [
                incident_to_dict(inc, stu, include_updated_at=True) for inc, stu in rows
            ],
        }


@router.post("/student-incidents", status_code=201)
async def create_incident(
    payload: IncidentCreate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """新增學生事件紀錄"""
    validate_incident_fields(
        incident_type=payload.incident_type, severity=payload.severity
    )

    try:
        with session_scope() as session:
            student = (
                session.query(Student).filter(Student.id == payload.student_id).first()
            )
            if not student:
                raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)
            if student.classroom_id:
                _require_classroom_access(session, current_user, student.classroom_id)

            incident = StudentIncident(
                student_id=payload.student_id,
                incident_type=payload.incident_type,
                severity=payload.severity,
                occurred_at=payload.occurred_at,
                description=payload.description,
                action_taken=payload.action_taken,
                parent_notified=payload.parent_notified,
                parent_notified_at=datetime.now() if payload.parent_notified else None,
                recorded_by=current_user.get("id"),
            )
            session.add(incident)
            session.flush()
            session.refresh(incident)

            logger.info(
                "新增學生事件紀錄：student_id=%d type=%s operator=%s",
                payload.student_id,
                payload.incident_type,
                current_user.get("username"),
            )
            return incident_to_dict(incident, student, include_updated_at=True)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增失敗")


@router.put("/student-incidents/{incident_id}")
async def update_incident(
    incident_id: int,
    payload: IncidentUpdate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """更新學生事件紀錄（含標記已通知家長）"""
    try:
        with session_scope() as session:
            incident = (
                session.query(StudentIncident)
                .filter(StudentIncident.id == incident_id)
                .first()
            )
            if not incident:
                raise HTTPException(status_code=404, detail="找不到該事件紀錄")

            student = (
                session.query(Student).filter(Student.id == incident.student_id).first()
            )
            if student and student.classroom_id:
                _require_classroom_access(session, current_user, student.classroom_id)

            validate_incident_fields(
                incident_type=payload.incident_type, severity=payload.severity
            )

            for field, value in payload.model_dump(exclude_unset=True).items():
                if field in _INCIDENT_SIMPLE_FIELDS and value is not None:
                    setattr(incident, field, value)

            if payload.parent_notified is not None:
                prev_notified = incident.parent_notified
                incident.parent_notified = payload.parent_notified
                if payload.parent_notified and not prev_notified:
                    incident.parent_notified_at = (
                        payload.parent_notified_at or datetime.now()
                    )
                elif not payload.parent_notified:
                    incident.parent_notified_at = None

            if payload.parent_notified_at is not None:
                incident.parent_notified_at = payload.parent_notified_at

            incident.updated_at = datetime.now()
            session.flush()
            session.refresh(incident)

            logger.info(
                "更新學生事件紀錄：id=%d operator=%s",
                incident_id,
                current_user.get("username"),
            )
            return incident_to_dict(incident, student, include_updated_at=True)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="更新失敗")


@router.delete("/student-incidents/{incident_id}")
async def delete_incident(
    incident_id: int,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """刪除學生事件紀錄"""
    try:
        with session_scope() as session:
            incident = (
                session.query(StudentIncident)
                .filter(StudentIncident.id == incident_id)
                .first()
            )
            if not incident:
                raise HTTPException(status_code=404, detail="找不到該事件紀錄")

            student_for_access = (
                session.query(Student).filter(Student.id == incident.student_id).first()
            )
            if student_for_access and student_for_access.classroom_id:
                _require_classroom_access(
                    session, current_user, student_for_access.classroom_id
                )

            student_id_for_log = incident.student_id
            session.delete(incident)
            logger.warning(
                "刪除學生事件紀錄：id=%d student_id=%d operator=%s",
                incident_id,
                student_id_for_log,
                current_user.get("username"),
            )
            return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="刪除失敗")
