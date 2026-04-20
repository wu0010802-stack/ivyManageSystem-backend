"""
Audit log query router
"""

import csv
import io
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import desc
from typing import Optional

from models.database import get_session, AuditLog
from utils.audit import ACTION_LABELS, ENTITY_LABELS
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.search import LIKE_ESCAPE_CHAR, escape_like_pattern

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["audit"])

# 匯出上限：避免寬鬆篩選拉出超大筆數
EXPORT_MAX_ROWS = 10000


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
