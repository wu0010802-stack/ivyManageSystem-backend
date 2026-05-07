"""
Portal - 教師事件紀錄端點
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500

from models.database import session_scope, Student, StudentIncident
from utils.auth import get_current_user, require_permission
from utils.error_messages import STUDENT_NOT_FOUND
from utils.permissions import Permission
from utils.record_formatters import incident_to_dict
from utils.validators import validate_incident_fields, parse_date_range_params
from ._shared import _get_employee, _get_teacher_classroom_ids, _get_teacher_student_ids
from api.student_incidents import IncidentCreate as PortalIncidentCreate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/my-class-incidents")
def get_my_class_incidents(
    classroom_id: Optional[int] = Query(None),
    incident_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """教師查看自己班級學生的事件紀錄"""
    try:
        with session_scope() as session:
            emp = _get_employee(session, current_user)
            _, student_ids = _get_teacher_student_ids(
                session,
                emp.id,
                classroom_id,
                forbidden_detail="無權查看此班級的事件紀錄",
            )
            if not student_ids:
                return {"total": 0, "items": []}

            query = (
                session.query(StudentIncident, Student)
                .join(Student, StudentIncident.student_id == Student.id)
                .filter(StudentIncident.student_id.in_(student_ids))
            )

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
                "items": [incident_to_dict(inc, stu) for inc, stu in rows],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="取得事件紀錄失敗")


@router.post("/incidents", status_code=201)
def create_portal_incident(
    payload: PortalIncidentCreate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """教師填寫新事件（只能填寫自己班級的學生）"""
    validate_incident_fields(
        incident_type=payload.incident_type, severity=payload.severity
    )

    try:
        with session_scope() as session:
            emp = _get_employee(session, current_user)
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)

            # F-007：「學生不存在」「不屬於本班」「已停用」一律 generic 403，
            # 避免透過 status code 差異枚舉 Student id 存在性與在學狀態。
            student = (
                session.query(Student)
                .filter(
                    Student.id == payload.student_id,
                    Student.is_active.is_(True),
                )
                .first()
            )
            if not student or student.classroom_id not in classroom_ids:
                raise HTTPException(
                    status_code=403, detail="查無此學生或無權為此學生填寫事件紀錄"
                )

            incident = StudentIncident(
                student_id=payload.student_id,
                incident_type=payload.incident_type,
                severity=payload.severity,
                occurred_at=payload.occurred_at,
                description=payload.description,
                action_taken=payload.action_taken,
                parent_notified=payload.parent_notified,
                parent_notified_at=datetime.now() if payload.parent_notified else None,
                recorded_by=current_user.get("user_id"),
            )
            session.add(incident)
            session.flush()
            session.refresh(incident)

            logger.info(
                "教師新增學生事件紀錄：emp=%s student_id=%d type=%s",
                emp.name,
                payload.student_id,
                payload.incident_type,
            )
            return incident_to_dict(incident, student)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增失敗")
