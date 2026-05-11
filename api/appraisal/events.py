"""考核事件流 router（功過 / 扣加分）。"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalEvent,
    AppraisalParticipant,
    AppraisalPenaltyCatalogItem,
    CycleStatus,
    EventType,
)
from models.database import get_session
from schemas.appraisal import EventCreate, EventOut, EventPatch, EventRevert
from services.appraisal_service import (
    check_termination_threshold,
    mark_summary_stale,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_user_employee_id(current_user: dict) -> Optional[int]:
    """從 current_user dict 取對應的 Employee.id（用於自登守衛）。

    JWT payload 在登入時由 api/auth.py 寫入 "employee_id" 欄位（見 api/auth.py line 429）。
    純管理帳號（無對應員工）時此欄位為 None → 不擋。
    """
    return current_user.get("employee_id")


def _guard_event_writable(
    db: Session,
    participant: AppraisalParticipant,
    event_date: date,
    current_user: dict,
) -> AppraisalCycle:
    """驗證事件可寫入的守衛條件。

    Rules:
    1. cycle 必須是 OPEN 狀態（非 LOCKED / CLOSED）
       - LOCKED：spec §4.7 唯讀期間，禁止新增/修改/作廢事件
       - CLOSED：封存後不可變動
    2. event_date 必須落在 cycle.start_date ~ cycle.end_date 內
    3. 自登守衛：不能登錄自己的事件
    """
    cycle = db.get(AppraisalCycle, participant.cycle_id)
    if cycle.status == CycleStatus.CLOSED:
        raise HTTPException(400, "cycle_closed")
    if cycle.status == CycleStatus.LOCKED:
        raise HTTPException(400, "cycle_locked:LOCKED 期間禁止新增/修改事件")
    if not (cycle.start_date <= event_date <= cycle.end_date):
        raise HTTPException(400, "event_date_out_of_cycle")
    actor_emp_id = _resolve_user_employee_id(current_user)
    if actor_emp_id is not None and participant.employee_id == actor_emp_id:
        raise HTTPException(403, "self_event_forbidden:不能登錄自己的事件")
    return cycle


# ── List ──────────────────────────────────────────────────────────────────────


@router.get("/events", response_model=list[EventOut])
def list_events(
    cycle_id: Optional[int] = None,
    participant_id: Optional[int] = None,
    event_type: Optional[EventType] = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    """列出事件。可依 cycle_id / participant_id / event_type 篩選。"""
    stmt = (
        select(AppraisalEvent)
        .order_by(AppraisalEvent.event_date.desc(), AppraisalEvent.id.desc())
        .limit(limit)
    )
    if cycle_id is not None:
        stmt = stmt.where(AppraisalEvent.cycle_id == cycle_id)
    if participant_id is not None:
        stmt = stmt.where(AppraisalEvent.participant_id == participant_id)
    if event_type is not None:
        stmt = stmt.where(AppraisalEvent.event_type == event_type)
    return db.execute(stmt).scalars().all()


# ── Create ────────────────────────────────────────────────────────────────────


@router.post("/events", response_model=EventOut, status_code=201)
def create_event(
    payload: EventCreate,
    request: Request,
    db: Session = Depends(get_session),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_EVENT_WRITE)
    ),
):
    """新增考核事件（功過 / 扣加分）。

    - 自動帶入 catalog_item 的 score_delta / severity_level（可在 payload 中覆寫）
    - 觸發 participant summary 標 stale
    - 達解聘門檻時寫 WARNING log（T15 整合 notification）
    """
    participant = db.get(AppraisalParticipant, payload.participant_id)
    if not participant:
        raise HTTPException(404, "participant_not_found")
    _guard_event_writable(db, participant, payload.event_date, current_user)

    # catalog 帶值（若指定 catalog_item_id）
    catalog_item: Optional[AppraisalPenaltyCatalogItem] = None
    if payload.catalog_item_id is not None:
        catalog_item = db.get(AppraisalPenaltyCatalogItem, payload.catalog_item_id)
        if catalog_item is None or not catalog_item.is_active:
            raise HTTPException(404, "catalog_item_not_found")

    ev = AppraisalEvent(
        participant_id=participant.id,
        cycle_id=participant.cycle_id,
        catalog_item_id=payload.catalog_item_id,
        event_type=payload.event_type,
        event_date=payload.event_date,
        score_delta=payload.score_delta,
        severity_level=payload.severity_level,
        parent_reaction=payload.parent_reaction,
        title=payload.title,
        detail=payload.detail,
        attachments=[],
        created_by=current_user["user_id"],
    )
    db.add(ev)
    db.flush()

    try:
        mark_summary_stale(db, participant.id)
    except PermissionError as e:
        db.rollback()
        raise HTTPException(409, str(e))

    db.commit()
    db.refresh(ev)

    # 解聘門檻檢查（T15 notification 整合）
    # ─────────────────────────────────────────────────────────────────────
    # 調查結果（2026-05-11）：
    #   此 codebase 目前不存在通用 create_notification / notify_users_with_permission。
    #   api/notifications.py 是 **拉取模型**（dashboard pull），非推播 API。
    #   services/line_service.py 的 _push() 為單一群組 target，語義不符
    #   「通知所有 APPRAISAL_FINALIZE 持有者」的需求。
    #
    # 後續整合選項（待業主確認後擇一實作）：
    #   (A) 擴充 dashboard_query_service.build_notification_summary
    #       → 加入 appraisal_termination_threshold section；管理端儀表板
    #         自動顯示警示；不需推播基礎建設。
    #   (B) 新增 per-user in-app notification 表
    #       → 搜尋 APPRAISAL_FINALIZE 持有者、逐一寫入；前端
    #         Notification Center 可輪詢或 WS 推播。
    #   (C) 利用 line_service 逐一 _push_to_user
    #       → 查詢有 APPRAISAL_FINALIZE 且已綁 LINE 的 users；無 LINE
    #         綁定者 silent fail，需搭配 (A) 或 (B) 補救。
    #
    # 現階段：保留 WARNING log，確保事件可被 log aggregation 工具（Loki /
    # CloudWatch）觸發告警規則，直到推播基礎建設確定後替換。
    # ─────────────────────────────────────────────────────────────────────
    if check_termination_threshold(db, participant):
        logger.warning(
            "[appraisal] termination_threshold_reached participant=%d cycle=%d",
            participant.id,
            participant.cycle_id,
        )

    request.state.audit_entity_id = ev.id
    return ev


# ── Patch ─────────────────────────────────────────────────────────────────────


@router.patch("/events/{event_id}", response_model=EventOut)
def patch_event(
    event_id: int,
    payload: EventPatch,
    request: Request,
    db: Session = Depends(get_session),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_EVENT_WRITE)
    ),
):
    """修改事件欄位（已作廢事件不可編輯）。"""
    ev = db.get(AppraisalEvent, event_id)
    if not ev:
        raise HTTPException(404, "event_not_found")
    if ev.reverted_at is not None:
        raise HTTPException(400, "event_reverted_cannot_edit")
    participant = db.get(AppraisalParticipant, ev.participant_id)
    _guard_event_writable(
        db, participant, payload.event_date or ev.event_date, current_user
    )

    if payload.event_type is not None:
        ev.event_type = payload.event_type
    if payload.event_date is not None:
        ev.event_date = payload.event_date
    if payload.score_delta is not None:
        ev.score_delta = payload.score_delta
    if payload.severity_level is not None:
        ev.severity_level = payload.severity_level
    if payload.parent_reaction is not None:
        ev.parent_reaction = payload.parent_reaction
    if payload.title is not None:
        ev.title = payload.title
    if payload.detail is not None:
        ev.detail = payload.detail
    db.flush()

    try:
        mark_summary_stale(db, participant.id)
    except PermissionError as e:
        db.rollback()
        raise HTTPException(409, str(e))

    db.commit()
    db.refresh(ev)
    request.state.audit_entity_id = ev.id
    return ev


# ── Revert ────────────────────────────────────────────────────────────────────


@router.post("/events/{event_id}/revert", response_model=EventOut)
def revert_event(
    event_id: int,
    payload: EventRevert,
    request: Request,
    db: Session = Depends(get_session),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_EVENT_WRITE)
    ),
):
    """軟作廢事件（不真刪；reverted_at + reverted_by + reverted_reason 標記）。"""
    ev = db.get(AppraisalEvent, event_id)
    if not ev:
        raise HTTPException(404, "event_not_found")
    if ev.reverted_at is not None:
        raise HTTPException(400, "already_reverted")
    ev.reverted_at = datetime.now(timezone.utc)
    ev.reverted_by = current_user["user_id"]
    ev.reverted_reason = payload.reason
    db.flush()

    try:
        mark_summary_stale(db, ev.participant_id)
    except PermissionError as e:
        db.rollback()
        raise HTTPException(409, str(e))

    db.commit()
    db.refresh(ev)
    request.state.audit_entity_id = ev.id
    return ev


# ── Attachment upload ─────────────────────────────────────────────────────────
# TODO(T15)：api/attachments.py 的 upload handler 是針對學生 portfolio 的多型附件
# router（owner_type='observation' 等），尚未開放供考核事件使用。
# v1 暫不實作此 endpoint；T15 整合 notification 時一併接入 attachment 上傳。


@router.post("/events/{event_id}/attachments", response_model=EventOut)
async def upload_event_attachment(event_id: int):
    """考核事件附件上傳（v1 暫未實作）。

    api/attachments.py 的 save_uploaded_file helper 尚不存在；
    現有 upload handler 僅支援學生 portfolio owner_type。
    T15（Notification 整合）時一併接入。
    """
    raise HTTPException(
        status_code=501,
        detail="attachment_upload_not_implemented:T15 後續整合",
    )
