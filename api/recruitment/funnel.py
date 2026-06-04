"""api/recruitment/funnel.py — Phase A funnel endpoints.

- GET  /board                          → 4-stage Kanban data
- POST /visits/{visit_id}/transition  → state machine driver (with dynamic permission)
- GET  /visits/{visit_id}/timeline    → union of recruitment_event_log + student_change_logs
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.classroom import Student
from models.recruitment import RecruitmentVisit
from schemas.recruitment_funnel import (
    FunnelBoardOut,
    FunnelCard,
    FunnelSummary,
    Stage,
    TransitionIn,
    TransitionOut,
    TimelineOut,
)
from services.recruitment_funnel import (
    transition_visit,
    derive_stage,
    RecruitmentFunnelError,
)
from utils.academic import resolve_current_academic_term
from utils.auth import require_staff_permission, get_current_user
from utils.permissions import Permission, has_permission

router = APIRouter(prefix="/funnel", tags=["recruitment-funnel"])


def _build_funnel_card(visit, student, grade_name_map):
    """把一筆訪視（+ 對應 student）組成看板卡片；純函式、不碰 session。"""
    stage = derive_stage(visit, student)
    return FunnelCard(
        visit_id=visit.id,
        child_name=visit.child_name,
        grade=visit.grade,
        phone=visit.phone,
        district=visit.district,
        source=visit.source,
        deposited_at=visit.updated_at if visit.has_deposit else None,
        student_id=student.id if student else None,
        current_stage=stage,
        provisional_grade_id=visit.provisional_grade_id,
        provisional_grade_name=grade_name_map.get(visit.provisional_grade_id),
        target_school_year=visit.target_school_year,
    )


# === GET /board ===
@router.get("/board", response_model=FunnelBoardOut)
def get_board(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None),
    session: Session = Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """4 階段看板資料。

    Phase A 不做時間範圍過濾 — 抓全部 visits 推導 stage（資料量小可承受）。
    Phase B 若需要過濾，加 month range filter。
    """
    if school_year is None or semester is None:
        sy, sm = resolve_current_academic_term()
        school_year = school_year or sy
        semester = semester or sm

    visits = session.query(RecruitmentVisit).all()
    student_map: dict[int, Student] = {
        s.recruitment_visit_id: s
        for s in (
            session.query(Student)
            .filter(Student.recruitment_visit_id.isnot(None))
            .all()
        )
    }

    from models.classroom import ClassGrade

    grade_name_map: dict[int, str] = {
        g.id: g.name for g in session.query(ClassGrade).all()
    }

    buckets: dict[str, list[FunnelCard]] = {
        "visited": [],
        "deposited": [],
        "enrolled": [],
        "active": [],
    }
    for v in visits:
        student = student_map.get(v.id)
        card = _build_funnel_card(v, student, grade_name_map)
        buckets[card.current_stage].append(card)

    return FunnelBoardOut(
        stages=buckets,
        summary=FunnelSummary(
            visited_count=len(buckets["visited"]),
            deposited_count=len(buckets["deposited"]),
            enrolled_count=len(buckets["enrolled"]),
            active_count=len(buckets["active"]),
        ),
    )


# === Permission helper (exposed for unit tests) ===
def _required_permissions(from_stage: str, to_stage: str) -> list[Permission]:
    """Return the list of Permissions required to execute the given stage transition."""
    if {from_stage, to_stage} == {"visited", "deposited"}:
        return [Permission.RECRUITMENT_WRITE]
    if from_stage == "deposited" and to_stage == "enrolled":
        return [Permission.RECRUITMENT_CONVERT]
    if from_stage == "enrolled" and to_stage in ("deposited", "visited"):
        return [Permission.RECRUITMENT_CONVERT, Permission.STUDENTS_WRITE]
    if {from_stage, to_stage} == {"enrolled", "active"}:
        return [Permission.STUDENTS_WRITE]
    if from_stage == "active" and to_stage in ("enrolled", "deposited", "visited"):
        return [Permission.STUDENTS_WRITE]
    return [Permission.RECRUITMENT_WRITE]


# === POST /visits/{visit_id}/transition ===
@router.post("/visits/{visit_id}/transition", response_model=TransitionOut)
def post_transition(
    visit_id: int,
    payload: TransitionIn,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(get_current_user),
):
    """State machine driver — dynamic permission check based on from/to stage."""
    visit = session.query(RecruitmentVisit).filter_by(id=visit_id).first()
    if visit is None:
        raise HTTPException(
            404, detail={"code": "VISIT_NOT_FOUND", "message": "visit not found"}
        )

    student = (
        session.query(Student).filter(Student.recruitment_visit_id == visit_id).first()
    )
    from_stage = derive_stage(visit, student)

    required = _required_permissions(from_stage, payload.to_stage)
    user_perms = current_user.get("permission_names") or []
    for p in required:
        if not has_permission(user_perms, p):
            raise HTTPException(
                403,
                detail={"code": "PERMISSION_DENIED", "message": f"missing {p.name}"},
            )

    try:
        result = transition_visit(
            session,
            visit_id=visit_id,
            to_stage=payload.to_stage,
            actor_user_id=current_user.get("user_id"),
            classroom_id=payload.classroom_id,
            reason=payload.reason,
        )
    except RecruitmentFunnelError as e:
        status = 409 if e.code == "STAGE_ALREADY" else 400
        raise HTTPException(status, detail={"code": e.code, "message": str(e)})

    return TransitionOut(
        visit_id=result.visit_id,
        from_stage=result.from_stage,
        to_stage=result.to_stage,
        student_id=result.student_id,
        event_log_id=result.event_log_id,
        warnings=result.warnings,
    )


# === GET /visits/{visit_id}/timeline ===
@router.get("/visits/{visit_id}/timeline", response_model=TimelineOut)
def get_timeline(
    visit_id: int,
    session: Session = Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """Union of recruitment_event_log + student_change_logs, sorted by time。

    邏輯已抽到 services.recruitment_timeline.build_visit_timeline（與正確路由端點
    /api/recruitment/visits/{id}/timeline 共用）；此 funnel route 已棄用但保留呼叫同 service。
    """
    from services.recruitment_timeline import (
        build_visit_timeline,
        TimelineNotFound,
    )

    try:
        events = build_visit_timeline(session, visit_id=visit_id)
    except TimelineNotFound:
        raise HTTPException(404, detail={"code": "VISIT_NOT_FOUND"})
    return TimelineOut(events=events)
