"""新生名額規劃 API：保留座位、名額彙總、計畫名額。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from models.base import session_scope
from models.classroom import ClassGrade
from services.recruitment_intake_plan import (
    IntakePlanError,
    compute_intake_plan,
    set_provisional_seat,
    upsert_intake_targets,
)
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from schemas.recruitment_intake import (
    IntakePlanOut,
    IntakeTargetsIn,
    IntakeTargetsOut,
    ReserveSeatIn,
    ReserveSeatOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment", tags=["recruitment-intake"])


@router.post("/funnel/visits/{visit_id}/reserve-seat", response_model=ReserveSeatOut)
def reserve_seat(
    visit_id: int,
    payload: ReserveSeatIn,
    current_user: dict = Depends(
        require_staff_permission(Permission.RECRUITMENT_WRITE)
    ),
):
    """設定/釋放暫定編班（保留座位）。null grade = 釋放。"""
    try:
        with session_scope() as session:
            try:
                visit = set_provisional_seat(
                    session,
                    visit_id=visit_id,
                    provisional_grade_id=payload.provisional_grade_id,
                    target_school_year=payload.target_school_year,
                    target_semester=(
                        payload.target_semester
                        if payload.target_semester is not None
                        else (1 if payload.provisional_grade_id is not None else None)
                    ),
                    actor_user_id=current_user.get("user_id"),
                )
                grade_name = None
                if visit.provisional_grade_id is not None:
                    g = session.get(ClassGrade, visit.provisional_grade_id)
                    grade_name = g.name if g else None
                out = ReserveSeatOut(
                    visit_id=visit.id,
                    provisional_grade_id=visit.provisional_grade_id,
                    provisional_grade_name=grade_name,
                    target_school_year=visit.target_school_year,
                    target_semester=visit.target_semester,
                )
            except IntakePlanError as e:
                raise HTTPException(status_code=400, detail=str(e))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="保留座位失敗")


@router.get("/intake-plan", response_model=IntakePlanOut)
def get_intake_plan(
    school_year: int = Query(...),
    semester: int = Query(1),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """名額規劃彙總：每年級 計畫/保留/註冊/剩餘。"""
    try:
        with session_scope() as session:
            rows = compute_intake_plan(
                session, school_year=school_year, semester=semester
            )
        return IntakePlanOut(school_year=school_year, semester=semester, rows=rows)
    except Exception as e:
        raise_safe_500(e, context="名額彙總失敗")


@router.put("/intake-targets", response_model=IntakeTargetsOut)
def put_intake_targets(
    payload: IntakeTargetsIn,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """設定每年級計畫名額。"""
    try:
        with session_scope() as session:
            rows = upsert_intake_targets(
                session,
                school_year=payload.school_year,
                semester=payload.semester,
                targets=[t.model_dump() for t in payload.targets],
            )
            out = IntakeTargetsOut(
                school_year=payload.school_year,
                semester=payload.semester,
                targets=[
                    {"grade_id": r.grade_id, "target_seats": r.target_seats}
                    for r in rows
                ],
            )
        return out
    except Exception as e:
        raise_safe_500(e, context="設定計畫名額失敗")
