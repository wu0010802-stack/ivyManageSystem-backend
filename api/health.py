"""
api/health.py — 健康檢查端點

提供 liveness 與 readiness 探針，供負載均衡器 / K8s 使用。

Readiness shallow（無 query）：SELECT 1，K8s/zeabur 預設 probe 用。回 既有 shape
（status / db / latency_ms）— 不更動，避免破壞既有監控與 LB 設定。
Readiness deep（?deep=1）：另探 LINE / Supabase breaker state + DB pool 飽和度。
SRE 手測 / 排錯用，**不打外網**（讀既有 P1 breaker state + LineTokenHealth 表）。
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from schemas._base import IvyBaseModel
from sqlalchemy import text

from models.base import get_engine, get_session
from models.scheduler_heartbeat import SchedulerHeartbeat
from schemas._common import OkStatusOut
from utils.scheduler_observability import ALERT_THRESHOLD

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


# Deep-only thresholds
_DB_POOL_UTILIZATION_WARN = 0.85
_SUPABASE_PENDING_WARN = 50
_LINE_HEALTH_STALE_HOURS = 26


def _check_line() -> dict:
    """LINE 健康度：breaker state + LineTokenHealth 最新 row。不打外網。"""
    try:
        from utils.circuit_breaker import LINE_BREAKER
    except Exception:
        return {"ok": False, "error": "breaker_import_failed"}

    breaker_stats = LINE_BREAKER.stats
    state = breaker_stats.get("state", "unknown")
    out: dict = {
        "ok": state == "closed",
        "breaker": state,
        "consecutive_failures": breaker_stats.get("consecutive_failures", 0),
    }

    # 補 LineTokenHealth row（不打外網，讀本機表）
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                text(
                    "SELECT healthy, last_check_at, consecutive_failures "
                    "FROM line_token_health WHERE id=1"
                )
            ).first()
        if row is None:
            out["token_health"] = "no_record"
        else:
            out["token_healthy"] = bool(row.healthy)
            out["token_last_check_at"] = (
                row.last_check_at.isoformat() if row.last_check_at else None
            )
            if row.last_check_at:
                age = datetime.now(timezone.utc) - row.last_check_at
                if age > timedelta(hours=_LINE_HEALTH_STALE_HOURS):
                    out["ok"] = False
                    out["stale"] = True
            if not row.healthy:
                out["ok"] = False
    except Exception as e:
        logger.warning("LineTokenHealth query failed: %s", e)
        out["token_health"] = "query_failed"

    return out


def _check_supabase() -> dict:
    """Supabase 健康度：breaker state + pending_uploads 積壓。不打外網。"""
    try:
        from utils.circuit_breaker import SUPABASE_BREAKER
    except Exception:
        return {"ok": False, "error": "breaker_import_failed"}

    breaker_stats = SUPABASE_BREAKER.stats
    state = breaker_stats.get("state", "unknown")
    out: dict = {"ok": state == "closed", "breaker": state}

    try:
        with get_engine().connect() as conn:
            pending = (
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM pending_uploads "
                        "WHERE status='pending' AND attempts < 5"
                    )
                ).scalar()
                or 0
            )
        out["pending_uploads"] = pending
        if pending > _SUPABASE_PENDING_WARN:
            out["ok"] = False
    except Exception as e:
        logger.warning("pending_uploads query failed: %s", e)
        out["pending_uploads"] = None

    return out


def _check_db_pool() -> dict:
    """DB connection pool 飽和度。"""
    try:
        pool = get_engine().pool
        used = pool.checkedout()
        size = pool.size() if hasattr(pool, "size") else -1
    except Exception as e:
        logger.warning("db pool stats failed: %s", e)
        return {"ok": True, "error": "stats_unavailable"}

    utilization = (used / size) if size > 0 else 0.0
    return {
        "ok": utilization <= _DB_POOL_UTILIZATION_WARN,
        "used": used,
        "size": size,
        "utilization": round(utilization, 2),
    }


@router.get("/live", response_model=OkStatusOut)
def liveness():
    """Liveness probe — 程序存活即回 200。"""
    return {"status": "ok"}


async def verify_deep_readiness_access(
    request: Request, deep: bool = Query(False)
) -> None:
    """Deep readiness（?deep=1）存取守衛。

    威脅：deep 分支回傳 db_pool 飽和度（used/size/utilization）、LINE/Supabase
    breaker 內部狀態、parent_portal.detail 等運維敏感明細；整個 router 原本無
    任何認證，未認證攻擊者即可 scrape 後端內部拓撲（資安掃描 2026-06-16 C29）。

    收緊：shallow（無 deep）維持公開（K8s/LB probe 用）；deep 比照
    internal_metrics_router 要求 AUDIT_LOGS 且非 teacher/parent（staff-only）。
    """
    if not deep:
        return None

    from utils.auth import get_current_user
    from utils.permissions import Permission, has_permission

    current_user = await get_current_user(request)
    role = current_user.get("role")
    if role in ("teacher", "parent"):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="此帳號不可存取 deep readiness")
    if not has_permission(current_user.get("permission_names"), Permission.AUDIT_LOGS):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="您沒有此功能的存取權限")
    return None


@router.get("/ready")
async def readiness(
    request: Request,
    deep: bool = Query(False),
    _deep_access: None = Depends(verify_deep_readiness_access),
):
    """Readiness probe.

    Shallow（無 query）— 既有 shape，不更動：
        {"status": "ok", "db": "connected", "latency_ms": X.X}

    Deep（?deep=1）— enriched shape：
        {
          "status": "ok" | "degraded",
          "latency_ms": X.X,
          "components": {
            "db": {...},
            "line": {...},
            "supabase": {...},
            "db_pool": {...},
          },
        }
    """
    start = time.monotonic()

    # cold-start migration 韌性（對標稽核 P1 / boot-loop 止血）：app_lifespan 在 alembic
    # migration 失敗時進維護模式（app.state.migration_ok=False）。此閘優先於 DB 連線檢查
    # （壞 / 半套 schema 下 SELECT 1 仍可能成功，會誤報健康），讓 LB / UptimeRobot 看得到
    # 不健康並停止導流。shallow 與 deep 路徑一致回 503。state 未設時預設 True（保留既有行為）。
    if getattr(request.app.state, "migration_ok", True) is False:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "reason": "migration_failed",
                "detail": getattr(
                    request.app.state, "migration_detail", "啟動 migration 失敗"
                ),
            },
        )

    # Shallow path — 既有行為，逐字保留
    if not deep:
        try:
            engine = get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            return {
                "status": "ok",
                "db": "connected",
                "latency_ms": elapsed_ms,
            }
        except Exception as e:
            logger.error("Readiness check failed: %s", e, exc_info=True)
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "db": "unavailable"},
            )

    # Deep path — components shape
    components: dict[str, dict] = {}

    # DB
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        components["db"] = {"ok": True}
    except Exception as e:
        logger.error("Deep readiness DB check failed: %s", e, exc_info=True)
        components["db"] = {"ok": False, "error": "db_unavailable"}

    # 3 deep components
    components["line"] = _check_line()
    components["supabase"] = _check_supabase()
    components["db_pool"] = _check_db_pool()

    overall_ok = all(c.get("ok") for c in components.values())
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    body = {
        "status": "ok" if overall_ok else "degraded",
        "latency_ms": elapsed_ms,
        "components": components,
        # 家長端 RLS 就緒狀態（資訊欄位，刻意不放進 components、不參與 overall_ok：
        # 家長端 RLS 降級不應拖垮整個後端 readiness 害 admin/staff 一起被斷流量；
        # 系統設計審查 2026-06-14, top#7）。值由 app_lifespan 啟動探測寫入 app.state。
        "parent_portal": {
            "rls_ready": bool(getattr(request.app.state, "parent_rls_ok", False)),
            "detail": getattr(request.app.state, "parent_rls_detail", "unknown"),
        },
    }
    return JSONResponse(
        status_code=200 if overall_ok else 503,
        content=body,
    )


class SchedulersHealthOut(IvyBaseModel):
    """GET /health/schedulers — UptimeRobot 公開 endpoint。

    僅回聚合狀態（HTTP status code + 非敏感計數）。scheduler 名稱 / lag /
    失敗數等明細**不對未認證公開端外洩**（改記 server-side log 供 ops 排查）。
    """

    status: str
    total: int
    lagging_count: int


@router.get("/schedulers", response_model=SchedulersHealthOut)
def schedulers_health():
    """檢查所有 scheduler heartbeat lag。

    無權限端點（UptimeRobot 公開可打）。
    200 = 全綠；503 = 至少一個 scheduler lag > 2 × expected_interval。
    啟動後尚未跑過（last_success_at IS NULL）的視為「未滿足 lag 條件」，回 200。
    """
    now = datetime.now(timezone.utc)
    schedulers: list[dict] = []
    degraded: list[dict] = []
    with get_session() as s:
        rows = s.query(SchedulerHeartbeat).all()
        for row in rows:
            cf = row.consecutive_failures or 0
            if row.last_success_at is None:
                lag_seconds: float | None = None
                is_lagging = False
            else:
                # SQLite 回 naive datetime；PG 回 tz-aware。統一補 UTC 後再相減。
                last_success = row.last_success_at
                if last_success.tzinfo is None:
                    last_success = last_success.replace(tzinfo=timezone.utc)
                lag_seconds = (now - last_success).total_seconds()
                is_lagging = lag_seconds > 2 * row.expected_interval_seconds
            # QW4（設計審查 2026-06-25）：原本只看 lag → 「註冊了但每次都失敗
            # （last_success_at 永遠 NULL）」的 scheduler，consecutive_failures 一直
            # 累積卻仍回 200 綠燈，對外部 watchdog 隱形。改為連續失敗達 ALERT_THRESHOLD
            # （與 LINE/Sentry 告警同門檻）也視為 degraded，與 lag 條件並聯。剛啟動
            # 尚未跑（NULL + 0 失敗）仍視為健康，避免冷啟動誤報。
            is_failing = cf >= ALERT_THRESHOLD
            is_degraded = is_lagging or is_failing
            item = {
                "name": row.scheduler_name,
                "last_success_at": (
                    row.last_success_at.isoformat() if row.last_success_at else None
                ),
                "lag_seconds": lag_seconds,
                "expected_interval_seconds": row.expected_interval_seconds,
                "consecutive_failures": cf,
                "reason": (
                    "lagging" if is_lagging else ("failing" if is_failing else None)
                ),
            }
            schedulers.append(item)
            if is_degraded:
                degraded.append(item)
    total = len(schedulers)
    if degraded:

        def _fmt(x: dict) -> str:
            lag = "NULL" if x["lag_seconds"] is None else f"{x['lag_seconds']:.0f}s"
            return (
                f"{x['name']}(reason={x['reason']},lag={lag},"
                f"fail={x['consecutive_failures']})"
            )

        # 明細（名稱/lag/失敗數）僅入 server log 供 ops 排查，不對未認證公開端外洩
        logger.warning("scheduler 健康降級：%s", ", ".join(_fmt(x) for x in degraded))
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "total": total,
                "lagging_count": len(degraded),
            },
        )
    return {"status": "ok", "total": total, "lagging_count": 0}
