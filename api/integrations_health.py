"""api/integrations_health.py — Phase 4 P1 resilience: admin 整合健康狀態.

GET /api/internal/integrations/health
  - 3 circuit breaker state（LINE / Supabase / external_http）
  - LINE token liveness（last_check_at / healthy / consecutive_failures）
  - pending_uploads count（未成功 + attempts<5）
  - line retry pending count（line_next_retry_at IS NOT NULL + retry<3）

權限：AUDIT_LOGS（與 /api/internal/metrics 一致）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.integration_health import LineTokenHealth
from models.pending_uploads import PendingUpload
from utils.auth import require_staff_permission
from utils.circuit_breaker import EXTERNAL_HTTP_BREAKER, LINE_BREAKER, SUPABASE_BREAKER
from utils.permissions import Permission

router = APIRouter(prefix="/api/internal/integrations", tags=["integrations-health"])


class LineHealth(BaseModel):
    breaker: str
    token_healthy: bool | None
    token_last_check_at: str | None
    token_consecutive_failures: int
    retry_pending: int
    retry_final_failed_24h: int


class SupabaseHealth(BaseModel):
    breaker: str
    pending_uploads: int


class ExternalHttpHealth(BaseModel):
    breaker: str


class IntegrationsHealthResponse(BaseModel):
    line: LineHealth
    supabase: SupabaseHealth
    external_http: ExternalHttpHealth


@router.get("/health", response_model=IntegrationsHealthResponse)
def get_integrations_health(
    _current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
    session: Session = Depends(get_session_dep),
) -> IntegrationsHealthResponse:
    """回傳外部整合系統的即時健康狀態。"""
    from models.notification_log import NotificationLog

    token_row = (
        session.query(LineTokenHealth).filter(LineTokenHealth.id == 1).first()
    )

    retry_pending = (
        session.query(NotificationLog)
        .filter(
            NotificationLog.line_next_retry_at.is_not(None),
            NotificationLog.line_retry_count < 3,
        )
        .count()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    retry_final_failed_24h = (
        session.query(NotificationLog)
        .filter(
            NotificationLog.line_retry_count >= 3,
            NotificationLog.created_at >= cutoff,
        )
        .count()
    )

    pending_uploads = (
        session.query(PendingUpload)
        .filter(
            PendingUpload.succeeded_at.is_(None),
            PendingUpload.attempts < 5,
        )
        .count()
    )

    return IntegrationsHealthResponse(
        line=LineHealth(
            breaker=LINE_BREAKER.state,
            token_healthy=token_row.healthy if token_row else None,
            token_last_check_at=(
                token_row.last_check_at.isoformat() if token_row else None
            ),
            token_consecutive_failures=(
                token_row.consecutive_failures if token_row else 0
            ),
            retry_pending=retry_pending,
            retry_final_failed_24h=retry_final_failed_24h,
        ),
        supabase=SupabaseHealth(
            breaker=SUPABASE_BREAKER.state,
            pending_uploads=pending_uploads,
        ),
        external_http=ExternalHttpHealth(breaker=EXTERNAL_HTTP_BREAKER.state),
    )
