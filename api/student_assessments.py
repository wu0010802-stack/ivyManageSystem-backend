"""
Student assessments router — 學生學期評量記錄（管理端）
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel

from models.database import get_session, Student, StudentAssessment
from utils.auth import require_permission
from utils.error_messages import STUDENT_NOT_FOUND
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["student-assessments"])

VALID_ASSESSMENT_TYPES = {"期中", "期末", "學期"}
VALID_DOMAINS = {"身體動作與健康", "語文", "認知", "社會", "情緒", "美感", "綜合"}
VALID_RATINGS = {"優", "良", "需加強"}


# ============ Pydantic Models ============

class AssessmentCreate(BaseModel):
    student_id: int
    semester: str
    assessment_type: str
    domain: Optional[str] = None
    rating: Optional[str] = None
    content: str
    suggestions: Optional[str] = None
    assessment_date: date


class AssessmentUpdate(BaseModel):
    semester: Optional[str] = None
    assessment_type: Optional[str] = None
    domain: Optional[str] = None
    rating: Optional[str] = None
    content: Optional[str] = None
    suggestions: Optional[str] = None
    assessment_date: Optional[date] = None


# ============ Helpers ============

def _assessment_to_dict(assessment: StudentAssessment, student: Student) -> dict:
    return {
        "id": assessment.id,
        "student_id": assessment.student_id,
        "student_name": student.name if student else None,
        "student_no": student.student_id if student else None,
        "classroom_id": student.classroom_id if student else None,
        "semester": assessment.semester,
        "assessment_type": assessment.assessment_type,
        "domain": assessment.domain,
        "rating": assessment.rating,
        "content": assessment.content,
        "suggestions": assessment.suggestions,
        "assessment_date": assessment.assessment_date.isoformat() if assessment.assessment_date else None,
        "recorded_by": assessment.recorded_by,
        "created_at": assessment.created_at.isoformat() if assessment.created_at else None,
        "updated_at": assessment.updated_at.isoformat() if assessment.updated_at else None,
    }


# ============ Routes ============

@router.get("/student-assessments")
async def list_assessments(
    student_id: Optional[int] = Query(None),
    classroom_id: Optional[int] = Query(None),
    semester: Optional[str] = Query(None),
    assessment_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """列表查詢學生評量記錄"""
    session = get_session()
    try:
        query = (
            session.query(StudentAssessment, Student)
            .join(Student, StudentAssessment.student_id == Student.id)
        )

        if student_id:
            query = query.filter(StudentAssessment.student_id == student_id)

        if classroom_id:
            query = query.filter(Student.classroom_id == classroom_id)

        if semester:
            query = query.filter(StudentAssessment.semester == semester)

        if assessment_type:
            query = query.filter(StudentAssessment.assessment_type == assessment_type)

        total = query.count()
        rows = query.order_by(StudentAssessment.assessment_date.desc()).offset(skip).limit(limit).all()

        return {
            "total": total,
            "items": [_assessment_to_dict(a, s) for a, s in rows],
        }
    finally:
        session.close()


@router.post("/student-assessments", status_code=201)
async def create_assessment(
    payload: AssessmentCreate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """新增學生評量記錄"""
    if payload.assessment_type not in VALID_ASSESSMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"無效的評量類型，允許值：{VALID_ASSESSMENT_TYPES}")
    if payload.domain and payload.domain not in VALID_DOMAINS:
        raise HTTPException(status_code=400, detail=f"無效的領域，允許值：{VALID_DOMAINS}")
    if payload.rating and payload.rating not in VALID_RATINGS:
        raise HTTPException(status_code=400, detail=f"無效的評等，允許值：{VALID_RATINGS}")

    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == payload.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)

        assessment = StudentAssessment(
            student_id=payload.student_id,
            semester=payload.semester,
            assessment_type=payload.assessment_type,
            domain=payload.domain,
            rating=payload.rating,
            content=payload.content,
            suggestions=payload.suggestions,
            assessment_date=payload.assessment_date,
            recorded_by=current_user.get("id"),
        )
        session.add(assessment)
        session.commit()
        session.refresh(assessment)

        logger.info("新增學生評量記錄：student_id=%d semester=%s type=%s operator=%s",
                    payload.student_id, payload.semester, payload.assessment_type,
                    current_user.get("username"))
        return _assessment_to_dict(assessment, student)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="新增失敗")
    finally:
        session.close()


@router.put("/student-assessments/{assessment_id}")
async def update_assessment(
    assessment_id: int,
    payload: AssessmentUpdate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """更新學生評量記錄"""
    session = get_session()
    try:
        assessment = session.query(StudentAssessment).filter(StudentAssessment.id == assessment_id).first()
        if not assessment:
            raise HTTPException(status_code=404, detail="找不到該評量記錄")

        if payload.assessment_type is not None:
            if payload.assessment_type not in VALID_ASSESSMENT_TYPES:
                raise HTTPException(status_code=400, detail=f"無效的評量類型，允許值：{VALID_ASSESSMENT_TYPES}")
            assessment.assessment_type = payload.assessment_type

        if payload.domain is not None:
            if payload.domain and payload.domain not in VALID_DOMAINS:
                raise HTTPException(status_code=400, detail=f"無效的領域，允許值：{VALID_DOMAINS}")
            assessment.domain = payload.domain

        if payload.rating is not None:
            if payload.rating and payload.rating not in VALID_RATINGS:
                raise HTTPException(status_code=400, detail=f"無效的評等，允許值：{VALID_RATINGS}")
            assessment.rating = payload.rating

        if payload.semester is not None:
            assessment.semester = payload.semester
        if payload.content is not None:
            assessment.content = payload.content
        if payload.suggestions is not None:
            assessment.suggestions = payload.suggestions
        if payload.assessment_date is not None:
            assessment.assessment_date = payload.assessment_date

        from datetime import datetime
        assessment.updated_at = datetime.now()
        session.commit()
        session.refresh(assessment)

        student = session.query(Student).filter(Student.id == assessment.student_id).first()
        logger.info("更新學生評量記錄：id=%d operator=%s", assessment_id, current_user.get("username"))
        return _assessment_to_dict(assessment, student)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="更新失敗")
    finally:
        session.close()


@router.delete("/student-assessments/{assessment_id}")
async def delete_assessment(
    assessment_id: int,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """刪除學生評量記錄"""
    session = get_session()
    try:
        assessment = session.query(StudentAssessment).filter(StudentAssessment.id == assessment_id).first()
        if not assessment:
            raise HTTPException(status_code=404, detail="找不到該評量記錄")

        session.delete(assessment)
        session.commit()
        logger.warning("刪除學生評量記錄：id=%d student_id=%d operator=%s",
                       assessment_id, assessment.student_id, current_user.get("username"))
        return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="刪除失敗")
    finally:
        session.close()
