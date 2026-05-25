"""
Audit log query router
"""

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, update
from sqlalchemy.orm import Session

from models.database import get_session, AuditLog
from services.audit_high_risk import classify_risk_kind, filter_high_risk
from utils.audit import ACTION_LABELS, ENTITY_LABELS, write_explicit_audit
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.search import LIKE_ESCAPE_CHAR, escape_like_pattern

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["audit"])

# 匯出上限：避免寬鬆篩選拉出超大筆數
EXPORT_MAX_ROWS = 10000

# 列表預設時間窗：未指定 start_at / end_at 時，僅查最近 N 天
# Why（audit H.P0.1）：audit_logs 是 append-only 成長表，全表 COUNT + OFFSET
# 在資料量上萬後會明顯拖累 list 端點。預設 30 天窗口既能讓最常見的「最近操作」
# 查詢瞬時返回，又不阻擋使用者主動指定更長範圍。
LIST_DEFAULT_DAYS = 30


def _parse_changes(raw):
    """changes 欄位是 JSON text，解析失敗則回原字串"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _apply_filters(
    q,
    entity_type,
    action,
    username,
    entity_id,
    ip_address,
    start_at,
    end_at,
):
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if action:
        q = q.filter(AuditLog.action == action)
    if username:
        safe_un = escape_like_pattern(username)
        q = q.filter(AuditLog.username.ilike(f"%{safe_un}%", escape=LIKE_ESCAPE_CHAR))
    if entity_id:
        q = q.filter(AuditLog.entity_id == str(entity_id))
    if ip_address:
        safe_ip = escape_like_pattern(ip_address)
        q = q.filter(AuditLog.ip_address.ilike(f"%{safe_ip}%", escape=LIKE_ESCAPE_CHAR))
    if start_at:
        q = q.filter(AuditLog.created_at >= start_at)
    if end_at:
        q = q.filter(AuditLog.created_at <= end_at)
    return q


@router.get("/audit-logs")
def get_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    username: Optional[str] = None,
    entity_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
):
    """查詢操作審計紀錄"""
    # 未指定時間窗時，預設只看最近 30 天，避免全表 COUNT + OFFSET 隨表成長拖慢
    # （audit H.P0.1）；使用者要更久的歷史只需主動帶 start_at 即可。
    if start_at is None and end_at is None:
        start_at = datetime.now() - timedelta(days=LIST_DEFAULT_DAYS)

    session = get_session()
    try:
        q = _apply_filters(
            session.query(AuditLog),
            entity_type,
            action,
            username,
            entity_id,
            ip_address,
            start_at,
            end_at,
        )

        total = q.count()
        items = (
            q.order_by(desc(AuditLog.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        return {
            "items": [
                {
                    "id": log.id,
                    "user_id": log.user_id,
                    "username": log.username,
                    "action": log.action,
                    "entity_type": log.entity_type,
                    "entity_id": log.entity_id,
                    "summary": log.summary,
                    "changes": _parse_changes(log.changes),
                    "ip_address": log.ip_address,
                    "created_at": (
                        log.created_at.isoformat() if log.created_at else None
                    ),
                }
                for log in items
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    finally:
        session.close()


@router.get("/audit-logs/meta")
def get_audit_logs_meta(
    current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
):
    """回傳可用的 entity_type 與 action 清單（含中文 label），給前端下拉用"""
    return {
        "entity_types": [{"value": k, "label": v} for k, v in ENTITY_LABELS.items()],
        "actions": [{"value": k, "label": v} for k, v in ACTION_LABELS.items()],
    }


@router.get("/audit-logs/export")
def export_audit_logs(
    request: Request,
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    username: Optional[str] = None,
    entity_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
):
    """匯出操作審計紀錄為 CSV。上限 10000 筆，超過請縮小篩選範圍。"""
    session = get_session()
    try:
        q = _apply_filters(
            session.query(AuditLog),
            entity_type,
            action,
            username,
            entity_id,
            ip_address,
            start_at,
            end_at,
        )
        total = q.count()
        if total > EXPORT_MAX_ROWS:
            raise HTTPException(
                status_code=400,
                detail=f"符合條件的紀錄有 {total} 筆，超過匯出上限 {EXPORT_MAX_ROWS} 筆，請縮小篩選範圍",
            )

        items = q.order_by(desc(AuditLog.created_at)).all()

        # F-035：匯出全系統審計軌跡屬高敏感讀取，事件本身需顯式留稽核軌跡
        # （AuditMiddleware 只審計 POST/PUT/PATCH/DELETE，GET 匯出需手動補）
        write_explicit_audit(
            request,
            action="EXPORT",
            entity_type="audit_log",
            summary=(
                f"匯出操作審計紀錄（{len(items)} 筆，篩選："
                f"entity_type={entity_type or '*'}, action={action or '*'}, "
                f"username={username or '*'}）"
            ),
            changes={
                "count": len(items),
                "filters": {
                    "entity_type": entity_type,
                    "action": action,
                    "username": username,
                    "entity_id": entity_id,
                    "ip_address": ip_address,
                    "start_at": start_at.isoformat() if start_at else None,
                    "end_at": end_at.isoformat() if end_at else None,
                },
            },
        )

        buf = io.StringIO()
        # Excel 開 UTF-8 CSV 需要 BOM
        buf.write("\ufeff")
        writer = csv.writer(buf)
        writer.writerow(
            [
                "時間",
                "使用者",
                "操作",
                "資源類型",
                "資源 ID",
                "摘要",
                "變更內容",
                "IP",
            ]
        )
        for log in items:
            writer.writerow(
                [
                    (
                        log.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        if log.created_at
                        else ""
                    ),
                    log.username or "",
                    ACTION_LABELS.get(log.action, log.action),
                    ENTITY_LABELS.get(log.entity_type, log.entity_type),
                    log.entity_id or "",
                    log.summary or "",
                    log.changes or "",
                    log.ip_address or "",
                ]
            )

        filename = f"audit_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        session.close()


# ── 高風險 audit 事件 Pydantic schemas ─────────────────────────────────────────


class AuditLogHighRiskItem(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: Optional[str] = None
    summary: str
    username: str
    created_at: datetime
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[int] = None
    risk_kind: Literal["hard_delete", "blocked", "permission_change"]

    class Config:
        from_attributes = True


class HighRiskListResponse(BaseModel):
    items: list[AuditLogHighRiskItem]
    unack_count: int
    total: int


class AckAllResponse(BaseModel):
    acknowledged_count: int


# ── 高風險 audit 事件 endpoints ────────────────────────────────────────────────
# 注意：ack-all 靜態路徑必須在 {audit_id}/ack 動態路徑之前，否則 FastAPI 會把
# "ack-all" 當成 audit_id 嘗試匹配。


@router.post(
    "/audit-logs/ack-all",
    response_model=AckAllResponse,
    summary="標記所有高風險未 ack 為已 ack",
)
def ack_all_audits(
    request: Request,
    days: int = 7,
    current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
):
    """批次將時間窗內所有未 ack 高風險事件標為已讀。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    user_id = current_user.get("user_id")

    session = get_session()
    try:
        target_ids_q = filter_high_risk(
            sa.select(AuditLog.id), since=since, only_unack=True
        )
        target_ids = [row[0] for row in session.execute(target_ids_q).all()]

        if target_ids:
            session.execute(
                update(AuditLog)
                .where(AuditLog.id.in_(target_ids))
                .values(
                    acknowledged_at=datetime.now(timezone.utc),
                    acknowledged_by=user_id,
                )
            )
            session.commit()

        # ack 動作本身不寫 audit log（避免無限遞迴）
        request.state.audit_skip = True
        return AckAllResponse(acknowledged_count=len(target_ids))
    finally:
        session.close()


@router.post(
    "/audit-logs/{audit_id}/ack",
    summary="標記單筆 audit 為已 ack",
)
def ack_audit(
    audit_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
):
    """將單筆高風險事件標為已讀（idempotent：重複呼叫不覆寫首次 timestamp）。"""
    user_id = current_user.get("user_id")

    session = get_session()
    try:
        row = session.get(AuditLog, audit_id)
        if row is None:
            raise HTTPException(status_code=404, detail="audit log not found")

        if row.acknowledged_at is None:  # idempotent
            row.acknowledged_at = datetime.now(timezone.utc)
            row.acknowledged_by = user_id
            session.commit()

        # ack 動作本身不寫 audit log（避免無限遞迴）
        request.state.audit_skip = True
        return {"ok": True, "id": audit_id, "acknowledged_at": row.acknowledged_at}
    finally:
        session.close()


@router.get(
    "/audit-logs/high-risk",
    response_model=HighRiskListResponse,
    summary="高風險 audit 事件列表（紅點用）",
)
def get_high_risk_audits(
    days: int = 7,
    unack_only: bool = True,
    limit: int = 50,
    current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
):
    """列出時間窗內高風險 audit 事件，含 unack_count 供前端紅點顯示。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    session = get_session()
    try:
        rows_q = filter_high_risk(
            sa.select(AuditLog), since=since, only_unack=unack_only
        ).limit(limit)
        rows = session.execute(rows_q).scalars().all()

        items = [
            AuditLogHighRiskItem(
                id=row.id,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                summary=row.summary or "",
                username=row.username or "",
                created_at=row.created_at,
                acknowledged_at=row.acknowledged_at,
                acknowledged_by=row.acknowledged_by,
                risk_kind=classify_risk_kind(row),
            )
            for row in rows
        ]

        unack_count = (
            session.execute(
                sa.select(sa.func.count()).select_from(
                    filter_high_risk(
                        sa.select(AuditLog), since=since, only_unack=True
                    ).subquery()
                )
            ).scalar()
            or 0
        )

        total = (
            session.execute(
                sa.select(sa.func.count()).select_from(
                    filter_high_risk(
                        sa.select(AuditLog), since=since, only_unack=False
                    ).subquery()
                )
            ).scalar()
            or 0
        )

        return HighRiskListResponse(items=items, unack_count=unack_count, total=total)
    finally:
        session.close()
