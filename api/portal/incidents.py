"""
Portal - 教師事件紀錄端點
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel
from sqlalchemy import or_

from models.database import get_session, Student, StudentIncident, Classroom
from utils.auth import get_current_user
from utils.error_messages import STUDENT_NOT_FOUND
from ._shared import _get_employee

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_INCIDENT_TYPES = {"身體健康", "意外受傷", "行為觀察", "其他"}
VALID_SEVERITIES = {"輕微", "中度", "嚴重"}


class PortalIncidentCreate(BaseModel):
    student_id: int
    incident_type: str
    severity: Optional[str] = None
    occurred_at: datetime
    description: str
    action_taken: Optional[str] = None
    parent_notified: bool = False


def _get_teacher_classroom_ids(session, emp_id: int) -> list[int]:
    """取得教師所屬班級 ID 列表"""
    classrooms = session.query(Classroom).filter(
        Classroom.is_active == True,
        or_(
            Classroom.head_teacher_id == emp_id,
            Classroom.assistant_teacher_id == emp_id,
            Classroom.art_teacher_id == emp_id,
        ),
    ).all()
    return [c.id for c in classrooms]


def _incident_to_dict(incident: StudentIncident, student: Student) -> dict:
    return {
        "id": incident.id,
        "student_id": incident.student_id,
        "student_name": student.name if student else None,
        "student_no": student.student_id if student else None,
        "classroom_id": student.classroom_id if student else None,
        "incident_type": incident.incident_type,
        "severity": incident.severity,
        "occurred_at": incident.occurred_at.isoformat() if incident.occurred_at else None,
        "description": incident.description,
        "action_taken": incident.action_taken,
        "parent_notified": incident.parent_notified,
        "parent_notified_at": incident.parent_notified_at.isoformat() if incident.parent_notified_at else None,
        "recorded_by": incident.recorded_by,
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
    }


@router.get("/my-class-incidents")
def get_my_class_incidents(
    classroom_id: Optional[int] = Query(None),
    incident_type: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    """教師查看自己班級學生的事件紀錄"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        if not classroom_ids:
            return {"total": 0, "items": []}

        # 若指定 classroom_id 需確認是自己班級
        if classroom_id:
            if classroom_id not in classroom_ids:
                raise HTTPException(status_code=403, detail="無權查看此班級的事件紀錄")
            target_ids = [classroom_id]
        else:
            target_ids = classroom_ids

        # 取得班級中的學生 IDs
        student_ids = [
            s.id for s in session.query(Student.id).filter(
                Student.classroom_id.in_(target_ids),
                Student.is_active == True,
            ).all()
        ]

        if not student_ids:
            return {"total": 0, "items": []}

        query = (
            session.query(StudentIncident, Student)
            .join(Student, StudentIncident.student_id == Student.id)
            .filter(StudentIncident.student_id.in_(student_ids))
        )

        if incident_type:
            query = query.filter(StudentIncident.incident_type == incident_type)

        if start_date:
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                query = query.filter(StudentIncident.occurred_at >= start_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="start_date 格式錯誤，請使用 YYYY-MM-DD")

        if end_date:
            try:
                end_dt = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S")
                query = query.filter(StudentIncident.occurred_at <= end_dt)
            except ValueError:
                raise HTTPException(status_code=400, detail="end_date 格式錯誤，請使用 YYYY-MM-DD")

        total = query.count()
        rows = query.order_by(StudentIncident.occurred_at.desc()).offset(skip).limit(limit).all()

        return {
            "total": total,
            "items": [_incident_to_dict(inc, stu) for inc, stu in rows],
        }
    finally:
        session.close()


@router.post("/incidents", status_code=201)
def create_portal_incident(
    payload: PortalIncidentCreate,
    current_user: dict = Depends(get_current_user),
):
    """教師填寫新事件（只能填寫自己班級的學生）"""
    if payload.incident_type not in VALID_INCIDENT_TYPES:
        raise HTTPException(status_code=400, detail=f"無效的事件類型，允許值：{VALID_INCIDENT_TYPES}")
    if payload.severity and payload.severity not in VALID_SEVERITIES:
        raise HTTPException(status_code=400, detail=f"無效的嚴重程度，允許值：{VALID_SEVERITIES}")

    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        # 驗證學生屬於教師班級
        student = session.query(Student).filter(Student.id == payload.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)
        if student.classroom_id not in classroom_ids:
            raise HTTPException(status_code=403, detail="無權為此學生填寫事件紀錄")

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
        session.commit()
        session.refresh(incident)

        logger.info("教師新增學生事件紀錄：emp=%s student_id=%d type=%s",
                    emp.name, payload.student_id, payload.incident_type)
        return _incident_to_dict(incident, student)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="新增失敗")
    finally:
        session.close()
