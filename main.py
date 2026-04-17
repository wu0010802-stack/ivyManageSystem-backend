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

# Routers
from api.employees import router as employees_router, init_employee_services
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
from api.employee_allowances import router as employee_allowances_router
from api.auth import router as auth_router
from api.portal import router as portal_router, init_portal_notify_services
from api.shifts import router as shifts_router
from api.events import router as events_router
from api.meetings import router as meetings_router
from api.announcements import router as announcements_router
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
from api.bonus_preview import (
    router as bonus_preview_router,
    init_bonus_preview_services,
)
from api.health import router as health_router

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


def on_startup():
    run_alembic_upgrade()
    run_startup_bootstrap(salary_engine, line_service)
    logger.info("Application started successfully.")


@asynccontextmanager
async def app_lifespan(app_instance: FastAPI):
    on_startup()
    try:
        yield
    finally:
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
CORS_ORIGINS = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env
    else [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ]
)

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
init_webhook_service(line_service)
init_gov_report_services(insurance_service)
init_bonus_preview_services(salary_engine)

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
app.include_router(employee_allowances_router)
app.include_router(auth_router)
app.include_router(portal_router)
app.include_router(shifts_router)
app.include_router(events_router)
app.include_router(meetings_router)
app.include_router(announcements_router)
app.include_router(approvals_router)
app.include_router(notifications_router)
app.include_router(reports_router)
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
app.include_router(bonus_preview_router)
app.include_router(health_router)

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
