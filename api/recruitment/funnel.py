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
from utils.permissions import Permission
from utils.portfolio_access import is_unrestricted

router = APIRouter(prefix="/api/recruitment/funnel", tags=["recruitment-funnel"])


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
    """4 階段看板資料，依「入學學期（target_school_year/target_semester）」圈定範圍。

    所有寫入路徑皆保證 target 有值：create 預設當前學期、Excel import 由月份推導、
    enrterm01 migration backfill 補齊既有資料（含 dev DB null_tsy=0 記錄）。
    因此 board 可直接以 target 過濾，無需 month fallback。

    school_year 未帶時預設當前學年，semester 未帶時涵蓋整學年（上+下，即不加
    target_semester 條件）。
    """
    if school_year is None:
        sy, _ = resolve_current_academic_term()
        school_year = sy

    visit_q = session.query(RecruitmentVisit).filter(
        RecruitmentVisit.target_school_year == school_year
    )
    if semester is not None:
        visit_q = visit_q.filter(RecruitmentVisit.target_semester == semester)
    visits = visit_q.all()
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


def _missing_unrestricted_permission(
    current_user: dict, required: list[Permission]
) -> Optional[Permission]:
    """回傳第一個 caller 缺 unrestricted grant 的必要權限；全部具備則 None。

    funnel transition 無班級 context，scope-qualified（:own_class）grant 在此無意義；
    要求 unrestricted（bare / :all / wildcard）grant，避免自訂 scoped 角色越權轉換
    任意 visit。bare 權限解析為 scope 'all'（resolve_grant 向後相容），正當招生
    staff（持 bare RECRUITMENT_*/STUDENTS_WRITE）不受影響。
    """
    for p in required:
        if not is_unrestricted(current_user, code=p.value):
            return p
    return None


# === POST /visits/{visit_id}/transition ===
@router.post("/visits/{visit_id}/transition", response_model=TransitionOut)
def post_transition(
    visit_id: int,
    payload: TransitionIn,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(get_current_user),
):
    """State machine driver — dynamic permission check based on from/to stage."""
    # R4-3：補教師/家長結構封鎖（對齊其他 recruitment write 端點的 require_staff_permission）。
    # 原本只有下方 _missing_unrestricted_permission（scope 檢查），無 role 短路 → admin
    # 誤授某 teacher bare RECRUITMENT_WRITE 時，該 teacher 可打此端點 revert 轉換
    # （_do_revert_convert 硬刪 Student+Guardian）。置於 visit 查詢前先擋。
    if current_user.get("role") in ("teacher", "parent"):
        raise HTTPException(
            403,
            detail={
                "code": "PERMISSION_DENIED",
                "message": "教師/家長帳號不可存取招生管理端",
            },
        )

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
    missing = _missing_unrestricted_permission(current_user, required)
    if missing is not None:
        raise HTTPException(
            403,
            detail={"code": "PERMISSION_DENIED", "message": f"missing {missing.name}"},
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
        # STAGE_ALREADY / CONVERT_CONFLICT 屬資源狀態衝突 → 409；其餘業務錯誤 → 400。
        # CONVERT_CONFLICT 來自 deposited→enrolled 並發轉換 race（Bug #20），
        # 經 _do_convert 把底層 RecruitmentConversionError 包裝後傳出。
        status = 409 if e.code in ("STAGE_ALREADY", "CONVERT_CONFLICT") else 400
        raise HTTPException(status, detail={"code": e.code, "message": str(e)})

    # transition_visit / convert_recruitment_to_student 內部僅 flush（docstring 明示
    # 「呼叫端負責 commit」）；get_session_dep 的 finally 只 close、不 commit → 未顯式
    # commit 的成功轉換會在 close 時被 rollback（P1 資料遺失：端點回 200 但 DB 零持久化）。
    # 僅在成功路徑 commit；error 路徑已於上方 raise HTTPException（不到這裡，不會 commit）。
    session.commit()

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
