"""api/recruitment/funnel.py — Phase A funnel endpoints.

- GET  /board                          → 4-stage Kanban data
- POST /visits/{visit_id}/transition  → state machine driver (with dynamic permission)
- GET  /visits/{visit_id}/timeline    → union of recruitment_event_log + student_change_logs
"""

from datetime import datetime as _dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.classroom import Student
from models.recruitment import RecruitmentVisit, RecruitmentEventLog
from models.student_log import StudentChangeLog
from schemas.recruitment_funnel import (
    FunnelBoardOut,
    FunnelCard,
    FunnelSummary,
    Stage,
    TransitionIn,
    TransitionOut,
    TimelineEvent,
    TimelineOut,
)
from services.recruitment_funnel import (
    transition_visit,
    derive_stage,
    RecruitmentFunnelError,
)
from utils.academic import resolve_current_academic_term
from utils.auth import require_staff_permission, get_current_user
from utils.permissions import Permission

router = APIRouter(prefix="/funnel", tags=["recruitment-funnel"])


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

    buckets: dict[str, list[FunnelCard]] = {
        "visited": [],
        "deposited": [],
        "enrolled": [],
        "active": [],
    }
    for v in visits:
        student = student_map.get(v.id)
        stage = derive_stage(v, student)
        buckets[stage].append(
            FunnelCard(
                visit_id=v.id,
                child_name=v.child_name,
                grade=v.grade,
                phone=v.phone,
                district=v.district,
                source=v.source,
                deposited_at=v.updated_at if v.has_deposit else None,
                student_id=student.id if student else None,
                current_stage=stage,
            )
        )

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
    user_perms = current_user.get("permissions", 0)
    for p in required:
        if not (int(user_perms) & p.value):
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
    """Union of recruitment_event_log + student_change_logs, sorted by time."""
    visit = session.query(RecruitmentVisit).filter_by(id=visit_id).first()
    if visit is None:
        raise HTTPException(404, detail={"code": "VISIT_NOT_FOUND"})

    rec_events = (
        session.query(RecruitmentEventLog)
        .filter_by(recruitment_visit_id=visit_id)
        .all()
    )
    student = (
        session.query(Student).filter(Student.recruitment_visit_id == visit_id).first()
    )
    student_events = []
    if student is not None:
        student_events = (
            session.query(StudentChangeLog).filter_by(student_id=student.id).all()
        )

    events: list[TimelineEvent] = []
    for e in rec_events:
        events.append(
            TimelineEvent(
                source="recruitment",
                event_type=e.event_type,
                from_stage=e.from_stage,
                to_stage=e.to_stage,
                actor_user_id=e.actor_user_id,
                reason=e.reason,
                created_at=e.created_at,
            )
        )
    for e in student_events:
        # event_date is a date; cast to datetime for unified sorting
        ts = (
            e.event_date
            if hasattr(e.event_date, "hour")
            else _dt.combine(e.event_date, _dt.min.time())
        )
        events.append(
            TimelineEvent(
                source="student",
                event_type=e.event_type,
                from_stage=None,
                to_stage=None,
                actor_user_id=e.recorded_by,
                reason=e.reason,
                created_at=ts,
            )
        )

    events.sort(key=lambda x: x.created_at)
    return TimelineOut(events=events)
