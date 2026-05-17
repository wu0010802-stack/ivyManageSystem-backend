"""api/appraisal — 半年考核 API（M4 重建版）。

提供 cycles / participants / score_items / summaries / bonus_rates / catalog 與
Excel 雙向 I/O 端點，全部聚合在單一 router 內。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalBonusRate,
    AppraisalCycle,
    AppraisalManualEventCount,
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalScoreItemCatalog,
    AppraisalScoringRule,
    AppraisalSummary,
    CycleStatus,
    Grade,
    RoleGroup,
    Semester,
    SummaryStatus,
)
from models.base import get_session_dep, session_scope
from models.employee import Employee
from schemas.appraisal import (
    ActivityRateAggregateOut,
    AggregatedStatusOut,
    AttendanceAggregateOut,
    BonusRateCreate,
    BonusRateOut,
    BulkAddParticipantsRequest,
    BulkAddParticipantsResult,
    CatalogOut,
    ClassRetentionAggregateOut,
    CycleCreate,
    CycleOut,
    CycleUpdate,
    DisciplinaryActionItemOut,
    DisciplinaryAggregateOut,
    DisciplinaryTieredConfig,
    FlatThresholdConfig,
    ImportResultOut,
    ManualEventCountBatchIn,
    ManualEventCountListOut,
    ManualEventCountOut,
    ParticipantCreate,
    ParticipantOut,
    ParticipantStatusOut,
    PerUnitConfig,
    ScoreItemCreate,
    ScoreItemOut,
    ScorePreviewItem,
    ScorePreviewOut,
    ScorePreviewParticipant,
    ScoringRuleIn,
    ScoringRuleOut,
    SummaryOut,
    SyncResultOut,
    SyncResultPreviewItem,
    TierConfig,
)
from services.appraisal.employee_inference import (
    infer_classroom_id,
    infer_role_group,
)
from services.appraisal.engine import BonusRateLookup, compute_summary
from services.appraisal.excel_io import (
    ExportRow,
    TransferRow,
    export_half_year_xlsx,
    export_transfer_roster_xlsx,
    import_half_year_to_db,
    parse_half_year_excel,
)
from services.appraisal.status_aggregator import (
    aggregate_all_active_employees_status,
    aggregate_cycle_status,
)
from utils.academic import resolve_current_academic_term, semester_int_to_enum
from utils.approval_helpers import assert_not_self_approval
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

appraisal_router = APIRouter(prefix="/api/appraisal", tags=["appraisal"])


# ===== Cycles =====


@appraisal_router.get("/cycles", response_model=list[CycleOut])
def list_cycles(
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(AppraisalCycle)
        .order_by(AppraisalCycle.academic_year.desc(), AppraisalCycle.semester)
        .all()
    )


@appraisal_router.get("/current", response_model=Optional[CycleOut])
def get_current_cycle(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=2),
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """取得當前學期 cycle；不存在回 null（200，**不**自動建立 / **不** 404）。

    參數規則：
      - 兩個都不給：用 `resolve_current_academic_term()` 決定當前學期
      - 兩個都給：用傳入值
      - 只給一個：400
    """
    if (school_year is None) != (semester is None):
        raise HTTPException(400, "school_year 與 semester 需同時提供")
    if school_year is None:
        sy, sem_int = resolve_current_academic_term()
    else:
        sy, sem_int = school_year, semester
    sem_enum = semester_int_to_enum(sem_int)
    return (
        session.query(AppraisalCycle)
        .filter_by(academic_year=sy, semester=sem_enum)
        .first()
    )


@appraisal_router.get("/by_year/{academic_year}", response_model=list[CycleOut])
def list_cycles_by_year(
    academic_year: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """取得某學年的所有 cycle（最多兩筆：上/下）。"""
    return (
        session.query(AppraisalCycle)
        .filter_by(academic_year=academic_year)
        .order_by(AppraisalCycle.semester)
        .all()
    )


def _build_participant_status_out(s) -> ParticipantStatusOut:
    """ParticipantStatus dataclass → ParticipantStatusOut Pydantic 共用組裝。"""
    return ParticipantStatusOut(
        participant_id=s.participant_id,
        employee_id=s.employee_id,
        employee_name=s.employee_name,
        role_group=RoleGroup(s.role_group),
        classroom_id=s.classroom_id,
        is_participant=s.is_participant,
        hire_months_in_cycle=s.hire_months_in_cycle,
        attendance=AttendanceAggregateOut(
            late_count=s.attendance.late_count,
            early_leave_count=s.attendance.early_leave_count,
            missing_punch_count=s.attendance.missing_punch_count,
            leave_days=s.attendance.leave_days,
            suggested_score_delta=s.attendance.suggested_score_delta,
        ),
        retention=ClassRetentionAggregateOut(
            classroom_id=s.retention.classroom_id,
            classroom_name=s.retention.classroom_name,
            initial_count=s.retention.initial_count,
            final_count=s.retention.final_count,
            retention_rate=s.retention.retention_rate,
            suggested_score_delta=s.retention.suggested_score_delta,
        ),
        activity=ActivityRateAggregateOut(
            classroom_id=s.activity.classroom_id,
            enrolled_students=s.activity.enrolled_students,
            registered_for_activity=s.activity.registered_for_activity,
            activity_rate=s.activity.activity_rate,
            suggested_score_delta=s.activity.suggested_score_delta,
        ),
        disciplinary=DisciplinaryAggregateOut(
            warning_count=s.disciplinary.warning_count,
            minor_count=s.disciplinary.minor_count,
            major_count=s.disciplinary.major_count,
            actions=[
                DisciplinaryActionItemOut(
                    id=a.id,
                    action_date=a.action_date,
                    action_type=a.action_type,
                    deduction_amount=a.deduction_amount,
                    reason=a.reason,
                )
                for a in s.disciplinary.actions
            ],
            suggested_score_delta=s.disciplinary.suggested_score_delta,
        ),
    )


@appraisal_router.get(
    "/cycles/{cycle_id}/aggregated_status", response_model=AggregatedStatusOut
)
def get_aggregated_status(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """彙整 cycle 期間每位 participant 的四個指標（不寫 DB）。"""
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    statuses = aggregate_cycle_status(session, cycle)
    participants = [_build_participant_status_out(s) for s in statuses]
    return AggregatedStatusOut(
        cycle_id=cycle.id,
        academic_year=cycle.academic_year,
        semester=cycle.semester,
        start_date=cycle.start_date,
        end_date=cycle.end_date,
        generated_at=datetime.now(timezone.utc),
        participants=participants,
    )


@appraisal_router.get(
    "/cycles/{cycle_id}/all_employees_status", response_model=AggregatedStatusOut
)
def get_all_employees_status(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """彙整 cycle 期間所有在職員工的四指標（包含未加入考核者）。

    - 已加入 cycle 的 participant 標 is_participant=True、participant_id 帶值；
    - 未加入者 is_participant=False、participant_id=None，role_group / classroom_id
      由 employee_inference helpers 從 Employee 推斷。
    - is_excluded=True 的 participant 視同未加入（不混入 participant override）。
    """
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    statuses = aggregate_all_active_employees_status(session, cycle)
    participants = [_build_participant_status_out(s) for s in statuses]
    return AggregatedStatusOut(
        cycle_id=cycle.id,
        academic_year=cycle.academic_year,
        semester=cycle.semester,
        start_date=cycle.start_date,
        end_date=cycle.end_date,
        generated_at=datetime.now(timezone.utc),
        participants=participants,
    )


@appraisal_router.post("/cycles", response_model=CycleOut)
def create_cycle(
    payload: CycleCreate,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_FINALIZE)),
    session: Session = Depends(get_session_dep),
):
    if (
        session.query(AppraisalCycle)
        .filter_by(academic_year=payload.academic_year, semester=payload.semester)
        .first()
    ):
        raise HTTPException(409, "週期已存在")
    base_score = Decimal("0")
    if payload.enrollment_target and payload.enrollment_actual is not None:
        base_score = (
            Decimal(payload.enrollment_actual)
            / Decimal(payload.enrollment_target)
            * 100
        ).quantize(Decimal("0.1"))
    cycle = AppraisalCycle(
        academic_year=payload.academic_year,
        semester=payload.semester,
        start_date=payload.start_date,
        end_date=payload.end_date,
        base_score_calc_date=payload.base_score_calc_date,
        base_score=base_score,
        enrollment_target=payload.enrollment_target,
        enrollment_actual=payload.enrollment_actual,
        status=CycleStatus.OPEN,
        created_by=current_user.get("user_id"),
    )
    session.add(cycle)
    session.commit()
    session.refresh(cycle)
    return cycle


@appraisal_router.patch("/cycles/{cycle_id}", response_model=CycleOut)
def update_cycle(
    cycle_id: int,
    payload: CycleUpdate,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_FINALIZE)),
    session: Session = Depends(get_session_dep),
):
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    # bug sweep 2026-05-16 P0-2：cycle 進入 LOCKED/CLOSED 後，base_score 與 enrollment
    # 是 recompute 的輸入；放任修改會讓已 FINALIZED 的 summary 與「重新打開重算」結果
    # 不一致，等於事後改獎金基數。狀態切換（payload.status）本身仍允許（例如 OPEN → LOCKED）。
    score_or_enrollment_changing = (
        payload.base_score is not None
        or payload.enrollment_target is not None
        or payload.enrollment_actual is not None
    )
    if score_or_enrollment_changing and cycle.status != CycleStatus.OPEN:
        raise HTTPException(
            status_code=400,
            detail=(
                f"週期狀態為 {cycle.status.value}，"
                "不允許修改 base_score / enrollment_target / enrollment_actual"
            ),
        )
    if payload.base_score is not None:
        cycle.base_score = payload.base_score
    if payload.enrollment_target is not None:
        cycle.enrollment_target = payload.enrollment_target
    if payload.enrollment_actual is not None:
        cycle.enrollment_actual = payload.enrollment_actual
    if payload.status is not None:
        cycle.status = payload.status
    session.commit()
    session.refresh(cycle)
    return cycle


# ===== Catalog =====


@appraisal_router.get("/catalog", response_model=list[CatalogOut])
def list_catalog(
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(AppraisalScoreItemCatalog)
        .order_by(AppraisalScoreItemCatalog.display_order)
        .all()
    )


# ===== Participants =====


@appraisal_router.get(
    "/cycles/{cycle_id}/participants", response_model=list[ParticipantOut]
)
def list_participants(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(AppraisalParticipant)
        .filter_by(cycle_id=cycle_id)
        .order_by(AppraisalParticipant.id)
        .all()
    )


@appraisal_router.post("/cycles/{cycle_id}/participants", response_model=ParticipantOut)
def add_participant(
    cycle_id: int,
    payload: ParticipantCreate,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_EVENT_WRITE)),
    session: Session = Depends(get_session_dep),
):
    if not session.get(AppraisalCycle, cycle_id):
        raise HTTPException(404, "週期不存在")
    if (
        session.query(AppraisalParticipant)
        .filter_by(cycle_id=cycle_id, employee_id=payload.employee_id)
        .first()
    ):
        raise HTTPException(409, "已存在")
    p = AppraisalParticipant(cycle_id=cycle_id, **payload.model_dump())
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@appraisal_router.post(
    "/cycles/{cycle_id}/participants:bulk_from_active",
    response_model=BulkAddParticipantsResult,
)
def bulk_add_participants_from_active(
    cycle_id: int,
    payload: BulkAddParticipantsRequest,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_EVENT_WRITE)),
    session: Session = Depends(get_session_dep),
):
    """把指定（或全部）在職員工自動加入 cycle。

    - role_group / classroom_id 從 Employee 推斷（infer_role_group / infer_classroom_id）；
    - hire_months_in_cycle 預設 6；
    - 已加入 cycle 的員工會被 skip（不論 is_excluded）；
    - cycle.status != OPEN 直接 400。
    """
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    if cycle.status != CycleStatus.OPEN:
        raise HTTPException(400, f"cycle 已 {cycle.status.value}，無法加入")

    q = session.query(Employee).filter(Employee.is_active == True)  # noqa: E712
    if payload.employee_ids:
        q = q.filter(Employee.id.in_(payload.employee_ids))
    employees = q.all()
    existing = {
        p.employee_id
        for p in session.query(AppraisalParticipant).filter_by(cycle_id=cycle_id).all()
    }
    created_rows: list[AppraisalParticipant] = []
    skipped_count = 0
    for e in employees:
        if e.id in existing:
            skipped_count += 1
            continue
        p = AppraisalParticipant(
            cycle_id=cycle_id,
            employee_id=e.id,
            role_group=infer_role_group(e),
            classroom_id=infer_classroom_id(e),
            hire_months_in_cycle=Decimal("6"),
            is_excluded=False,
        )
        session.add(p)
        created_rows.append(p)
    session.commit()
    for p in created_rows:
        session.refresh(p)
    return BulkAddParticipantsResult(
        cycle_id=cycle_id,
        created_count=len(created_rows),
        skipped_count=skipped_count,
        created_participants=[ParticipantOut.model_validate(p) for p in created_rows],
    )


# ===== Score Items =====


@appraisal_router.get(
    "/participants/{participant_id}/score_items", response_model=list[ScoreItemOut]
)
def list_score_items(
    participant_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(AppraisalScoreItem)
        .filter_by(participant_id=participant_id)
        .order_by(AppraisalScoreItem.item_code, AppraisalScoreItem.sequence_no)
        .all()
    )


@appraisal_router.post(
    "/participants/{participant_id}/score_items", response_model=ScoreItemOut
)
def add_score_item(
    participant_id: int,
    payload: ScoreItemCreate,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_EVENT_WRITE)),
    session: Session = Depends(get_session_dep),
):
    participant = session.get(AppraisalParticipant, participant_id)
    if participant is None:
        raise HTTPException(404, "participant 不存在")
    catalog = (
        session.query(AppraisalScoreItemCatalog)
        .filter_by(code=payload.item_code)
        .first()
    )
    si = AppraisalScoreItem(
        participant_id=participant_id,
        cycle_id=participant.cycle_id,
        catalog_id=catalog.id if catalog else None,
        item_code=payload.item_code,
        sequence_no=payload.sequence_no,
        score_delta=payload.score_delta,
        raw_value=payload.raw_value,
        note=payload.note,
        created_by=current_user.get("user_id"),
    )
    session.add(si)
    session.commit()
    session.refresh(si)
    return si


@appraisal_router.post(
    "/cycles/{cycle_id}/sync_score_items", response_model=SyncResultOut
)
def sync_score_items(
    cycle_id: int,
    dry_run: bool = Query(False),
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_EVENT_WRITE)),
    session: Session = Depends(get_session_dep),
):
    """把 14 個 item_code 的 suggested_score_delta 寫入 appraisal_score_items。

    source_ref 規範：auto:<lowercase_item_code>:<cycle_id>
      （例：auto:late_early:42 / auto:school_meeting_absence:42 ...）

    Sync 流程：DELETE WHERE source_ref LIKE 'auto:%:<cycle_id>' → INSERT 14×N；
    人工 row（source_ref IS NULL 或非 'auto:%:<cycle_id>'）絕不動。

    限制：
      - cycle.status != OPEN → 400（已鎖/已關閉）
      - dry_run=true → 不寫 DB，回 preview
    """
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    if cycle.status != CycleStatus.OPEN:
        raise HTTPException(400, f"cycle 已 {cycle.status.value}，無法同步")

    from services.appraisal.rule_applier import compute_all_deltas

    deltas = compute_all_deltas(session, cycle)

    suffix = f":{cycle_id}"
    auto_like = f"auto:%{suffix}"

    # 撈 participant → employee name（preview 顯示）
    pids = {pid for pid, _ in deltas.keys()}
    participants = (
        session.query(AppraisalParticipant)
        .filter(AppraisalParticipant.id.in_(pids))
        .all()
        if pids
        else []
    )
    pid_to_emp = {p.id: p.employee_id for p in participants}
    emp_ids = set(pid_to_emp.values())
    emp_names = (
        {
            e.id: e.name
            for e in session.query(Employee).filter(Employee.id.in_(emp_ids)).all()
        }
        if emp_ids
        else {}
    )

    auto_rows: list[dict] = []
    for (pid, code), dr in deltas.items():
        ref = f"auto:{code.lower()}:{cycle_id}"
        auto_rows.append(
            {
                "participant_id": pid,
                "cycle_id": cycle_id,
                "item_code": code,
                "score_delta": dr.delta,
                "raw_value": dr.raw_value,
                "note": dr.note,
                "source_ref": ref,
            }
        )

    existing_auto = (
        session.query(AppraisalScoreItem)
        .filter(AppraisalScoreItem.cycle_id == cycle_id)
        .filter(AppraisalScoreItem.source_ref.like(auto_like))
        .all()
    )
    skipped_manual = (
        session.query(func.count(AppraisalScoreItem.id))
        .filter(AppraisalScoreItem.cycle_id == cycle_id)
        .filter(
            or_(
                AppraisalScoreItem.source_ref.is_(None),
                ~AppraisalScoreItem.source_ref.like(auto_like),
            )
        )
        .scalar()
    ) or 0
    old_by_key = {(r.participant_id, r.item_code): r.score_delta for r in existing_auto}

    preview = [
        SyncResultPreviewItem(
            participant_id=row["participant_id"],
            employee_name=emp_names.get(pid_to_emp.get(row["participant_id"]), ""),
            item_code=row["item_code"],
            old_score_delta=old_by_key.get(
                (row["participant_id"], row["item_code"]), Decimal("0")
            ),
            new_score_delta=row["score_delta"],
            source_ref=row["source_ref"],
        )
        for row in auto_rows
    ]

    if dry_run:
        return SyncResultOut(
            cycle_id=cycle_id,
            dry_run=True,
            deleted_count=len(existing_auto),
            inserted_count=len(auto_rows),
            skipped_manual_count=int(skipped_manual),
            items=preview,
        )

    deleted = (
        session.query(AppraisalScoreItem)
        .filter(AppraisalScoreItem.cycle_id == cycle_id)
        .filter(AppraisalScoreItem.source_ref.like(auto_like))
        .delete(synchronize_session=False)
    )
    # 把 DELETE 先 flush 出去，避免 INSERT 階段 identity map 還抓著舊 row
    # （PG 不會出問題，但 SQLite 會丟 SAWarning）。
    session.flush()
    catalog_ids = {c.code: c.id for c in session.query(AppraisalScoreItemCatalog).all()}
    for row in auto_rows:
        session.add(
            AppraisalScoreItem(
                participant_id=row["participant_id"],
                cycle_id=row["cycle_id"],
                catalog_id=catalog_ids.get(row["item_code"]),
                item_code=row["item_code"],
                sequence_no=1,
                score_delta=row["score_delta"],
                raw_value=row["raw_value"],
                note=row["note"],
                source_ref=row["source_ref"],
                created_by=current_user.get("user_id"),
            )
        )
    session.commit()
    return SyncResultOut(
        cycle_id=cycle_id,
        dry_run=False,
        deleted_count=int(deleted),
        inserted_count=len(auto_rows),
        skipped_manual_count=int(skipped_manual),
        items=preview,
    )


# ===== Summaries =====


@appraisal_router.get("/cycles/{cycle_id}/summaries", response_model=list[SummaryOut])
def list_summaries(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(AppraisalSummary)
        .filter_by(cycle_id=cycle_id)
        .order_by(AppraisalSummary.id)
        .all()
    )


@appraisal_router.post(
    "/cycles/{cycle_id}/summaries:recompute", response_model=list[SummaryOut]
)
def recompute_summaries(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_EVENT_WRITE)),
    session: Session = Depends(get_session_dep),
):
    """以引擎重算 cycle 內所有 participant 的 summary（5-step）。"""
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    rates_rows = session.query(AppraisalBonusRate).all()
    bonus_lookup = BonusRateLookup(
        rates={
            (r.effective_from.isoformat(), r.role_group, r.grade): r.base_amount
            for r in rates_rows
        }
    )
    participants = (
        session.query(AppraisalParticipant)
        .filter_by(cycle_id=cycle_id, is_excluded=False)
        .all()
    )
    enrollment_target = cycle.enrollment_target or 0
    enrollment_actual = cycle.enrollment_actual or 0
    out: list[AppraisalSummary] = []
    for p in participants:
        deltas = [
            si.score_delta
            for si in session.query(AppraisalScoreItem)
            .filter_by(participant_id=p.id)
            .all()
        ]
        result = compute_summary(
            actual_enrollment=enrollment_actual,
            enrollment_target=enrollment_target,
            score_deltas=deltas,
            role_group=p.role_group,
            bonus_rates=bonus_lookup,
            on_date=cycle.base_score_calc_date,
        )
        summary = session.query(AppraisalSummary).filter_by(participant_id=p.id).first()
        if summary is None:
            summary = AppraisalSummary(
                participant_id=p.id,
                cycle_id=cycle.id,
                base_score=result.base_score,
                event_score_sum=result.event_score_sum,
                total_score=result.total_score,
                grade=result.grade,
                bonus_amount=result.bonus_amount,
            )
            session.add(summary)
        elif summary.status == SummaryStatus.FINALIZED:
            # bug sweep 2026-05-16 P0-2：FINALIZED 後不再覆寫金額。
            # 若需要重算已核定的 summary，需先走「駁回 → 重簽」流程。
            pass
        else:
            summary.base_score = result.base_score
            summary.event_score_sum = result.event_score_sum
            summary.total_score = result.total_score
            summary.grade = result.grade
            summary.bonus_amount = result.bonus_amount
            summary.version += 1
        out.append(summary)
    session.commit()
    return out


@appraisal_router.post(
    "/summaries/{summary_id}/sign_supervisor", response_model=SummaryOut
)
def sign_supervisor(
    summary_id: int,
    comment: str = "",
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_REVIEW)),
    session: Session = Depends(get_session_dep),
):
    # with_for_update：兩個 reviewer 同時簽核時，後贏者會覆蓋簽核人欄位
    # 卻只看到「已是 SUPERVISOR_SIGNED」就跳過更新，造成稽核軌跡被誰偷換。
    # bug sweep 2026-05-16 P1-3。
    summary = (
        session.query(AppraisalSummary)
        .filter(AppraisalSummary.id == summary_id)
        .with_for_update()
        .first()
    )
    if summary is None:
        raise HTTPException(404, "summary 不存在")
    if summary.status != SummaryStatus.DRAFT:
        raise HTTPException(400, f"非 DRAFT 狀態（current={summary.status.value}）")
    assert_not_self_approval(
        current_user,
        summary.participant.employee_id,
        doc_label="考核獎金",
    )
    summary.status = SummaryStatus.SUPERVISOR_SIGNED
    summary.supervisor_signed_by = current_user.get("user_id")
    from datetime import datetime, timezone

    summary.supervisor_signed_at = datetime.now(timezone.utc)
    summary.supervisor_comment = comment
    session.commit()
    session.refresh(summary)
    return summary


@appraisal_router.post(
    "/summaries/{summary_id}/sign_accounting", response_model=SummaryOut
)
def sign_accounting(
    summary_id: int,
    comment: str = "",
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_ACCOUNTING)),
    session: Session = Depends(get_session_dep),
):
    # with_for_update：見 sign_supervisor 註解。bug sweep 2026-05-16 P1-3。
    summary = (
        session.query(AppraisalSummary)
        .filter(AppraisalSummary.id == summary_id)
        .with_for_update()
        .first()
    )
    if summary is None:
        raise HTTPException(404, "summary 不存在")
    if summary.status != SummaryStatus.SUPERVISOR_SIGNED:
        raise HTTPException(400, f"未經主管簽核（current={summary.status.value}）")
    assert_not_self_approval(
        current_user,
        summary.participant.employee_id,
        doc_label="考核獎金",
    )
    summary.status = SummaryStatus.ACCOUNTING_SIGNED
    summary.accounting_signed_by = current_user.get("user_id")
    from datetime import datetime, timezone

    summary.accounting_signed_at = datetime.now(timezone.utc)
    summary.accounting_comment = comment
    session.commit()
    session.refresh(summary)
    return summary


@appraisal_router.post("/summaries/{summary_id}/finalize", response_model=SummaryOut)
def finalize_summary(
    summary_id: int,
    comment: str = "",
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_FINALIZE)),
    session: Session = Depends(get_session_dep),
):
    # with_for_update：見 sign_supervisor 註解。bug sweep 2026-05-16 P1-3。
    summary = (
        session.query(AppraisalSummary)
        .filter(AppraisalSummary.id == summary_id)
        .with_for_update()
        .first()
    )
    if summary is None:
        raise HTTPException(404, "summary 不存在")
    if summary.status != SummaryStatus.ACCOUNTING_SIGNED:
        raise HTTPException(400, f"未經行政會計簽核（current={summary.status.value}）")
    assert_not_self_approval(
        current_user,
        summary.participant.employee_id,
        doc_label="考核獎金",
    )
    summary.status = SummaryStatus.FINALIZED
    summary.finalized_by = current_user.get("user_id")
    from datetime import datetime, timezone

    summary.finalized_at = datetime.now(timezone.utc)
    summary.finalized_comment = comment
    session.commit()
    session.refresh(summary)
    return summary


# ===== Bonus Rates =====


@appraisal_router.get("/bonus_rates", response_model=list[BonusRateOut])
def list_bonus_rates(
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(AppraisalBonusRate)
        .order_by(
            AppraisalBonusRate.effective_from.desc(),
            AppraisalBonusRate.role_group,
            AppraisalBonusRate.grade,
        )
        .all()
    )


@appraisal_router.post("/bonus_rates", response_model=BonusRateOut)
def create_bonus_rate(
    payload: BonusRateCreate,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_FINALIZE)),
    session: Session = Depends(get_session_dep),
):
    br = AppraisalBonusRate(
        **payload.model_dump(), created_by=current_user.get("user_id")
    )
    session.add(br)
    session.commit()
    session.refresh(br)
    return br


# ===== Excel I/O =====


def _build_employee_resolver(session: Session):
    employees = session.query(Employee).filter_by(is_active=True).all()
    name_to_id = {e.name: e.id for e in employees}

    def resolver(name: str):
        return name_to_id.get(name)

    return resolver


def _build_role_resolver(session: Session):
    employees = {e.id: e for e in session.query(Employee).all()}

    def resolver(emp_id: int) -> RoleGroup:
        e = employees.get(emp_id)
        if e is None:
            return RoleGroup.STAFF
        return infer_role_group(e)

    return resolver


def _build_classroom_resolver(session: Session):
    employees = {e.id: e for e in session.query(Employee).all()}

    def resolver(emp_id: int):
        e = employees.get(emp_id)
        return infer_classroom_id(e) if e else None

    return resolver


@appraisal_router.post("/cycles/import_excel", response_model=ImportResultOut)
async def import_excel(
    file: UploadFile = File(...),
    start_date: date = Query(...),
    end_date: date = Query(...),
    base_score_calc_date: date = Query(...),
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_EVENT_WRITE)),
):
    """上傳半年考核 Excel（.xls 或 .xlsx）→ 建立/更新 cycle/participants/score_items/summaries。"""
    if not file.filename or not file.filename.lower().endswith((".xls", ".xlsx")):
        raise HTTPException(400, "僅支援 .xls / .xlsx")
    content = await file.read()
    suffix = ".xls" if file.filename.lower().endswith(".xls") else ".xlsx"
    with NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(content)
        tmp.flush()
        parsed = parse_half_year_excel(tmp.name)

    with session_scope() as session:
        emp_resolver = _build_employee_resolver(session)
        role_resolver = _build_role_resolver(session)
        room_resolver = _build_classroom_resolver(session)
        result = import_half_year_to_db(
            parsed,
            session,
            employee_resolver=emp_resolver,
            role_group_resolver=role_resolver,
            cycle_dates=(start_date, end_date, base_score_calc_date),
            classroom_resolver=room_resolver,
        )
        return ImportResultOut(
            cycle_id=result.cycle_id,
            participants_created=result.participants_created,
            participants_updated=result.participants_updated,
            score_items_upserted=result.score_items_upserted,
            summaries_upserted=result.summaries_upserted,
            skipped_unresolved_names=result.skipped_unresolved_names,
        )


@appraisal_router.get("/cycles/{cycle_id}/export.xlsx")
def export_excel(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """匯出半年考核成績表（與 Excel 原始版同欄位）。"""
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    participants = (
        session.query(AppraisalParticipant).filter_by(cycle_id=cycle_id).all()
    )
    employees = {e.id: e for e in session.query(Employee).all()}
    rows: list[ExportRow] = []
    for p in participants:
        score_items = {
            si.item_code: si.score_delta
            for si in session.query(AppraisalScoreItem)
            .filter_by(participant_id=p.id)
            .all()
        }
        summary = session.query(AppraisalSummary).filter_by(participant_id=p.id).first()
        emp_name = (
            employees.get(p.employee_id).name
            if employees.get(p.employee_id)
            else f"emp#{p.employee_id}"
        )
        rows.append(
            ExportRow(
                name=emp_name,
                score_items=score_items,
                total_score=summary.total_score if summary else Decimal("0"),
                grade=summary.grade if summary else Grade.FAIL,
                bonus_amount=summary.bonus_amount if summary else Decimal("0"),
                leave_note=summary.leave_note if summary else None,
                is_excluded=p.is_excluded,
                exclude_reason=p.exclude_reason,
            )
        )
    sem_label = "上" if cycle.semester == Semester.FIRST else "下"
    title = f"{cycle.academic_year}({sem_label})年度考核統計表"
    payload = export_half_year_xlsx(
        title=title,
        academic_year=cycle.academic_year,
        semester=cycle.semester,
        base_score=cycle.base_score,
        rows=rows,
    )
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="appraisal_{cycle.academic_year}_{sem_label}.xlsx"'
        },
    )


@appraisal_router.get("/cycles/{cycle_id}/transfer_roster.xlsx")
def export_transfer_roster(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """匯出轉帳名冊（只含 bonus > 0 的員工）。"""
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    summaries = (
        session.query(AppraisalSummary)
        .filter(
            AppraisalSummary.cycle_id == cycle_id, AppraisalSummary.bonus_amount > 0
        )
        .all()
    )
    employees = {e.id: e for e in session.query(Employee).all()}
    rows: list[TransferRow] = []
    for s in summaries:
        p = session.get(AppraisalParticipant, s.participant_id)
        if p is None:
            continue
        e = employees.get(p.employee_id)
        if e is None:
            continue
        rows.append(
            TransferRow(
                bank_account=e.bank_account or "",
                name=e.bank_account_name or e.name,
                amount=s.bonus_amount,
            )
        )
    payload = export_transfer_roster_xlsx(rows=rows)
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="transfer_roster_{cycle_id}.xlsx"'
        },
    )


# ===== Scoring Rules (calibrate Phase 1) =====


_CONFIG_VALIDATORS = {
    "PER_UNIT": PerUnitConfig,
    "TIER": TierConfig,
    "FLAT_THRESHOLD": FlatThresholdConfig,
    "DISCIPLINARY_TIERED": DisciplinaryTieredConfig,
}


def _validate_rule_config(rule_type: str, config: dict) -> dict:
    """依 rule_type 用對應 BaseModel 二次 validate rule_config。"""
    validator = _CONFIG_VALIDATORS.get(rule_type)
    if validator is None:
        raise HTTPException(422, f"未知 rule_type: {rule_type}")
    try:
        return validator(**config).model_dump(mode="json")
    except (ValueError, TypeError) as e:
        raise HTTPException(422, f"rule_config 驗證失敗: {e}")


def _row_to_scoring_rule_out(row: AppraisalScoringRule) -> ScoringRuleOut:
    return ScoringRuleOut(
        id=row.id,
        item_code=row.item_code,
        effective_from=row.effective_from,
        rule_type=row.rule_type,
        rule_config=row.rule_config,
        applies_to_role_groups=row.applies_to_role_groups,
        notes=row.notes,
        created_at=row.created_at.isoformat() if row.created_at else None,
        created_by=row.created_by,
    )


@appraisal_router.get("/scoring_rules", response_model=list[ScoringRuleOut])
def list_scoring_rules(
    effective_on: Optional[date] = Query(None),
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """列出指定日期當前有效的所有規則（每個 item_code 只取最新版）。"""
    on_date = effective_on or date.today()
    rows = (
        session.query(AppraisalScoringRule)
        .filter(AppraisalScoringRule.effective_from <= on_date)
        .order_by(
            AppraisalScoringRule.item_code,
            AppraisalScoringRule.effective_from.desc(),
        )
        .all()
    )
    seen: set[str] = set()
    out: list[ScoringRuleOut] = []
    for row in rows:
        if row.item_code in seen:
            continue
        seen.add(row.item_code)
        out.append(_row_to_scoring_rule_out(row))
    return out


@appraisal_router.get("/scoring_rules/history", response_model=list[ScoringRuleOut])
def get_scoring_rule_history(
    item_code: str = Query(...),
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """單一 item_code 的版本歷史，依 effective_from 由新到舊。"""
    rows = (
        session.query(AppraisalScoringRule)
        .filter(AppraisalScoringRule.item_code == item_code)
        .order_by(AppraisalScoringRule.effective_from.desc())
        .all()
    )
    return [_row_to_scoring_rule_out(r) for r in rows]


@appraisal_router.post("/scoring_rules", response_model=ScoringRuleOut, status_code=201)
def create_scoring_rule(
    payload: ScoringRuleIn,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_RULE_WRITE)),
    session: Session = Depends(get_session_dep),
):
    """建立新版規則。

    - effective_from 不可早於今天
    - rule_config 依 rule_type 二次 validate
    - (item_code, effective_from) UNIQUE 衝突回 409
    """
    if payload.effective_from < date.today():
        raise HTTPException(422, "effective_from 不可早於今天")
    validated_config = _validate_rule_config(payload.rule_type, payload.rule_config)
    exists = (
        session.query(AppraisalScoringRule)
        .filter(
            AppraisalScoringRule.item_code == payload.item_code,
            AppraisalScoringRule.effective_from == payload.effective_from,
        )
        .first()
    )
    if exists:
        raise HTTPException(
            409,
            f"{payload.item_code} 已有 {payload.effective_from} 生效的版本",
        )
    row = AppraisalScoringRule(
        item_code=payload.item_code,
        effective_from=payload.effective_from,
        rule_type=payload.rule_type,
        rule_config=validated_config,
        applies_to_role_groups=payload.applies_to_role_groups,
        notes=payload.notes,
        created_by=current_user.get("user_id"),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _row_to_scoring_rule_out(row)


# ===== Manual Event Counts (calibrate Phase 1) =====


@appraisal_router.get(
    "/cycles/{cycle_id}/manual_event_counts",
    response_model=ManualEventCountListOut,
)
def list_manual_event_counts(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """列出指定 cycle 已填的手填事件次數（含員工姓名）。"""
    rows = (
        session.query(AppraisalManualEventCount, AppraisalParticipant.employee_id)
        .join(
            AppraisalParticipant,
            AppraisalParticipant.id == AppraisalManualEventCount.participant_id,
        )
        .filter(AppraisalManualEventCount.cycle_id == cycle_id)
        .all()
    )
    employee_ids = [emp_id for _, emp_id in rows]
    emp_names: dict[int, str] = (
        {
            e.id: e.name
            for e in session.query(Employee).filter(Employee.id.in_(employee_ids)).all()
        }
        if employee_ids
        else {}
    )
    return ManualEventCountListOut(
        cycle_id=cycle_id,
        entries=[
            ManualEventCountOut(
                participant_id=r.participant_id,
                employee_name=emp_names.get(emp_id, ""),
                item_code=r.item_code,
                count=r.count,
                entered_by=r.entered_by,
                entered_at=r.entered_at.isoformat() if r.entered_at else None,
            )
            for r, emp_id in rows
        ],
    )


@appraisal_router.put("/cycles/{cycle_id}/manual_event_counts:batch")
def batch_upsert_manual_event_counts(
    cycle_id: int,
    payload: ManualEventCountBatchIn,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_EVENT_WRITE)),
    session: Session = Depends(get_session_dep),
):
    """Batch UPSERT 手填事件次數；僅 OPEN 狀態的 cycle 可寫。"""
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")
    if cycle.status != CycleStatus.OPEN:
        raise HTTPException(400, f"cycle 已 {cycle.status.value}，無法編輯")

    for entry in payload.entries:
        existing = (
            session.query(AppraisalManualEventCount)
            .filter_by(
                cycle_id=cycle_id,
                participant_id=entry.participant_id,
                item_code=entry.item_code,
            )
            .first()
        )
        if existing:
            existing.count = entry.count
            existing.entered_by = current_user.get("user_id")
            existing.note = entry.note
        else:
            session.add(
                AppraisalManualEventCount(
                    cycle_id=cycle_id,
                    participant_id=entry.participant_id,
                    item_code=entry.item_code,
                    count=entry.count,
                    entered_by=current_user.get("user_id"),
                    note=entry.note,
                )
            )
    session.commit()
    return {"ok": True, "updated_count": len(payload.entries)}


@appraisal_router.post(
    "/cycles/{cycle_id}/score_preview", response_model=ScorePreviewOut
)
def score_preview(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_READ)),
    session: Session = Depends(get_session_dep),
):
    """Dry-run 算 14 條 delta + 對比目前 DB score_items 標 highlight。

    不寫 DB；前端拿來給 admin 預覽 sync 前後差異。
    """
    cycle = session.get(AppraisalCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "週期不存在")

    from services.appraisal.rule_applier import compute_all_deltas

    deltas = compute_all_deltas(session, cycle)

    # 目前 DB 中已存的 score_items（用於 current_db_value 比對）
    existing = (
        session.query(AppraisalScoreItem)
        .filter(AppraisalScoreItem.cycle_id == cycle_id)
        .all()
    )
    current_by_key = {(r.participant_id, r.item_code): r.score_delta for r in existing}

    # 員工名稱
    pids = {pid for pid, _ in deltas.keys()}
    pid_to_emp = (
        {
            p.id: p.employee_id
            for p in session.query(AppraisalParticipant)
            .filter(AppraisalParticipant.id.in_(pids))
            .all()
        }
        if pids
        else {}
    )
    emp_ids = set(pid_to_emp.values())
    emp_names = (
        {
            e.id: e.name
            for e in session.query(Employee).filter(Employee.id.in_(emp_ids)).all()
        }
        if emp_ids
        else {}
    )

    by_pid: dict[int, list[ScorePreviewItem]] = defaultdict(list)
    for (pid, code), dr in deltas.items():
        by_pid[pid].append(
            ScorePreviewItem(
                item_code=code,
                delta=dr.delta,
                raw_value=dr.raw_value,
                note=dr.note,
                current_db_value=current_by_key.get((pid, code)),
            )
        )
    # 穩定排序：participant_id asc，items 內按 item_code asc
    participants_out = [
        ScorePreviewParticipant(
            participant_id=pid,
            employee_name=emp_names.get(pid_to_emp.get(pid, 0), ""),
            items=sorted(items, key=lambda i: i.item_code),
        )
        for pid, items in sorted(by_pid.items())
    ]

    return ScorePreviewOut(
        cycle_id=cycle_id,
        on_date=cycle.base_score_calc_date,
        participants=participants_out,
    )


__all__ = ["appraisal_router"]
