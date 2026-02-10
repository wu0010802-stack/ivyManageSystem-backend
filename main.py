"""
幼稚園考勤薪資系統 - FastAPI 後端
"""

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from models.database import (
    init_database, get_session,
    AttendancePolicy, BonusConfig as DBBonusConfig, GradeTarget, InsuranceRate, JobTitle,
    User, Employee, ShiftType,
)
from services.insurance_service import InsuranceService
from services.salary_engine import SalaryEngine

# Routers
from api.employees import router as employees_router
from api.students import router as students_router
from api.classrooms import router as classrooms_router
from api.attendance import router as attendance_router
from api.salary import router as salary_router, init_salary_services
from api.config import router as config_router, init_config_services
from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from api.insurance import router as insurance_router, init_insurance_services
from api.employee_allowances import router as employee_allowances_router
from api.auth import router as auth_router
from api.portal import router as portal_router
from api.shifts import router as shifts_router

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="幼稚園考勤薪資系統",
    description="Kindergarten Payroll Management System API",
    version="2.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

insurance_service = InsuranceService()
salary_engine = SalaryEngine(load_from_db=True)

# Initialize service dependencies for routers that need them
init_salary_services(salary_engine, insurance_service)
init_config_services(salary_engine)
init_insurance_services(insurance_service)

# Ensure data directories exist
os.makedirs("data", exist_ok=True)
os.makedirs("output", exist_ok=True)

# ---------------------------------------------------------------------------
# Register Routers
# ---------------------------------------------------------------------------

app.include_router(employees_router)
app.include_router(students_router)
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

# ---------------------------------------------------------------------------
# Seed Data
# ---------------------------------------------------------------------------


def seed_job_titles():
    session = get_session()
    try:
        if session.query(JobTitle).count() == 0:
            defaults = ["園長", "主任", "組長", "副組長", "幼兒園教師", "教保員", "助理教保員", "行政", "司機", "廚工"]
            for i, name in enumerate(defaults):
                session.add(JobTitle(name=name, sort_order=i))
            session.commit()
            logger.info("Seeded default job titles.")
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
                grace_minutes=5,
                late_threshold=2,
                late_deduction=50,
                early_leave_deduction=50,
                missing_punch_deduction=50,
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
                labor_rate=0.12,
                labor_employee_ratio=0.20,
                labor_employer_ratio=0.70,
                labor_government_ratio=0.10,
                health_rate=0.0517,
                health_employee_ratio=0.30,
                health_employer_ratio=0.60,
                pension_employer_rate=0.06,
                average_dependents=0.57,
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
    """建立預設管理員帳號"""
    from utils.auth import hash_password
    session = get_session()
    try:
        if session.query(User).count() == 0:
            emp = session.query(Employee).first()
            if emp:
                admin_user = User(
                    employee_id=emp.id,
                    username="admin",
                    password_hash=hash_password("admin123"),
                    role="admin",
                )
                session.add(admin_user)
                session.commit()
                logger.info(f"Seeded default admin user (linked to {emp.name}).")
    finally:
        session.close()


@app.on_event("startup")
def on_startup():
    init_database()
    seed_job_titles()
    seed_default_configs()
    seed_shift_types()
    seed_default_admin()
    salary_engine.load_config_from_db()
    logger.info("Application started successfully.")


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return {"message": "幼稚園考勤薪資系統 API", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
