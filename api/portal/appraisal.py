"""教師自助考核查詢端點。

設計原則對齊 api/portal/salary.py：
- 以 current_user.employee_id 自動鎖定身份；不需要 APPRAISAL_READ 權限
- summary.status != FINALIZED 時不揭分數（避免簽核中途數字變動引爭議）
- 評語（supervisor_comment 等）一律不曝光
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalScoreItemCatalog,
    AppraisalSummary,
    SummaryStatus,
)
from models.database import get_session_dep
from utils.auth import get_current_user

from ._shared import _get_employee

router = APIRouter()


# ============ Pydantic Models ============


class MyAppraisalListItem(BaseModel):
    cycle_id: int
    academic_year: int
    semester: Literal["FIRST", "SECOND"]
    start_date: date
    end_date: date
    cycle_status: Literal["OPEN", "LOCKED", "CLOSED"]
    participant_id: int
    role_group: str
    is_excluded: bool
    exclude_reason: Optional[str] = None
    summary_status: Optional[
        Literal["DRAFT", "SUPERVISOR_SIGNED", "ACCOUNTING_SIGNED", "FINALIZED"]
    ] = None
    is_rejected: bool = False
    is_visible: bool = False
    total_score: Optional[Decimal] = None
    grade: Optional[Literal["OUTSTANDING", "GOOD", "PASS", "WARN", "FAIL"]] = None
    bonus_amount: Optional[Decimal] = None


class MyAppraisalListOut(BaseModel):
    items: list[MyAppraisalListItem]


# ============ Helpers ============


def _build_list_item(
    cycle: AppraisalCycle,
    participant: AppraisalParticipant,
    summary: Optional[AppraisalSummary],
) -> MyAppraisalListItem:
    is_rejected = bool(summary and summary.rejected_at is not None)
    is_finalized = bool(summary and summary.status == SummaryStatus.FINALIZED)
    is_visible = is_finalized and not is_rejected
    return MyAppraisalListItem(
        cycle_id=cycle.id,
        academic_year=cycle.academic_year,
        semester=cycle.semester.value,
        start_date=cycle.start_date,
        end_date=cycle.end_date,
        cycle_status=cycle.status.value,
        participant_id=participant.id,
        role_group=participant.role_group.value,
        is_excluded=participant.is_excluded,
        exclude_reason=participant.exclude_reason,
        summary_status=summary.status.value if summary else None,
        is_rejected=is_rejected,
        is_visible=is_visible,
        total_score=summary.total_score if is_visible else None,
        grade=summary.grade.value if is_visible else None,
        bonus_amount=summary.bonus_amount if is_visible else None,
    )


# ============ Endpoints ============


@router.get("/my-appraisals", response_model=MyAppraisalListOut)
def list_my_appraisals(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session_dep),
):
    """歷年考核清單。未 FINALIZED 不回分數。"""
    emp = _get_employee(session, current_user)
    rows = (
        session.query(AppraisalParticipant, AppraisalCycle, AppraisalSummary)
        .join(AppraisalCycle, AppraisalParticipant.cycle_id == AppraisalCycle.id)
        .outerjoin(
            AppraisalSummary,
            AppraisalSummary.participant_id == AppraisalParticipant.id,
        )
        .filter(AppraisalParticipant.employee_id == emp.id)
        .order_by(
            AppraisalCycle.academic_year.desc(),
            AppraisalCycle.semester.desc(),
        )
        .all()
    )
    items = [_build_list_item(cycle, p, s) for p, cycle, s in rows]
    return MyAppraisalListOut(items=items)


# ============ Trend ============


class MyTrendPoint(BaseModel):
    cycle_id: int
    academic_year: int
    semester: Literal["FIRST", "SECOND"]
    label: str
    total_score: Decimal
    base_score: Decimal
    event_score_sum: Decimal
    grade: str


class MyTrendOut(BaseModel):
    points: list[MyTrendPoint]


def _cycle_label(academic_year: int, semester: str) -> str:
    return f"{academic_year}{'上' if semester == 'FIRST' else '下'}"


@router.get("/my-appraisals/trend", response_model=MyTrendOut)
def my_appraisals_trend(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session_dep),
):
    """折線圖資料 — 只回 FINALIZED 且未 rejected 期，按時間 ASC。"""
    emp = _get_employee(session, current_user)
    rows = (
        session.query(AppraisalSummary, AppraisalCycle)
        .join(
            AppraisalParticipant,
            AppraisalSummary.participant_id == AppraisalParticipant.id,
        )
        .join(AppraisalCycle, AppraisalSummary.cycle_id == AppraisalCycle.id)
        .filter(
            AppraisalParticipant.employee_id == emp.id,
            AppraisalSummary.status == SummaryStatus.FINALIZED,
            AppraisalSummary.rejected_at.is_(None),
        )
        .order_by(
            AppraisalCycle.academic_year.asc(),
            AppraisalCycle.semester.asc(),
        )
        .all()
    )
    points = [
        MyTrendPoint(
            cycle_id=cycle.id,
            academic_year=cycle.academic_year,
            semester=cycle.semester.value,
            label=_cycle_label(cycle.academic_year, cycle.semester.value),
            total_score=s.total_score,
            base_score=s.base_score,
            event_score_sum=s.event_score_sum,
            grade=s.grade.value,
        )
        for s, cycle in rows
    ]
    return MyTrendOut(points=points)


# ============ Detail ============


class MyScoreItemOut(BaseModel):
    item_code: str
    label: str
    sign: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"]
    display_order: int
    sequence_no: int
    score_delta: Decimal
    raw_value: Optional[Decimal] = None
    note: Optional[str] = None


class MyAppraisalDetailOut(BaseModel):
    cycle_id: int
    academic_year: int
    semester: Literal["FIRST", "SECOND"]
    role_group: str
    base_score: Decimal
    event_score_sum: Decimal
    total_score: Decimal
    grade: str
    bonus_amount: Decimal
    summary_status: str
    finalized_at: Optional[datetime] = None
    score_items: list[MyScoreItemOut]


@router.get("/my-appraisals/{cycle_id}", response_model=MyAppraisalDetailOut)
def my_appraisal_detail(
    cycle_id: int,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session_dep),
):
    """單期完整明細；非 FINALIZED → 403。"""
    emp = _get_employee(session, current_user)
    row = (
        session.query(AppraisalParticipant, AppraisalCycle, AppraisalSummary)
        .join(AppraisalCycle, AppraisalParticipant.cycle_id == AppraisalCycle.id)
        .outerjoin(
            AppraisalSummary,
            AppraisalSummary.participant_id == AppraisalParticipant.id,
        )
        .filter(
            AppraisalParticipant.employee_id == emp.id,
            AppraisalParticipant.cycle_id == cycle_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="找不到此週期考核資料")
    participant, cycle, summary = row
    if (
        summary is None
        or summary.status != SummaryStatus.FINALIZED
        or summary.rejected_at is not None
    ):
        raise HTTPException(
            status_code=403,
            detail="考核進行中，分數尚未公布",
        )

    # 取 score_items 並 join catalog 拿 label/sign/order
    items_rows = (
        session.query(AppraisalScoreItem, AppraisalScoreItemCatalog)
        .outerjoin(
            AppraisalScoreItemCatalog,
            AppraisalScoreItem.catalog_id == AppraisalScoreItemCatalog.id,
        )
        .filter(AppraisalScoreItem.participant_id == participant.id)
        .all()
    )
    score_items = [
        MyScoreItemOut(
            item_code=si.item_code,
            label=(catalog.label if catalog else si.item_code),
            sign=(catalog.sign.value if catalog else "NEUTRAL"),
            display_order=(catalog.display_order if catalog else 999),
            sequence_no=si.sequence_no,
            score_delta=si.score_delta,
            raw_value=si.raw_value,
            note=si.note,
        )
        for si, catalog in items_rows
    ]
    score_items.sort(key=lambda x: (x.display_order, x.item_code, x.sequence_no))

    return MyAppraisalDetailOut(
        cycle_id=cycle.id,
        academic_year=cycle.academic_year,
        semester=cycle.semester.value,
        role_group=participant.role_group.value,
        base_score=summary.base_score,
        event_score_sum=summary.event_score_sum,
        total_score=summary.total_score,
        grade=summary.grade.value,
        bonus_amount=summary.bonus_amount,
        summary_status=summary.status.value,
        finalized_at=summary.finalized_at,
        score_items=score_items,
    )
