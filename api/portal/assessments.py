"""
Portal - 教師學期評量端點
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500

from models.database import session_scope, Student, StudentAssessment
from utils.auth import get_current_user, require_permission
from utils.error_messages import STUDENT_NOT_FOUND
from utils.permissions import Permission
from utils.record_formatters import assessment_to_dict
from utils.validators import validate_assessment_fields
from ._shared import _get_employee, _get_teacher_classroom_ids, _get_teacher_student_ids
from api.student_assessments import AssessmentCreate as PortalAssessmentCreate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/my-class-assessments")
def get_my_class_assessments(
    classroom_id: Optional[int] = Query(None),
    semester: Optional[str] = Query(None),
    assessment_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """教師查看自己班級學生的評量記錄"""
    try:
        with session_scope() as session:
            emp = _get_employee(session, current_user)
            _, student_ids = _get_teacher_student_ids(
                session,
                emp.id,
                classroom_id,
                forbidden_detail="無權查看此班級的評量記錄",
            )
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
                query = query.filter(
                    StudentAssessment.assessment_type == assessment_type
                )

            total = query.count()
            rows = (
                query.order_by(StudentAssessment.assessment_date.desc())
                .offset(skip)
                .limit(limit)
                .all()
            )

            return {
                "total": total,
                "items": [assessment_to_dict(a, s) for a, s in rows],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="取得評量記錄失敗")


@router.post("/assessments", status_code=201)
def create_portal_assessment(
    payload: PortalAssessmentCreate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """教師填寫新評量（只能填寫自己班級的學生）"""
    validate_assessment_fields(
        assessment_type=payload.assessment_type,
        domain=payload.domain,
        rating=payload.rating,
    )

    try:
        with session_scope() as session:
            emp = _get_employee(session, current_user)
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)

            # F-008：「學生不存在」「不屬於本班」「已停用」一律 generic 403。
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
                    status_code=403, detail="查無此學生或無權為此學生填寫評量記錄"
                )

            assessment = StudentAssessment(
                student_id=payload.student_id,
                semester=payload.semester,
                assessment_type=payload.assessment_type,
                domain=payload.domain,
                rating=payload.rating,
                content=payload.content,
                suggestions=payload.suggestions,
                assessment_date=payload.assessment_date,
                recorded_by=current_user.get("user_id"),
            )
            session.add(assessment)
            session.flush()
            session.refresh(assessment)

            logger.info(
                "教師新增學生評量記錄：emp=%s student_id=%d semester=%s type=%s",
                emp.name,
                payload.student_id,
                payload.semester,
                payload.assessment_type,
            )
            return assessment_to_dict(assessment, student)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增失敗")
