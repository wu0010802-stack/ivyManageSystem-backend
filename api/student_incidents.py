"""
Student incidents router — 學生事件紀錄（管理端）
"""

import logging
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from models.database import get_session, Student, StudentIncident, Classroom
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["student-incidents"])

VALID_INCIDENT_TYPES = {"身體健康", "意外受傷", "行為觀察", "其他"}
VALID_SEVERITIES = {"輕微", "中度", "嚴重"}


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


# ============ Helpers ============

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
        "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
    }


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
    session = get_session()
    try:
        query = (
            session.query(StudentIncident, Student)
            .join(Student, StudentIncident.student_id == Student.id)
        )

        if student_id:
            query = query.filter(StudentIncident.student_id == student_id)

        if classroom_id:
            query = query.filter(Student.classroom_id == classroom_id)

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


@router.post("/student-incidents", status_code=201)
async def create_incident(
    payload: IncidentCreate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """新增學生事件紀錄"""
    if payload.incident_type not in VALID_INCIDENT_TYPES:
        raise HTTPException(status_code=400, detail=f"無效的事件類型，允許值：{VALID_INCIDENT_TYPES}")
    if payload.severity and payload.severity not in VALID_SEVERITIES:
        raise HTTPException(status_code=400, detail=f"無效的嚴重程度，允許值：{VALID_SEVERITIES}")

    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == payload.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")

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

        logger.info("新增學生事件紀錄：student_id=%d type=%s operator=%s",
                    payload.student_id, payload.incident_type, current_user.get("username"))
        return _incident_to_dict(incident, student)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()


@router.put("/student-incidents/{incident_id}")
async def update_incident(
    incident_id: int,
    payload: IncidentUpdate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """更新學生事件紀錄（含標記已通知家長）"""
    session = get_session()
    try:
        incident = session.query(StudentIncident).filter(StudentIncident.id == incident_id).first()
        if not incident:
            raise HTTPException(status_code=404, detail="找不到該事件紀錄")

        if payload.incident_type is not None:
            if payload.incident_type not in VALID_INCIDENT_TYPES:
                raise HTTPException(status_code=400, detail=f"無效的事件類型，允許值：{VALID_INCIDENT_TYPES}")
            incident.incident_type = payload.incident_type

        if payload.severity is not None:
            if payload.severity and payload.severity not in VALID_SEVERITIES:
                raise HTTPException(status_code=400, detail=f"無效的嚴重程度，允許值：{VALID_SEVERITIES}")
            incident.severity = payload.severity

        if payload.occurred_at is not None:
            incident.occurred_at = payload.occurred_at

        if payload.description is not None:
            incident.description = payload.description

        if payload.action_taken is not None:
            incident.action_taken = payload.action_taken

        if payload.parent_notified is not None:
            prev_notified = incident.parent_notified
            incident.parent_notified = payload.parent_notified
            if payload.parent_notified and not prev_notified:
                incident.parent_notified_at = payload.parent_notified_at or datetime.now()
            elif not payload.parent_notified:
                incident.parent_notified_at = None

        if payload.parent_notified_at is not None:
            incident.parent_notified_at = payload.parent_notified_at

        incident.updated_at = datetime.now()
        session.commit()
        session.refresh(incident)

        student = session.query(Student).filter(Student.id == incident.student_id).first()
        logger.info("更新學生事件紀錄：id=%d operator=%s", incident_id, current_user.get("username"))
        return _incident_to_dict(incident, student)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@router.delete("/student-incidents/{incident_id}")
async def delete_incident(
    incident_id: int,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """刪除學生事件紀錄"""
    session = get_session()
    try:
        incident = session.query(StudentIncident).filter(StudentIncident.id == incident_id).first()
        if not incident:
            raise HTTPException(status_code=404, detail="找不到該事件紀錄")

        session.delete(incident)
        session.commit()
        logger.warning("刪除學生事件紀錄：id=%d student_id=%d operator=%s",
                       incident_id, incident.student_id, current_user.get("username"))
        return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"刪除失敗: {str(e)}")
    finally:
        session.close()
