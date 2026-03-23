"""
幼稚園考勤薪資系統 - FastAPI 後端
"""

import logging
import os
from pathlib import Path
import subprocess
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect as sa_inspect

from models.database import (
    get_engine, init_database, get_session,
    AttendancePolicy, BonusConfig as DBBonusConfig, GradeTarget, InsuranceRate, JobTitle,
    User, Employee, ShiftType, ApprovalPolicy, LineConfig,
    ActivityRegistrationSettings,
)
from services.insurance_service import InsuranceService
from services.salary_engine import SalaryEngine
from services.line_service import LineService
from utils.permissions import _RW_PAIRS

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
from api.leaves import router as leaves_router, init_leaves_services, init_leaves_line_service
from api.overtimes import router as overtimes_router, init_overtimes_services, init_overtimes_line_service
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
from api.activity import router as activity_router
from api.dismissal_calls import router as dismissal_calls_router, init_dismissal_line_service
from api.dismissal_ws import ws_router as dismissal_ws_router
from api.line_webhook import router as line_webhook_router, init_webhook_service
from api.gov_reports import router as gov_reports_router, init_gov_report_services

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
ALEMBIC_BASELINE_REVISION = "4ddf3ebad3e8"

OFFICIAL_JOB_TITLES = [
    "園長",
    "幼兒園教師",
    "教保員",
    "助理教保員",
    "司機",
    "廚工",
    "職員",
]


def _is_production() -> bool:
    return os.environ.get("ENV", "development").lower() in ("production", "prod")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def on_startup():
    run_startup_bootstrap()
    logger.info("Application started successfully.")


@asynccontextmanager
async def app_lifespan(app_instance: FastAPI):
    on_startup()
    yield


app = FastAPI(
    title="幼稚園考勤薪資系統",
    description="Kindergarten Payroll Management System API",
    version="2.0.0",
    lifespan=app_lifespan,
)

# CORS — 由環境變數 CORS_ORIGINS 控制白名單，逗號分隔
# 例如: CORS_ORIGINS=http://localhost:5173,https://my-app.example.com
_cors_env = os.environ.get("CORS_ORIGINS", "")
CORS_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else [
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
# Services
# ---------------------------------------------------------------------------

insurance_service = InsuranceService()
salary_engine = SalaryEngine(load_from_db=True)
line_service = LineService()

# Initialize service dependencies for routers that need them
init_salary_services(salary_engine, insurance_service, line_service)
init_employee_services(salary_engine)
init_config_services(salary_engine, line_service)
init_insurance_services(insurance_service)
init_overtimes_services(salary_engine)
init_overtimes_line_service(line_service)
init_leaves_services(salary_engine)
init_leaves_line_service(line_service)
init_dismissal_line_service(line_service)
init_portal_notify_services(line_service)
init_webhook_service(line_service)
init_gov_report_services(insurance_service)

# Ensure data directories exist
os.makedirs("data", exist_ok=True)
os.makedirs("output", exist_ok=True)
os.makedirs("uploads/leave_attachments", exist_ok=True)

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

# Audit middleware (must be added after CORS middleware)
from utils.audit import AuditMiddleware
app.add_middleware(AuditMiddleware)

# Security headers middleware（X-Content-Type-Options、X-Frame-Options、HSTS 等）
from utils.security_headers import SecurityHeadersMiddleware
app.add_middleware(SecurityHeadersMiddleware)

# ---------------------------------------------------------------------------
# Seed Data
# ---------------------------------------------------------------------------


def seed_job_titles():
    session = get_session()
    try:
        existing_titles = {jt.name: jt for jt in session.query(JobTitle).all()}
        changed = False

        for i, name in enumerate(OFFICIAL_JOB_TITLES, start=1):
            job_title = existing_titles.get(name)
            if job_title:
                if not job_title.is_active or job_title.sort_order != i:
                    job_title.is_active = True
                    job_title.sort_order = i
                    changed = True
            else:
                session.add(JobTitle(name=name, sort_order=i, is_active=True))
                changed = True

        official_set = set(OFFICIAL_JOB_TITLES)
        for name, legacy_title in existing_titles.items():
            if name not in official_set and legacy_title.is_active:
                changed = True
                legacy_title.is_active = False

        if changed:
            session.commit()
            logger.info("Job titles synced to official bureau list.")
    finally:
        session.close()


def seed_default_configs():
    """初始化預設系統設定"""
    session = get_session()
    try:
        if session.query(AttendancePolicy).count() == 0:
            policy = AttendancePolicy(
                default_work_start="08:00",
                default_work_end="17:00",
                late_deduction=50,
                early_leave_deduction=50,
                missing_punch_deduction=0,
                festival_bonus_months=3,
                is_active=True,
            )
            session.add(policy)
            logger.info("Seeded default attendance policy.")

        if session.query(DBBonusConfig).count() == 0:
            config = DBBonusConfig(
                config_year=2026,
                head_teacher_ab=2000,
                head_teacher_c=1500,
                assistant_teacher_ab=1200,
                assistant_teacher_c=1200,
                principal_festival=6500,
                director_festival=3500,
                leader_festival=2000,
                driver_festival=1000,
                designer_festival=1000,
                admin_festival=2000,
                principal_dividend=5000,
                director_dividend=4000,
                leader_dividend=3000,
                vice_leader_dividend=1500,
                overtime_head_normal=400,
                overtime_head_baby=450,
                overtime_assistant_normal=100,
                overtime_assistant_baby=150,
                school_wide_target=160,
                is_active=True,
            )
            session.add(config)
            logger.info("Seeded default bonus config.")

        if session.query(GradeTarget).count() == 0:
            grade_targets = [
                {"grade_name": "大班", "festival_two_teachers": 27, "festival_one_teacher": 14, "festival_shared": 20,
                 "overtime_two_teachers": 25, "overtime_one_teacher": 13, "overtime_shared": 20},
                {"grade_name": "中班", "festival_two_teachers": 25, "festival_one_teacher": 13, "festival_shared": 18,
                 "overtime_two_teachers": 23, "overtime_one_teacher": 12, "overtime_shared": 18},
                {"grade_name": "小班", "festival_two_teachers": 23, "festival_one_teacher": 12, "festival_shared": 16,
                 "overtime_two_teachers": 21, "overtime_one_teacher": 11, "overtime_shared": 16},
                {"grade_name": "幼幼班", "festival_two_teachers": 15, "festival_one_teacher": 7, "festival_shared": 12,
                 "overtime_two_teachers": 14, "overtime_one_teacher": 7, "overtime_shared": 12},
            ]
            for gt in grade_targets:
                session.add(GradeTarget(config_year=2026, **gt))
            logger.info("Seeded default grade targets.")

        if session.query(InsuranceRate).count() == 0:
            rate = InsuranceRate(
                rate_year=2026,
                labor_rate=0.125,
                labor_employee_ratio=0.20,
                labor_employer_ratio=0.70,
                labor_government_ratio=0.10,
                health_rate=0.0517,
                health_employee_ratio=0.30,
                health_employer_ratio=0.60,
                pension_employer_rate=0.06,
                average_dependents=0.56,
                is_active=True,
            )
            session.add(rate)
            logger.info("Seeded default insurance rates.")

        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def seed_shift_types():
    """初始化預設班別"""
    session = get_session()
    try:
        if session.query(ShiftType).count() == 0:
            defaults = [
                {"name": "早值", "work_start": "08:00", "work_end": "17:00", "sort_order": 1},
                {"name": "正值(班導)", "work_start": "09:00", "work_end": "18:00", "sort_order": 2},
                {"name": "正值(副班導)", "work_start": "07:00", "work_end": "16:30", "sort_order": 3},
                {"name": "次值", "work_start": "08:30", "work_end": "18:00", "sort_order": 4},
                {"name": "無值週", "work_start": "08:00", "work_end": "17:30", "sort_order": 5},
                {"name": "早車", "work_start": "07:00", "work_end": "16:30", "sort_order": 6},
                {"name": "晚車", "work_start": "08:30", "work_end": "18:00", "sort_order": 7},
                {"name": "早上等接", "work_start": "07:30", "work_end": "17:00", "sort_order": 8},
            ]
            for st in defaults:
                session.add(ShiftType(**st))
            session.commit()
            logger.info("Seeded default shift types.")
    finally:
        session.close()


def seed_default_admin():
    """建立初始管理員帳號。

    優先從環境變數讀取帳密：
      ADMIN_INIT_USERNAME  （預設: admin）
      ADMIN_INIT_PASSWORD  （正式環境必須設定）

    正式環境（ENV=production）若未設定 ADMIN_INIT_PASSWORD，
    則不自動建立帳號，避免弱密碼遺留——請部署後手動透過環境變數設定。
    開發環境退而使用預設值 admin/admin123，並強制標記 must_change_password。
    """
    from utils.auth import hash_password

    # 若已存在任何 admin 帳號，跳過
    session = get_session()
    try:
        if session.query(User).filter(User.role == "admin").count() > 0:
            return
    finally:
        session.close()

    init_username = os.environ.get("ADMIN_INIT_USERNAME", "").strip()
    init_password = os.environ.get("ADMIN_INIT_PASSWORD", "").strip()

    if not init_password:
        if _is_production():
            logger.error(
                "正式環境尚未設定 ADMIN_INIT_PASSWORD，"
                "系統不會自動建立管理員帳號。"
                "請設定環境變數後重新啟動：\n"
                "  ADMIN_INIT_USERNAME=<帳號>  ADMIN_INIT_PASSWORD=<強密碼>"
            )
            return
        else:
            # 開發環境：使用眾所周知的預設值，但強制下次登入修改
            init_username = init_username or "admin"
            init_password = "admin123"
            logger.warning(
                "開發環境使用預設管理員帳號 admin/admin123，"
                "已標記 must_change_password=True。請勿在正式環境使用！"
            )
            must_change = True
    else:
        init_username = init_username or "admin"
        must_change = False

    session = get_session()
    try:
        # 確保至少有一位員工可以關聯
        emp = session.query(Employee).first()
        if not emp:
            emp = Employee(
                employee_id="ADMIN001",
                name="系統管理員",
                position="管理員",
            )
            session.add(emp)
            session.flush()

        admin_user = User(
            employee_id=emp.id,
            username=init_username,
            password_hash=hash_password(init_password),
            role="admin",
            permissions=-1,  # admin 擁有全部權限
            must_change_password=must_change,
        )
        session.add(admin_user)
        session.commit()
        logger.info("已建立初始管理員帳號：%s（linked to %s）", init_username, emp.name)
    finally:
        session.close()


def seed_approval_policies():
    """初始化預設審核政策（若表為空則 seed 4 筆預設值）"""
    from api.approval_settings import DEFAULT_POLICIES
    session = get_session()
    try:
        if session.query(ApprovalPolicy).count() == 0:
            for p in DEFAULT_POLICIES:
                session.add(ApprovalPolicy(
                    doc_type="all",
                    submitter_role=p["submitter_role"],
                    approver_roles=p["approver_roles"],
                    is_active=True,
                ))
            session.commit()
            logger.info("Seeded default approval policies.")
    finally:
        session.close()


def seed_activity_settings():
    """初始化課後才藝報名設定 singleton（若不存在則建立）"""
    session = get_session()
    try:
        if session.query(ActivityRegistrationSettings).count() == 0:
            session.add(ActivityRegistrationSettings(
                is_open=False,
                open_at=None,
                close_at=None,
            ))
            session.commit()
            logger.info("Seeded default activity registration settings.")
    finally:
        session.close()


def migrate_permissions_rw():
    """為既有非全權用戶自動補上 _WRITE 位元（冪等）"""
    session = get_session()
    try:
        users = session.query(User).filter(User.permissions != -1).all()
        updated = 0
        for user in users:
            old = user.permissions
            new = old
            for read_bit, write_bit in _RW_PAIRS:
                if (old & read_bit.value) == read_bit.value:
                    new = new | write_bit.value
            if new != old:
                user.permissions = new
                updated += 1
        if updated:
            session.commit()
            logger.info(f"migrate_permissions_rw: 已更新 {updated} 位用戶的 WRITE 權限位元")
        else:
            logger.info("migrate_permissions_rw: 無需遷移（所有用戶已是最新）")
    finally:
        session.close()


def _load_line_config():
    """啟動時從 DB 載入 LINE 通知設定"""
    session = get_session()
    try:
        cfg = session.query(LineConfig).first()
        if cfg and cfg.is_enabled and cfg.channel_access_token and cfg.target_id:
            channel_secret = getattr(cfg, "channel_secret", None)
            line_service.configure(cfg.channel_access_token, cfg.target_id, True, channel_secret)
            logger.info("LINE 通知服務已啟用")
        else:
            logger.info("LINE 通知服務未啟用或尚未設定")
    finally:
        session.close()


def needs_alembic_baseline_stamp():
    """舊部署若已有 schema 但未建立 alembic_version，先 stamp baseline。"""
    inspector = sa_inspect(get_engine())
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return False

    user_tables = tables - {"alembic_version"}
    return bool(user_tables)


def run_alembic_upgrade():
    """執行 Alembic schema migration。"""
    backend_root = Path(__file__).resolve().parent
    alembic_bin = shutil.which("alembic")
    if not alembic_bin:
        bundled_alembic = backend_root / "venv_sec" / "bin" / "alembic"
        if bundled_alembic.exists():
            alembic_bin = str(bundled_alembic)
    if not alembic_bin:
        raise RuntimeError("找不到 alembic 可執行檔，請先安裝 backend/requirements.txt 或啟用正確虛擬環境。")

    base_cmd = [
        alembic_bin,
        "-c",
        str(backend_root / "alembic.ini"),
    ]
    if needs_alembic_baseline_stamp():
        logger.info("偵測到既有 schema 但沒有 alembic_version，先 stamp baseline=%s", ALEMBIC_BASELINE_REVISION)
        subprocess.run(
            [*base_cmd, "stamp", ALEMBIC_BASELINE_REVISION],
            cwd=backend_root,
            check=True,
        )

    subprocess.run(
        [*base_cmd, "upgrade", "head"],
        cwd=backend_root,
        check=True,
    )


def run_startup_bootstrap():
    """執行啟動必要任務，不包含 schema/data migration。"""
    init_database()
    seed_job_titles()
    seed_default_configs()
    seed_shift_types()
    seed_default_admin()
    seed_approval_policies()
    seed_activity_settings()
    salary_engine.load_config_from_db()
    _load_line_config()


def run_maintenance_tasks():
    """執行部署/維運任務：schema migration 與資料回填。"""
    run_alembic_upgrade()
    migrate_permissions_rw()


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return {"message": "幼稚園考勤薪資系統 API", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
