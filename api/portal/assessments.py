"""
Portal - 教師學期評量端點
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from models.database import get_session, Student, StudentAssessment, Classroom
from utils.auth import get_current_user
from ._shared import _get_employee
from .incidents import _get_teacher_classroom_ids

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_ASSESSMENT_TYPES = {"期中", "期末", "學期"}
VALID_DOMAINS = {"身體動作與健康", "語文", "認知", "社會", "情緒", "美感", "綜合"}
VALID_RATINGS = {"優", "良", "需加強"}


class PortalAssessmentCreate(BaseModel):
    student_id: int
    semester: str
    assessment_type: str
    domain: Optional[str] = None
    rating: Optional[str] = None
    content: str
    suggestions: Optional[str] = None
    assessment_date: date


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
    }


@router.get("/my-class-assessments")
def get_my_class_assessments(
    classroom_id: Optional[int] = Query(None),
    semester: Optional[str] = Query(None),
    assessment_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    """教師查看自己班級學生的評量記錄"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        if not classroom_ids:
            return {"total": 0, "items": []}

        if classroom_id:
            if classroom_id not in classroom_ids:
                raise HTTPException(status_code=403, detail="無權查看此班級的評量記錄")
            target_ids = [classroom_id]
        else:
            target_ids = classroom_ids

        student_ids = [
            s.id for s in session.query(Student.id).filter(
                Student.classroom_id.in_(target_ids),
                Student.is_active == True,
            ).all()
        ]

        if not student_ids:
            return {"total": 0, "items": []}

        query = (
            session.query(StudentAssessment, Student)
            .join(Student, StudentAssessment.student_id == Student.id)
            .filter(StudentAssessment.student_id.in_(student_ids))
        )

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


@router.post("/assessments", status_code=201)
def create_portal_assessment(
    payload: PortalAssessmentCreate,
    current_user: dict = Depends(get_current_user),
):
    """教師填寫新評量（只能填寫自己班級的學生）"""
    if payload.assessment_type not in VALID_ASSESSMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"無效的評量類型，允許值：{VALID_ASSESSMENT_TYPES}")
    if payload.domain and payload.domain not in VALID_DOMAINS:
        raise HTTPException(status_code=400, detail=f"無效的領域，允許值：{VALID_DOMAINS}")
    if payload.rating and payload.rating not in VALID_RATINGS:
        raise HTTPException(status_code=400, detail=f"無效的評等，允許值：{VALID_RATINGS}")

    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        student = session.query(Student).filter(Student.id == payload.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")
        if student.classroom_id not in classroom_ids:
            raise HTTPException(status_code=403, detail="無權為此學生填寫評量記錄")

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

        logger.info("教師新增學生評量記錄：emp=%s student_id=%d semester=%s type=%s",
                    emp.name, payload.student_id, payload.semester, payload.assessment_type)
        return _assessment_to_dict(assessment, student)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()
