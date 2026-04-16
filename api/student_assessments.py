"""
Student assessments router — 學生學期評量記錄（管理端）
"""

import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel

from models.database import session_scope, Student, StudentAssessment, Classroom
from utils.auth import require_permission
from utils.error_messages import STUDENT_NOT_FOUND
from utils.permissions import Permission
from utils.record_formatters import assessment_to_dict
from utils.validators import validate_assessment_fields

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["student-assessments"])


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


def _require_classroom_access(session, current_user: dict, classroom_id: int) -> None:
    """確認操作者有權存取指定班級的學生記錄。

    admin/hr/supervisor 不受限制；其他角色（如教師）只能存取自己擔任
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
    with session_scope() as session:
        query = session.query(StudentAssessment, Student).join(
            Student, StudentAssessment.student_id == Student.id
        )

        if student_id:
            # NV2：對非特權角色（admin/hr/supervisor）驗證學生屬於可存取的班級；
            # classroom_id=NULL（未分班）只允許管理員查看，非管理員一律 403。
            role = current_user.get("role", "")
            if role not in ("admin", "hr", "supervisor"):
                stu = session.query(Student).filter(Student.id == student_id).first()
                if stu is None or stu.classroom_id is None:
                    raise HTTPException(
                        status_code=403, detail="您無權存取此學生的評量記錄"
                    )
                _require_classroom_access(session, current_user, stu.classroom_id)
            query = query.filter(StudentAssessment.student_id == student_id)

        if classroom_id:
            query = query.filter(Student.classroom_id == classroom_id)
            _require_classroom_access(session, current_user, classroom_id)

        if semester:
            query = query.filter(StudentAssessment.semester == semester)

        if assessment_type:
            query = query.filter(StudentAssessment.assessment_type == assessment_type)

        total = query.count()
        rows = (
            query.order_by(StudentAssessment.assessment_date.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        return {
            "total": total,
            "items": [
                assessment_to_dict(a, s, include_updated_at=True) for a, s in rows
            ],
        }


@router.post("/student-assessments", status_code=201)
async def create_assessment(
    payload: AssessmentCreate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """新增學生評量記錄"""
    validate_assessment_fields(
        assessment_type=payload.assessment_type,
        domain=payload.domain,
        rating=payload.rating,
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
            session.flush()
            session.refresh(assessment)

            logger.info(
                "新增學生評量記錄：student_id=%d semester=%s type=%s operator=%s",
                payload.student_id,
                payload.semester,
                payload.assessment_type,
                current_user.get("username"),
            )
            return assessment_to_dict(assessment, student, include_updated_at=True)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增失敗")


@router.put("/student-assessments/{assessment_id}")
async def update_assessment(
    assessment_id: int,
    payload: AssessmentUpdate,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """更新學生評量記錄"""
    try:
        with session_scope() as session:
            assessment = (
                session.query(StudentAssessment)
                .filter(StudentAssessment.id == assessment_id)
                .first()
            )
            if not assessment:
                raise HTTPException(status_code=404, detail="找不到該評量記錄")

            student = (
                session.query(Student)
                .filter(Student.id == assessment.student_id)
                .first()
            )
            if student and student.classroom_id:
                _require_classroom_access(session, current_user, student.classroom_id)

            validate_assessment_fields(
                assessment_type=payload.assessment_type,
                domain=payload.domain,
                rating=payload.rating,
            )

            for field, value in payload.model_dump(exclude_unset=True).items():
                if value is not None:
                    setattr(assessment, field, value)

            assessment.updated_at = datetime.now()
            session.flush()
            session.refresh(assessment)

            logger.info(
                "更新學生評量記錄：id=%d operator=%s",
                assessment_id,
                current_user.get("username"),
            )
            return assessment_to_dict(assessment, student, include_updated_at=True)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="更新失敗")


@router.delete("/student-assessments/{assessment_id}")
async def delete_assessment(
    assessment_id: int,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """刪除學生評量記錄"""
    try:
        with session_scope() as session:
            assessment = (
                session.query(StudentAssessment)
                .filter(StudentAssessment.id == assessment_id)
                .first()
            )
            if not assessment:
                raise HTTPException(status_code=404, detail="找不到該評量記錄")

            student_for_access = (
                session.query(Student)
                .filter(Student.id == assessment.student_id)
                .first()
            )
            if student_for_access and student_for_access.classroom_id:
                _require_classroom_access(
                    session, current_user, student_for_access.classroom_id
                )

            student_id_for_log = assessment.student_id
            session.delete(assessment)
            logger.warning(
                "刪除學生評量記錄：id=%d student_id=%d operator=%s",
                assessment_id,
                student_id_for_log,
                current_user.get("username"),
            )
            return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="刪除失敗")
