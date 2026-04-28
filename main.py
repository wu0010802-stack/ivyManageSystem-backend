"""
幼稚園考勤薪資系統 - FastAPI 後端
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.insurance_service import InsuranceService
from services.salary_engine import SalaryEngine
from services.line_service import LineService
from services.line_login_service import LineLoginService

# Routers
from api.employees import router as employees_router, init_employee_services
from api.employees_docs import router as employees_docs_router
from api.students import router as students_router
from api.student_attendance import router as student_attendance_router
from api.student_incidents import router as student_incidents_router
from api.student_assessments import router as student_assessments_router
from api.classrooms import router as classrooms_router
from api.attendance import router as attendance_router
from api.salary import router as salary_router, init_salary_services
from api.config import router as config_router, init_config_services
from api.leaves import (
    router as leaves_router,
    init_leaves_services,
    init_leaves_line_service,
)
from api.overtimes import (
    router as overtimes_router,
    init_overtimes_services,
    init_overtimes_line_service,
)
from api.insurance import router as insurance_router, init_insurance_services
from api.auth import router as auth_router
from api.portal import router as portal_router, init_portal_notify_services
from api.shifts import router as shifts_router
from api.events import router as events_router
from api.meetings import router as meetings_router
from api.announcements import (
    router as announcements_router,
    init_announcement_line_service,
)
from api.approvals import router as approvals_router
from api.notifications import router as notifications_router
from api.reports import router as reports_router
from api.exports import router as exports_router
from api.audit import router as audit_router
from api.punch_corrections import router as punch_corrections_router
from api.approval_settings import router as approval_settings_router
from api.activity import router as activity_router, init_activity_services
from api.dismissal_calls import (
    router as dismissal_calls_router,
    init_dismissal_line_service,
)
from api.dismissal_ws import ws_router as dismissal_ws_router
from api.line_webhook import router as line_webhook_router, init_webhook_service
from api.gov_reports import router as gov_reports_router, init_gov_report_services
from api.fees import router as fees_router
from api.recruitment import router as recruitment_router
from api.recruitment_ivykids import router as recruitment_ivykids_router
from api.recruitment_gov_kindergartens import (
    router as recruitment_gov_kindergartens_router,
)
from api.student_enrollment import router as student_enrollment_router
from api.student_change_logs import router as student_change_logs_router
from api.student_communications import router as student_communications_router
from api.bonus_preview import (
    router as bonus_preview_router,
    init_bonus_preview_services,
)
from api.health import router as health_router
from api.attachments import (
    router as attachments_router,
    download_router as attachments_download_router,
)
from api.portfolio import observations_router
from api.student_health import router as student_health_router
from api.parent_portal import (
    parent_router as parent_portal_router,
    admin_router as parent_admin_router,
    init_parent_line_service,
)
from api.student_leaves import (
    router as student_leaves_router,
    init_student_leaves_line_service,
)

# Startup modules
from startup.migrations import run_alembic_upgrade
from startup.bootstrap import run_startup_bootstrap

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging():
    """設定日誌：生產環境使用 JSON 格式，開發環境使用可讀格式。"""
    level = logging.INFO
    if os.environ.get("ENV", "development").lower() in ("production", "prod"):
        try:
            from pythonjsonlogger import jsonlogger

            handler = logging.StreamHandler()
            handler.setFormatter(
                jsonlogger.JsonFormatter(
                    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                    rename_fields={"asctime": "timestamp", "levelname": "level"},
                )
            )
            logging.root.handlers.clear()
            logging.root.addHandler(handler)
            logging.root.setLevel(level)
        except ImportError:
            # python-json-logger 未安裝時 fallback 到純文字
            logging.basicConfig(
                level=level,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )


_configure_logging()
logger = logging.getLogger(__name__)


def _is_production() -> bool:
    return os.environ.get("ENV", "development").lower() in ("production", "prod")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

# Services (singletons)
insurance_service = InsuranceService()
salary_engine = SalaryEngine(load_from_db=True)
line_service = LineService()
# 家長入口 LIFF 認證；channel_id 可為空，端點會回 503 直到正確設定
line_login_service = LineLoginService(
    channel_id=os.environ.get("LINE_LOGIN_CHANNEL_ID", "")
)


def on_startup():
    run_alembic_upgrade()
    run_startup_bootstrap(salary_engine, line_service)
    logger.info("Application started successfully.")


async def _activity_waitlist_sweeper():
    """每 10 分鐘掃描候補轉正過期，發放逾期放棄與即將到期提醒。

    多 worker 部署時以 env ACTIVITY_WAITLIST_SWEEPER_ENABLED=1 在「其中一個」
    worker 上啟用即可，其他 worker 預設不跑避免 Line 通知重發。
    """
    import asyncio
    from services.activity_service import activity_service
    from models.database import get_session

    interval = int(os.getenv("ACTIVITY_WAITLIST_SWEEP_INTERVAL_SECONDS", "600"))
    logger.info("候補過期掃描器啟動，間隔 %s 秒", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            session = get_session()
            try:
                result = activity_service.sweep_expired_pending_promotions(session)
                session.commit()
                if result["expired"] or result["reminded"]:
                    logger.info(
                        "候補過期掃描：expired=%s reminded=%s",
                        result["expired"],
                        result["reminded"],
                    )
            finally:
                session.close()
        except asyncio.CancelledError:
            logger.info("候補過期掃描器收到取消訊號，退出")
            raise
        except Exception:
            logger.exception("候補過期掃描失敗（忽略本次）")


@asynccontextmanager
async def app_lifespan(app_instance: FastAPI):
    import asyncio

    on_startup()
    # 只有 env 啟用時才跑 sweeper（避免多 worker 重複發送通知）
    sweeper_task = None
    if os.getenv("ACTIVITY_WAITLIST_SWEEPER_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        sweeper_task = asyncio.create_task(_activity_waitlist_sweeper())

    # 自動畢業排程：需要 AUTO_GRADUATION_ENABLED=1；建議僅在單一 worker 上啟用
    graduation_task = None
    graduation_stop_event: asyncio.Event | None = None
    try:
        from services import graduation_scheduler as _grad

        if _grad.scheduler_enabled():
            graduation_stop_event = asyncio.Event()
            graduation_task = asyncio.create_task(
                _grad.run_auto_graduation_scheduler(graduation_stop_event)
            )
    except Exception as e:
        logger.warning("自動畢業排程啟動失敗: %s", e)

    # 義華校官網自動同步：需要 IVYKIDS_SYNC_ENABLED=true + 帳密已設
    ivykids_sync_task = None
    ivykids_sync_stop_event: asyncio.Event | None = None
    try:
        from services import recruitment_ivykids_sync as _ivykids_sync

        if _ivykids_sync.scheduler_configured():
            ivykids_sync_stop_event = asyncio.Event()
            ivykids_sync_task = asyncio.create_task(
                _ivykids_sync.run_sync_scheduler(ivykids_sync_stop_event)
            )
    except Exception as e:
        logger.warning("義華校官網自動同步啟動失敗: %s", e)

    # 薪資月底快照排程：需要 SALARY_AUTO_SNAPSHOT_ENABLED=1；建議僅在單一 worker 啟用
    salary_snapshot_task = None
    salary_snapshot_stop_event: asyncio.Event | None = None
    try:
        from services import salary_snapshot_scheduler as _snap_sched

        if _snap_sched.scheduler_enabled():
            salary_snapshot_stop_event = asyncio.Event()
            salary_snapshot_task = asyncio.create_task(
                _snap_sched.run_salary_snapshot_scheduler(salary_snapshot_stop_event)
            )
    except Exception as e:
        logger.warning("薪資月底快照排程啟動失敗: %s", e)

    # 用藥提醒排程：需要 MEDICATION_REMINDER_ENABLED=1；建議僅在單一 worker 啟用
    medication_reminder_task = None
    medication_reminder_stop_event: asyncio.Event | None = None
    try:
        if os.getenv("MEDICATION_REMINDER_ENABLED", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            from services.medication_reminder_scheduler import (
                medication_reminder_loop,
            )

            medication_reminder_stop_event = asyncio.Event()
            medication_reminder_task = asyncio.create_task(
                medication_reminder_loop(medication_reminder_stop_event)
            )
    except Exception as e:
        logger.warning("用藥提醒排程啟動失敗: %s", e)

    # 安全支援表 GC：rate_limit_buckets / jwt_blocklist；預設啟用，env 可關
    security_gc_task = None
    security_gc_stop_event: asyncio.Event | None = None
    try:
        from services import security_gc_scheduler as _sec_gc

        if _sec_gc.scheduler_enabled():
            security_gc_stop_event = asyncio.Event()
            security_gc_task = asyncio.create_task(
                _sec_gc.run_security_gc_scheduler(security_gc_stop_event)
            )
    except Exception as e:
        logger.warning("安全支援表 GC 排程啟動失敗: %s", e)

    try:
        yield
    finally:
        if sweeper_task is not None:
            sweeper_task.cancel()
            try:
                await sweeper_task
            except (asyncio.CancelledError, Exception):
                pass
        if graduation_task is not None:
            if graduation_stop_event is not None:
                graduation_stop_event.set()
            try:
                await asyncio.wait_for(graduation_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                graduation_task.cancel()
                try:
                    await graduation_task
                except (asyncio.CancelledError, Exception):
                    pass
        if ivykids_sync_task is not None:
            if ivykids_sync_stop_event is not None:
                ivykids_sync_stop_event.set()
            try:
                await asyncio.wait_for(ivykids_sync_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                ivykids_sync_task.cancel()
                try:
                    await ivykids_sync_task
                except (asyncio.CancelledError, Exception):
                    pass
        if salary_snapshot_task is not None:
            if salary_snapshot_stop_event is not None:
                salary_snapshot_stop_event.set()
            try:
                await asyncio.wait_for(salary_snapshot_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                salary_snapshot_task.cancel()
                try:
                    await salary_snapshot_task
                except (asyncio.CancelledError, Exception):
                    pass
        if medication_reminder_task is not None:
            if medication_reminder_stop_event is not None:
                medication_reminder_stop_event.set()
            try:
                await asyncio.wait_for(medication_reminder_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                medication_reminder_task.cancel()
                try:
                    await medication_reminder_task
                except (asyncio.CancelledError, Exception):
                    pass
        if security_gc_task is not None:
            if security_gc_stop_event is not None:
                security_gc_stop_event.set()
            try:
                await asyncio.wait_for(security_gc_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                security_gc_task.cancel()
                try:
                    await security_gc_task
                except (asyncio.CancelledError, Exception):
                    pass
        # Graceful Shutdown：釋放資源
        logger.info("Application shutting down — releasing resources…")
        # 關閉所有 WebSocket 連線
        try:
            from api.dismissal_ws import manager as ws_manager

            for ws in list(ws_manager._admin_conns):
                try:
                    import asyncio

                    asyncio.get_event_loop().run_until_complete(
                        ws.close(code=1001, reason="Server shutting down")
                    )
                except Exception:
                    pass
            ws_manager._admin_conns.clear()
            for classroom_conns in ws_manager._teacher_conns.values():
                for ws in list(classroom_conns):
                    try:
                        import asyncio

                        asyncio.get_event_loop().run_until_complete(
                            ws.close(code=1001, reason="Server shutting down")
                        )
                    except Exception:
                        pass
                classroom_conns.clear()
            logger.info("WebSocket 連線已全部關閉")
        except Exception as e:
            logger.warning("WebSocket 關閉時發生錯誤: %s", e)
        # 釋放 DB 連線池
        try:
            from models.base import get_engine as _get_engine

            _get_engine().dispose()
            logger.info("資料庫連線池已釋放")
        except Exception as e:
            logger.warning("資料庫連線池釋放失敗: %s", e)
        logger.info("Application shutdown complete.")


app = FastAPI(
    title="幼稚園考勤薪資系統",
    description="Kindergarten Payroll Management System API",
    version="2.0.0",
    lifespan=app_lifespan,
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

_cors_env = os.environ.get("CORS_ORIGINS", "")
_env_name = os.environ.get("ENV", "development").lower()
_is_prod_env = _env_name in ("production", "prod")

if _cors_env:
    CORS_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()]
elif _is_prod_env:
    raise RuntimeError("CORS_ORIGINS 環境變數未設定，正式環境不允許使用開發預設來源。")
else:
    CORS_ORIGINS = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)

# ---------------------------------------------------------------------------
# Service Injection
# ---------------------------------------------------------------------------

init_salary_services(salary_engine, insurance_service, line_service)
init_employee_services(salary_engine)
init_config_services(salary_engine, line_service)
init_insurance_services(insurance_service)
init_overtimes_services(salary_engine)
init_overtimes_line_service(line_service)
init_leaves_services(salary_engine)
init_leaves_line_service(line_service)
init_dismissal_line_service(line_service)
init_activity_services(line_service)
init_portal_notify_services(line_service)
init_announcement_line_service(line_service)
init_webhook_service(line_service)
init_gov_report_services(insurance_service)
init_bonus_preview_services(salary_engine)
init_parent_line_service(line_login_service)
init_student_leaves_line_service(line_service)

# Ensure data directories exist
os.makedirs("data", exist_ok=True)
os.makedirs("output", exist_ok=True)
# 上傳檔案實體儲存路徑（集中在 data/uploads/，可用 STORAGE_ROOT 覆寫）
from utils.storage import get_storage_path  # noqa: E402

get_storage_path("leave_attachments")
get_storage_path("activity_posters")
get_storage_path("attendance_imports")

# ---------------------------------------------------------------------------
# Register Routers
# ---------------------------------------------------------------------------

app.include_router(employees_router)
app.include_router(employees_docs_router)
app.include_router(students_router)
app.include_router(student_attendance_router)
app.include_router(student_incidents_router)
app.include_router(student_assessments_router)
app.include_router(classrooms_router)
app.include_router(attendance_router)
app.include_router(salary_router)
app.include_router(config_router)
app.include_router(leaves_router)
app.include_router(overtimes_router)
app.include_router(insurance_router)
app.include_router(auth_router)
app.include_router(portal_router)
app.include_router(shifts_router)
app.include_router(events_router)
app.include_router(meetings_router)
app.include_router(announcements_router)
app.include_router(approvals_router)
app.include_router(notifications_router)
app.include_router(reports_router)
from api.analytics import router as analytics_router

app.include_router(analytics_router)
app.include_router(exports_router)
app.include_router(audit_router)
if not _is_production():
    from api.dev import router as dev_router, init_dev_services

    init_dev_services(salary_engine)
    app.include_router(dev_router)
    logger.warning("Dev router 已掛載（/api/dev/*），正式環境請設定 ENV=production")
app.include_router(punch_corrections_router)
app.include_router(approval_settings_router)
app.include_router(activity_router)
app.include_router(dismissal_calls_router)
app.include_router(dismissal_ws_router)  # WebSocket（路徑已含 /ws/...）
app.include_router(line_webhook_router)
app.include_router(gov_reports_router)
app.include_router(fees_router)
app.include_router(recruitment_router)
app.include_router(recruitment_ivykids_router)
app.include_router(recruitment_gov_kindergartens_router)
app.include_router(student_enrollment_router)
app.include_router(student_change_logs_router)
app.include_router(student_communications_router)
app.include_router(bonus_preview_router)
app.include_router(health_router)
# Portfolio / 幼兒成長歷程（Batch A）
app.include_router(attachments_router)
app.include_router(attachments_download_router)
app.include_router(observations_router)
app.include_router(student_health_router)
# 家長入口
app.include_router(parent_portal_router)
app.include_router(parent_admin_router)
app.include_router(student_leaves_router)

# ---------------------------------------------------------------------------
# Middleware（順序重要：最後加入的最先執行）
# ---------------------------------------------------------------------------

from utils.audit import AuditMiddleware

app.add_middleware(AuditMiddleware)

from utils.security_headers import SecurityHeadersMiddleware

app.add_middleware(SecurityHeadersMiddleware)

from utils.request_logging import RequestLoggingMiddleware

app.add_middleware(RequestLoggingMiddleware)

# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return {"message": "幼稚園考勤薪資系統 API", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
