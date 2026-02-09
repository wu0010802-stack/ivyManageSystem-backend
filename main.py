"""
幼稚園考勤薪資系統 - FastAPI 後端
"""

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import date
import pandas as pd
import os
import shutil

from services.attendance_parser import AttendanceParser, parse_attendance_file
from services.insurance_service import InsuranceService
from services.salary_engine import SalaryEngine
from models.database import (
    init_database, get_session, Employee, Attendance, SalaryRecord, Student, Classroom, ClassGrade,
    AllowanceType, DeductionType, BonusType, EmployeeAllowance, SalaryItem, JobTitle,
    SystemConfig, AttendancePolicy, BonusConfig as DBBonusConfig, GradeTarget, InsuranceRate
)
from typing import Dict

app = FastAPI(
    title="幼稚園考勤薪資系統",
    description="Kindergarten Payroll Management System API",
    version="1.0.0"
)

# ... (omitted existing code) ...

class JobTitleCreate(BaseModel):
    name: str

# ... (omitted existing code) ...

# ============ Salary Calculation Endpoints ============

@app.post("/api/salaries/calculate")
def calculate_salaries(
    year: int = Query(..., description="Calculate for which year"),
    month: int = Query(..., description="Calculate for which month")
):
    """
    Calculate or Recalculate salaries for all employees for a given month.
    """
    session = get_session()
    try:
        from services.salary_engine import SalaryEngine as Engine
        engine = Engine(session)
        
        # 1. Fetch all active employees
        employees = session.query(Employee).all() # Include inactive? Maybe not for new calculation, but for history?
        # Better to only calculate for employees active in that month or currently active
        # prioritizing currently active for now
        employees = session.query(Employee).filter(Employee.is_active == True).all()

        results = []
        for emp in employees:
            try:
                # Calculate salary for this employee using new process method
                # This fetches data, calculates, saves to DB, and returns breakdown
                salary_record = engine.process_salary_calculation(emp.id, year, month)
                
                # Convert to dict for response
                results.append({
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "base_salary": salary_record.base_salary,
                    "total_allowances": salary_record.total_allowances, 
                    "festival_bonus": salary_record.festival_bonus,
                    "labor_insurance": salary_record.labor_insurance,
                    "health_insurance": salary_record.health_insurance,
                    "total_deductions": salary_record.total_deduction, # singular in dataclass
                    "net_pay": salary_record.net_salary # net_salary in dataclass
                })
            except Exception as e:
                print(f"Error calculating for {emp.name}: {e}")
                # Log error but continue
        
        return results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@app.get("/api/salaries/festival-bonus")
def get_festival_bonus(
    year: int = Query(...),
    month: int = Query(...)
):
    """
    Return breakdown of festival bonus calculation
    """
    session = get_session()
    try:
        from services.salary_engine import SalaryEngine as Engine
        engine = Engine(session)
        
        employees = session.query(Employee).filter(Employee.is_active == True).all()
        results = []
        
        for emp in employees:
             # This logic mimics the frontend legacy logic or calls backend engine if available
             # Assuming backend engine has a breakdown method or we reconstruct it here
             # For now, let's reuse the calculate logic but expose the bonus details
             
             # Actually, the original app calculated bonus in JS (frontend/js/app.js:showFestivalBonusBreakdown)
             # But here we want to move it to backend.
             # We should use SalaryEngine to get the breakdown.
             
             # Let's see if SalaryEngine has a method or we need to invoke calculation
             # For simplicity, we trigger calculation (simulation) and extract bonus parts
             
             # Re-instantiate engine to be safe
             bonus_data = engine.calculate_festival_bonus_breakdown(emp.id, year, month)
             results.append(bonus_data)

        # Sort by category/name
        return results
    except Exception as e:
        # If calculate_festival_bonus_breakdown doesn't exist yet, we might need to add it to SalaryEngine
        # Or implement a basic version here
        print(f"Error getting festival bonus: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@app.get("/api/config/titles")
def get_job_titles():
    session = get_session()
    titles = session.query(JobTitle).filter(JobTitle.is_active == True).order_by(JobTitle.sort_order).all()
    return [{"id": t.id, "name": t.name} for t in titles]

@app.post("/api/config/titles")
def create_job_title(title: JobTitleCreate):
    session = get_session()
    existing = session.query(JobTitle).filter(JobTitle.name == title.name).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            session.commit()
            return {"message": "Job title reactivated", "id": existing.id}
        raise HTTPException(status_code=400, detail="Job title already exists")
    
    new_title = JobTitle(name=title.name, is_active=True)
    session.add(new_title)
    session.commit()
    return {"message": "Job title created", "id": new_title.id}

@app.put("/api/config/titles/{title_id}")
def update_job_title(title_id: int, title: JobTitleCreate):
    session = get_session()
    # Check if name exists for OTHER titles
    existing = session.query(JobTitle).filter(JobTitle.name == title.name, JobTitle.id != title_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Job title name already exists")
        
    db_title = session.query(JobTitle).filter(JobTitle.id == title_id).first()
    if not db_title:
        raise HTTPException(status_code=404, detail="Job title not found")
        
    db_title.name = title.name
    # Ensure it's active if we are updating it
    db_title.is_active = True
    session.commit()
    return {"message": "Job title updated"}

@app.delete("/api/config/titles/{title_id}")
def delete_job_title(title_id: int):
    session = get_session()
    db_title = session.query(JobTitle).filter(JobTitle.id == title_id).first()
    if not db_title:
        raise HTTPException(status_code=404, detail="Job title not found")
        
    # Soft delete
    db_title.is_active = False
    session.commit()
    return {"message": "Job title deleted (soft delete)"}

def seed_job_titles():
    session = get_session()
    if session.query(JobTitle).count() == 0:
        defaults = ["園長", "主任", "組長", "副組長", "幼兒園教師", "教保員", "助理教保員", "行政", "司機", "廚工"]

        for i, name in enumerate(defaults):
            session.add(JobTitle(name=name, sort_order=i))
        session.commit()
        print("Seeded default job titles.")

def seed_default_configs():
    """初始化預設系統設定"""
    session = get_session()

    # 初始化考勤政策
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
            is_active=True
        )
        session.add(policy)
        print("Seeded default attendance policy.")

    # 初始化獎金設定
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
            is_active=True
        )
        session.add(config)
        print("Seeded default bonus config.")

    # 初始化年級目標人數
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
        print("Seeded default grade targets.")

    # 初始化勞健保費率
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
            is_active=True
        )
        session.add(rate)
        print("Seeded default insurance rates.")

    session.commit()
    session.close()


@app.on_event("startup")
def on_startup():
    init_database()
    seed_job_titles()
    seed_default_configs()
    # 重新載入設定（因為 salary_engine 在模組載入時就初始化了）
    salary_engine.load_config_from_db()


# ============ System Configuration Endpoints ============

class AttendancePolicyUpdate(BaseModel):
    """考勤政策更新"""
    default_work_start: Optional[str] = None
    default_work_end: Optional[str] = None
    grace_minutes: Optional[int] = None
    late_threshold: Optional[int] = None
    late_deduction: Optional[float] = None
    early_leave_deduction: Optional[float] = None
    missing_punch_deduction: Optional[float] = None
    festival_bonus_months: Optional[int] = None


class BonusConfigUpdate(BaseModel):
    """獎金設定更新"""
    config_year: Optional[int] = None
    head_teacher_ab: Optional[float] = None
    head_teacher_c: Optional[float] = None
    assistant_teacher_ab: Optional[float] = None
    assistant_teacher_c: Optional[float] = None
    principal_festival: Optional[float] = None
    director_festival: Optional[float] = None
    leader_festival: Optional[float] = None
    driver_festival: Optional[float] = None
    designer_festival: Optional[float] = None
    admin_festival: Optional[float] = None
    principal_dividend: Optional[float] = None
    director_dividend: Optional[float] = None
    leader_dividend: Optional[float] = None
    vice_leader_dividend: Optional[float] = None
    overtime_head_normal: Optional[float] = None
    overtime_head_baby: Optional[float] = None
    overtime_assistant_normal: Optional[float] = None
    overtime_assistant_baby: Optional[float] = None
    school_wide_target: Optional[int] = None


class GradeTargetUpdate(BaseModel):
    """年級目標人數更新"""
    grade_name: str
    festival_two_teachers: Optional[int] = None
    festival_one_teacher: Optional[int] = None
    festival_shared: Optional[int] = None
    overtime_two_teachers: Optional[int] = None
    overtime_one_teacher: Optional[int] = None
    overtime_shared: Optional[int] = None


class InsuranceRateUpdate(BaseModel):
    """勞健保費率更新"""
    rate_year: Optional[int] = None
    labor_rate: Optional[float] = None
    labor_employee_ratio: Optional[float] = None
    labor_employer_ratio: Optional[float] = None
    health_rate: Optional[float] = None
    health_employee_ratio: Optional[float] = None
    health_employer_ratio: Optional[float] = None
    pension_employer_rate: Optional[float] = None
    average_dependents: Optional[float] = None


@app.get("/api/config/attendance-policy")
def get_attendance_policy():
    """取得考勤政策設定"""
    session = get_session()
    try:
        policy = session.query(AttendancePolicy).filter(AttendancePolicy.is_active == True).first()
        if not policy:
            return {}
        return {
            "id": policy.id,
            "default_work_start": policy.default_work_start,
            "default_work_end": policy.default_work_end,
            "grace_minutes": policy.grace_minutes,
            "late_threshold": policy.late_threshold,
            "late_deduction": policy.late_deduction,
            "early_leave_deduction": policy.early_leave_deduction,
            "missing_punch_deduction": policy.missing_punch_deduction,
            "festival_bonus_months": policy.festival_bonus_months
        }
    finally:
        session.close()


@app.put("/api/config/attendance-policy")
def update_attendance_policy(data: AttendancePolicyUpdate):
    """更新考勤政策設定"""
    session = get_session()
    try:
        policy = session.query(AttendancePolicy).filter(AttendancePolicy.is_active == True).first()
        if not policy:
            policy = AttendancePolicy(is_active=True)
            session.add(policy)

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(policy, key, value)

        session.commit()
        # 重新載入設定到薪資計算引擎
        salary_engine.load_config_from_db()
        return {"message": "考勤政策更新成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.get("/api/config/bonus")
def get_bonus_config():
    """取得獎金設定"""
    session = get_session()
    try:
        config = session.query(DBBonusConfig).filter(DBBonusConfig.is_active == True).order_by(DBBonusConfig.config_year.desc()).first()
        if not config:
            return {}
        return {
            "id": config.id,
            "config_year": config.config_year,
            "head_teacher_ab": config.head_teacher_ab,
            "head_teacher_c": config.head_teacher_c,
            "assistant_teacher_ab": config.assistant_teacher_ab,
            "assistant_teacher_c": config.assistant_teacher_c,
            "principal_festival": config.principal_festival,
            "director_festival": config.director_festival,
            "leader_festival": config.leader_festival,
            "driver_festival": config.driver_festival,
            "designer_festival": config.designer_festival,
            "admin_festival": config.admin_festival,
            "principal_dividend": config.principal_dividend,
            "director_dividend": config.director_dividend,
            "leader_dividend": config.leader_dividend,
            "vice_leader_dividend": config.vice_leader_dividend,
            "overtime_head_normal": config.overtime_head_normal,
            "overtime_head_baby": config.overtime_head_baby,
            "overtime_assistant_normal": config.overtime_assistant_normal,
            "overtime_assistant_baby": config.overtime_assistant_baby,
            "school_wide_target": config.school_wide_target
        }
    finally:
        session.close()


@app.put("/api/config/bonus")
def update_bonus_config(data: BonusConfigUpdate):
    """更新獎金設定"""
    session = get_session()
    try:
        config = session.query(DBBonusConfig).filter(DBBonusConfig.is_active == True).first()
        if not config:
            config = DBBonusConfig(config_year=2026, is_active=True)
            session.add(config)

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(config, key, value)

        session.commit()
        # 重新載入設定到薪資計算引擎
        salary_engine.load_config_from_db()
        return {"message": "獎金設定更新成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.get("/api/config/grade-targets")
def get_grade_targets():
    """取得年級目標人數設定"""
    session = get_session()
    try:
        targets = session.query(GradeTarget).order_by(GradeTarget.grade_name).all()
        result = {}
        for t in targets:
            result[t.grade_name] = {
                "id": t.id,
                "festival_two_teachers": t.festival_two_teachers,
                "festival_one_teacher": t.festival_one_teacher,
                "festival_shared": t.festival_shared,
                "overtime_two_teachers": t.overtime_two_teachers,
                "overtime_one_teacher": t.overtime_one_teacher,
                "overtime_shared": t.overtime_shared
            }
        return result
    finally:
        session.close()


@app.put("/api/config/grade-targets")
def update_grade_target(data: GradeTargetUpdate):
    """更新年級目標人數設定"""
    session = get_session()
    try:
        target = session.query(GradeTarget).filter(GradeTarget.grade_name == data.grade_name).first()
        if not target:
            target = GradeTarget(config_year=2026, grade_name=data.grade_name)
            session.add(target)

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None and key != 'grade_name':
                setattr(target, key, value)

        session.commit()
        # 重新載入設定到薪資計算引擎
        salary_engine.load_config_from_db()
        return {"message": f"{data.grade_name}目標人數更新成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.get("/api/config/insurance-rates")
def get_insurance_rates():
    """取得勞健保費率設定"""
    session = get_session()
    try:
        rate = session.query(InsuranceRate).filter(InsuranceRate.is_active == True).order_by(InsuranceRate.rate_year.desc()).first()
        if not rate:
            return {}
        return {
            "id": rate.id,
            "rate_year": rate.rate_year,
            "labor_rate": rate.labor_rate,
            "labor_employee_ratio": rate.labor_employee_ratio,
            "labor_employer_ratio": rate.labor_employer_ratio,
            "labor_government_ratio": rate.labor_government_ratio,
            "health_rate": rate.health_rate,
            "health_employee_ratio": rate.health_employee_ratio,
            "health_employer_ratio": rate.health_employer_ratio,
            "pension_employer_rate": rate.pension_employer_rate,
            "average_dependents": rate.average_dependents
        }
    finally:
        session.close()


@app.put("/api/config/insurance-rates")
def update_insurance_rates(data: InsuranceRateUpdate):
    """更新勞健保費率設定"""
    session = get_session()
    try:
        rate = session.query(InsuranceRate).filter(InsuranceRate.is_active == True).first()
        if not rate:
            rate = InsuranceRate(rate_year=2026, is_active=True)
            session.add(rate)

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(rate, key, value)

        session.commit()
        # 重新載入設定到薪資計算引擎
        salary_engine.load_config_from_db()
        return {"message": "勞健保費率更新成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.post("/api/config/reload")
def reload_config():
    """重新從資料庫載入設定到薪資計算引擎"""
    try:
        salary_engine.load_config_from_db()
        return {"message": "設定已重新載入"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config/all")
def get_all_configs():
    """取得所有設定（一次性載入）"""
    session = get_session()
    try:
        # 考勤政策
        policy = session.query(AttendancePolicy).filter(AttendancePolicy.is_active == True).first()
        attendance_policy = None
        if policy:
            attendance_policy = {
                "default_work_start": policy.default_work_start,
                "default_work_end": policy.default_work_end,
                "grace_minutes": policy.grace_minutes,
                "late_threshold": policy.late_threshold,
                "late_deduction": policy.late_deduction,
                "early_leave_deduction": policy.early_leave_deduction,
                "missing_punch_deduction": policy.missing_punch_deduction,
                "festival_bonus_months": policy.festival_bonus_months
            }

        # 獎金設定
        bonus = session.query(DBBonusConfig).filter(DBBonusConfig.is_active == True).first()
        bonus_config = None
        if bonus:
            bonus_config = {
                "config_year": bonus.config_year,
                "head_teacher_ab": bonus.head_teacher_ab,
                "head_teacher_c": bonus.head_teacher_c,
                "assistant_teacher_ab": bonus.assistant_teacher_ab,
                "assistant_teacher_c": bonus.assistant_teacher_c,
                "principal_festival": bonus.principal_festival,
                "director_festival": bonus.director_festival,
                "leader_festival": bonus.leader_festival,
                "driver_festival": bonus.driver_festival,
                "designer_festival": bonus.designer_festival,
                "admin_festival": bonus.admin_festival,
                "principal_dividend": bonus.principal_dividend,
                "director_dividend": bonus.director_dividend,
                "leader_dividend": bonus.leader_dividend,
                "vice_leader_dividend": bonus.vice_leader_dividend,
                "overtime_head_normal": bonus.overtime_head_normal,
                "overtime_head_baby": bonus.overtime_head_baby,
                "overtime_assistant_normal": bonus.overtime_assistant_normal,
                "overtime_assistant_baby": bonus.overtime_assistant_baby,
                "school_wide_target": bonus.school_wide_target
            }

        # 年級目標
        targets = session.query(GradeTarget).all()
        grade_targets = {}
        for t in targets:
            grade_targets[t.grade_name] = {
                "festival_two_teachers": t.festival_two_teachers,
                "festival_one_teacher": t.festival_one_teacher,
                "festival_shared": t.festival_shared,
                "overtime_two_teachers": t.overtime_two_teachers,
                "overtime_one_teacher": t.overtime_one_teacher,
                "overtime_shared": t.overtime_shared
            }

        # 勞健保費率
        rate = session.query(InsuranceRate).filter(InsuranceRate.is_active == True).first()
        insurance_rates = None
        if rate:
            insurance_rates = {
                "rate_year": rate.rate_year,
                "labor_rate": rate.labor_rate,
                "labor_employee_ratio": rate.labor_employee_ratio,
                "labor_employer_ratio": rate.labor_employer_ratio,
                "health_rate": rate.health_rate,
                "health_employee_ratio": rate.health_employee_ratio,
                "health_employer_ratio": rate.health_employer_ratio,
                "pension_employer_rate": rate.pension_employer_rate,
                "average_dependents": rate.average_dependents
            }

        return {
            "attendance_policy": attendance_policy,
            "bonus_config": bonus_config,
            "grade_targets": grade_targets,
            "insurance_rates": insurance_rates
        }
    finally:
        session.close()


# CORS 設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 初始化服務
insurance_service = InsuranceService()
salary_engine = SalaryEngine(load_from_db=True)

# 確保資料目錄存在
os.makedirs("data", exist_ok=True)
os.makedirs("output", exist_ok=True)

# 初始化資料庫 (PostgreSQL)
init_database()


# ============ Pydantic Models ============

class EmployeeCreate(BaseModel):
    employee_id: str
    name: str
    id_number: Optional[str] = None
    employee_type: str = "regular"
    title: Optional[str] = None # Legacy/Display
    job_title_id: Optional[int] = None # New FK
    position: Optional[str] = None
    classroom_id: Optional[int] = None
    base_salary: float = 0
    hourly_rate: float = 0
    supervisor_allowance: float = 0
    teacher_allowance: float = 0
    meal_allowance: float = 0
    transportation_allowance: float = 0
    other_allowance: float = 0
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None
    insurance_salary_level: float = 0
    work_start_time: str = "08:00"
    work_end_time: str = "17:00"
    hire_date: Optional[str] = None
    is_office_staff: bool = False


class EmployeeUpdate(BaseModel):
    employee_id: Optional[str] = None
    name: Optional[str] = None
    id_number: Optional[str] = None
    employee_type: Optional[str] = None
    title: Optional[str] = None
    job_title_id: Optional[int] = None
    position: Optional[str] = None
    classroom_id: Optional[int] = None
    base_salary: Optional[float] = None
    hourly_rate: Optional[float] = None
    supervisor_allowance: Optional[float] = None
    teacher_allowance: Optional[float] = None
    meal_allowance: Optional[float] = None
    transportation_allowance: Optional[float] = None
    other_allowance: Optional[float] = None
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None
    insurance_salary_level: Optional[float] = None
    work_start_time: Optional[str] = None
    work_end_time: Optional[str] = None
    hire_date: Optional[str] = None
    is_office_staff: Optional[bool] = None


class ClassBonusParam(BaseModel):
    classroom_id: int
    target_enrollment: int = 0
    current_enrollment: int = 0


class BonusSettings(BaseModel):
    year: int
    month: int
    target_enrollment: int = 160  # Default global target
    current_enrollment: int = 133 # Default global current
    festival_bonus_base: float = 0
    overtime_bonus_per_student: float = 500
    class_params: List[ClassBonusParam] = []
    position_bonus_base: Optional[Dict[str, float]] = None


class BonusBaseConfig(BaseModel):
    """獎金基數設定"""
    headTeacherAB: float = 2000
    headTeacherC: float = 1500
    assistantTeacherAB: float = 1200
    assistantTeacherC: float = 1200


class GradeTargetConfig(BaseModel):
    """單一年級目標人數設定"""
    twoTeachers: int = 0
    oneTeacher: int = 0
    sharedAssistant: int = 0


class OfficeFestivalBonusBase(BaseModel):
    """司機/美編/行政節慶獎金基數"""
    driver: float = 1000    # 司機
    designer: float = 1000  # 美編
    admin: float = 2000     # 行政


class SupervisorFestivalBonusConfig(BaseModel):
    """主管節慶獎金基數設定"""
    principal: float = 6500   # 園長
    director: float = 3500    # 主任
    leader: float = 2000      # 組長


class SupervisorDividendConfig(BaseModel):
    """主管紅利設定"""
    principal: float = 5000   # 園長
    director: float = 4000    # 主任
    leader: float = 3000      # 組長
    viceLeader: float = 1500  # 副組長


class OvertimePerPersonConfig(BaseModel):
    """超額獎金每人金額設定"""
    headBig: float = 400
    headMid: float = 400
    headSmall: float = 400
    headBaby: float = 450
    assistantBig: float = 100
    assistantMid: float = 100
    assistantSmall: float = 100
    assistantBaby: float = 150


class BonusConfig(BaseModel):
    """完整獎金設定"""
    bonusBase: BonusBaseConfig = BonusBaseConfig()
    targetEnrollment: Dict[str, GradeTargetConfig] = {}
    officeFestivalBonusBase: Optional[OfficeFestivalBonusBase] = None
    supervisorFestivalBonus: Optional[SupervisorFestivalBonusConfig] = None
    supervisorDividend: Optional[SupervisorDividendConfig] = None
    overtimePerPerson: Optional[OvertimePerPersonConfig] = None
    overtimeTarget: Optional[Dict[str, GradeTargetConfig]] = None


class ClassEnrollment(BaseModel):
    """班級在籍人數"""
    classroom_id: int
    current_enrollment: int = 0


class InsuranceTableImport(BaseModel):
    table_type: str = "labor"
    data: List[dict]


class CalculateSalaryRequest(BaseModel):
    year: int
    month: int
    bonus_settings: Optional[BonusSettings] = None
    # 新版設定
    bonus_config: Optional[BonusConfig] = None
    class_enrollments: Optional[List[ClassEnrollment]] = None
    overtime_bonus_per_student: float = 400
    # 辦公室人員用全校超額目標
    school_wide_overtime_target: int = 0


class StudentCreate(BaseModel):
    student_id: str
    name: str
    gender: Optional[str] = None
    birthday: Optional[str] = None
    classroom_id: Optional[int] = None
    enrollment_date: Optional[str] = None
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    address: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    status_tag: Optional[str] = None


class StudentUpdate(BaseModel):
    student_id: Optional[str] = None
    name: Optional[str] = None
    gender: Optional[str] = None
    birthday: Optional[str] = None
    classroom_id: Optional[int] = None
    enrollment_date: Optional[str] = None
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    address: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    status_tag: Optional[str] = None


class AllowanceTypeCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_taxable: bool = True
    sort_order: int = 0

class DeductionTypeCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    category: str = 'other'
    is_employer_paid: bool = False
    sort_order: int = 0

class BonusTypeCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_separate_transfer: bool = False
    sort_order: int = 0

class EmployeeAllowanceCreate(BaseModel):
    allowance_type_id: int
    amount: float
    effective_date: Optional[str] = None
    remark: Optional[str] = None


# ============ API Routes ============

@app.get("/")
async def root():
    return {"message": "幼稚園考勤薪資系統 API", "version": "1.0.0"}


# --- 員工管理 ---

@app.get("/api/employees")
def get_employees(skip: int = 0, limit: int = 100):
    session = get_session()
    try:
        employees = session.query(Employee).offset(skip).limit(limit).all()
        
        result = []
        for emp in employees:
            # Determine strict title to return (from relation)
            display_title = emp.job_title_rel.name if emp.job_title_rel else emp.title
            
            result.append({
                "id": emp.id,
                "employee_id": emp.employee_id,
                "name": emp.name,
                "id_number": emp.id_number,
                "employee_type": emp.employee_type,
                "title": display_title, # Return real title name for frontend display compatibility
                "job_title_id": emp.job_title_id,
                "position": emp.position,
                "classroom_id": emp.classroom_id,
                "base_salary": emp.base_salary,
                "hourly_rate": emp.hourly_rate,
                "supervisor_allowance": emp.supervisor_allowance,
                "teacher_allowance": emp.teacher_allowance,
                "meal_allowance": emp.meal_allowance,
                "transportation_allowance": emp.transportation_allowance,
                "other_allowance": emp.other_allowance,
                "insurance_salary_level": emp.insurance_salary_level,
                "work_start_time": emp.work_start_time,
                "work_end_time": emp.work_end_time,
                "is_active": emp.is_active,
                "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
                "bank_code": emp.bank_code,
                "bank_account": emp.bank_account,
                "bank_account_name": emp.bank_account_name,
                "is_office_staff": emp.is_office_staff
            })
        return result
    finally:
        session.close()


@app.get("/api/employees/{employee_id}")
async def get_employee(employee_id: int):
    """取得單一員工詳細資料"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到該員工")
        
        display_title = employee.job_title_rel.name if employee.job_title_rel else employee.title

        return {
            "id": employee.id,
            "employee_id": employee.employee_id,
            "name": employee.name,
            "id_number": employee.id_number,
            "employee_type": employee.employee_type,
            "title": display_title,
            "job_title_id": employee.job_title_id,
            "position": employee.position,
            "classroom_id": employee.classroom_id,
            "base_salary": employee.base_salary,
            "hourly_rate": employee.hourly_rate,
            "supervisor_allowance": employee.supervisor_allowance,
            "teacher_allowance": employee.teacher_allowance,
            "meal_allowance": employee.meal_allowance,
            "transportation_allowance": employee.transportation_allowance,
            "other_allowance": employee.other_allowance,
            "bank_code": employee.bank_code,
            "bank_account": employee.bank_account,
            "bank_account_name": employee.bank_account_name,
            "insurance_salary_level": employee.insurance_salary_level,
            "work_start_time": employee.work_start_time,
            "work_end_time": employee.work_end_time,
            "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
            "is_active": employee.is_active,
            "is_office_staff": employee.is_office_staff or False
        }
    finally:
        session.close()


@app.post("/api/employees")
async def create_employee(emp: EmployeeCreate):
    """新增員工"""
    session = get_session()
    try:
        # 檢查工號是否重複
        existing = session.query(Employee).filter(Employee.employee_id == emp.employee_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="工號已存在")

        emp_data = emp.dict()
        # 處理日期欄位
        if emp_data.get('hire_date'):
            from datetime import datetime
            emp_data['hire_date'] = datetime.strptime(emp_data['hire_date'], '%Y-%m-%d').date()
        else:
            emp_data.pop('hire_date', None)

        # Sync title string from job_title_id for safety/legacy
        if emp_data.get('job_title_id'):
            job_title = session.query(JobTitle).get(emp_data['job_title_id'])
            if job_title:
                emp_data['title'] = job_title.name
            else:
                raise HTTPException(status_code=400, detail="無效的職稱ID")
        elif 'title' in emp_data: # If job_title_id is not provided, but title is, use it
            pass
        else: # If neither is provided, set title to None
            emp_data['title'] = None

        employee = Employee(**emp_data)
        session.add(employee)
        session.commit()
        return {"message": "員工新增成功", "id": employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()


@app.put("/api/employees/{employee_id}")
async def update_employee(employee_id: int, emp: EmployeeUpdate):
    """更新員工資料"""
    session = get_session()
    try:
        db_employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not db_employee:
            raise HTTPException(status_code=404, detail="找不到該員工")

        update_data = emp.dict(exclude_unset=True)

        # 處理日期欄位
        if 'hire_date' in update_data and update_data['hire_date']:
            from datetime import datetime
            update_data['hire_date'] = datetime.strptime(update_data['hire_date'], '%Y-%m-%d').date()

        for key, value in update_data.items():
            if value is not None:
                if key == 'job_title_id':
                    setattr(db_employee, key, value)
                    # Sync legacy title
                    if value:
                        jt = session.query(JobTitle).get(value)
                        if jt:
                            db_employee.title = jt.name
                        else:
                            raise HTTPException(status_code=400, detail="無效的職稱ID")
                    else:
                        db_employee.title = None # If job_title_id is set to None, clear title
                elif key != 'title': # validation exclude manual title update
                    setattr(db_employee, key, value)
            elif key == 'job_title_id' and value is None: # Allow explicitly setting job_title_id to None
                setattr(db_employee, key, None)
                db_employee.title = None # Clear title if job_title_id is cleared

        session.commit()
        return {"message": "員工資料更新成功", "id": db_employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@app.delete("/api/employees/{employee_id}")
async def delete_employee(employee_id: int):
    """刪除員工（軟刪除，設為離職）"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到該員工")

        employee.is_active = False
        session.commit()
        return {"message": "員工已設為離職", "id": employee.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"刪除失敗: {str(e)}")
    finally:
        session.close()


# --- 學生管理 ---

@app.get("/api/students")
async def get_students():
    """取得所有在讀學生列表"""
    session = get_session()
    try:
        students = session.query(Student).filter(Student.is_active == True).all()
        result = []
        for s in students:
            result.append({
                "id": s.id,
                "student_id": s.student_id,
                "name": s.name,
                "gender": s.gender,
                "birthday": s.birthday.isoformat() if s.birthday else None,
                "classroom_id": s.classroom_id,
                "enrollment_date": s.enrollment_date.isoformat() if s.enrollment_date else None,
                "parent_name": s.parent_name,
                "parent_phone": s.parent_phone,
                "address": s.address,
                "address": s.address,
                "status_tag": s.status_tag,
                "is_active": s.is_active
            })
        return result
    finally:
        session.close()


@app.get("/api/students/{student_id}")
async def get_student(student_id: int):
    """取得單一學生詳細資料"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")
        return {
            "id": student.id,
            "student_id": student.student_id,
            "name": student.name,
            "gender": student.gender,
            "birthday": student.birthday.isoformat() if student.birthday else None,
            "classroom_id": student.classroom_id,
            "enrollment_date": student.enrollment_date.isoformat() if student.enrollment_date else None,
            "parent_name": student.parent_name,
            "parent_phone": student.parent_phone,
            "address": student.address,
            "notes": student.notes,
            "is_active": student.is_active
        }
    finally:
        session.close()


@app.post("/api/students")
async def create_student(item: StudentCreate):
    """新增學生"""
    session = get_session()
    try:
        # 檢查學號是否重複
        existing = session.query(Student).filter(Student.student_id == item.student_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="學號已存在")

        data = item.dict()
        # 處理日期欄位
        if data.get('birthday'):
            from datetime import datetime
            data['birthday'] = datetime.strptime(data['birthday'], '%Y-%m-%d').date()
        else:
            data.pop('birthday', None)
            
        if data.get('enrollment_date'):
            from datetime import datetime
            data['enrollment_date'] = datetime.strptime(data['enrollment_date'], '%Y-%m-%d').date()
        else:
            data.pop('enrollment_date', None)

        student = Student(**data)
        session.add(student)
        session.commit()
        return {"message": "學生新增成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"新增失敗: {str(e)}")
    finally:
        session.close()


@app.put("/api/students/{student_id}")
async def update_student(student_id: int, item: StudentUpdate):
    """更新學生資料"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")

        update_data = item.dict(exclude_unset=True)

        # 處理日期欄位
        if 'birthday' in update_data and update_data['birthday']:
            from datetime import datetime
            update_data['birthday'] = datetime.strptime(update_data['birthday'], '%Y-%m-%d').date()
            
        if 'enrollment_date' in update_data and update_data['enrollment_date']:
            from datetime import datetime
            update_data['enrollment_date'] = datetime.strptime(update_data['enrollment_date'], '%Y-%m-%d').date()

        for key, value in update_data.items():
            if value is not None:
                setattr(student, key, value)

        session.commit()
        return {"message": "學生資料更新成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@app.delete("/api/students/{student_id}")
async def delete_student(student_id: int):
    """刪除學生（軟刪除）"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="找不到該學生")

        student.is_active = False
        session.commit()
        return {"message": "學生已刪除", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"刪除失敗: {str(e)}")
    finally:
        session.close()


# --- 班級管理 ---

@app.get("/api/classrooms")
async def get_classrooms():
    """取得所有班級列表（含老師和學生數）"""
    session = get_session()
    try:
        # 依班級 ID 排序，確保順序一致
        classrooms = session.query(Classroom).filter(Classroom.is_active == True).order_by(Classroom.id).all()
        result = []
        for c in classrooms:
            # 取得年級名稱
            grade = session.query(ClassGrade).filter(ClassGrade.id == c.grade_id).first() if c.grade_id else None

            # 取得班導師
            head_teacher = session.query(Employee).filter(Employee.id == c.head_teacher_id).first() if c.head_teacher_id else None

            # 取得副班導
            assistant_teacher = session.query(Employee).filter(Employee.id == c.assistant_teacher_id).first() if c.assistant_teacher_id else None

            # 取得學生數
            student_count = session.query(Student).filter(
                Student.classroom_id == c.id,
                Student.is_active == True
            ).count()

            # 取得美師
            art_teacher = session.query(Employee).filter(Employee.id == c.art_teacher_id).first() if c.art_teacher_id else None

            result.append({
                "id": c.id,
                "name": c.name,
                "class_code": c.class_code,
                "grade_id": c.grade_id,
                "grade_name": grade.name if grade else None,
                "capacity": c.capacity,
                "current_count": student_count,
                "head_teacher_id": c.head_teacher_id,
                "head_teacher_name": head_teacher.name if head_teacher else None,
                "assistant_teacher_id": c.assistant_teacher_id,
                "assistant_teacher_name": assistant_teacher.name if assistant_teacher else None,
                "art_teacher_id": c.art_teacher_id,
                "art_teacher_name": art_teacher.name if art_teacher else None,
                "is_active": c.is_active
            })
        return result
    finally:
        session.close()


@app.get("/api/classrooms/{classroom_id}")
async def get_classroom(classroom_id: int):
    """取得單一班級詳細資料（含學生列表）"""
    session = get_session()
    try:
        classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        if not classroom:
            raise HTTPException(status_code=404, detail="找不到該班級")

        # 取得年級
        grade = session.query(ClassGrade).filter(ClassGrade.id == classroom.grade_id).first() if classroom.grade_id else None

        # 取得老師
        head_teacher = session.query(Employee).filter(Employee.id == classroom.head_teacher_id).first() if classroom.head_teacher_id else None
        assistant_teacher = session.query(Employee).filter(Employee.id == classroom.assistant_teacher_id).first() if classroom.assistant_teacher_id else None

        # 取得學生列表
        students = session.query(Student).filter(
            Student.classroom_id == classroom_id,
            Student.is_active == True
        ).all()

        student_list = [{
            "id": s.id,
            "student_id": s.student_id,
            "name": s.name,
            "gender": s.gender
        } for s in students]

        return {
            "id": classroom.id,
            "name": classroom.name,
            "grade_id": classroom.grade_id,
            "grade_name": grade.name if grade else None,
            "capacity": classroom.capacity,
            "current_count": len(student_list),
            "head_teacher_id": classroom.head_teacher_id,
            "head_teacher_name": head_teacher.name if head_teacher else None,
            "assistant_teacher_id": classroom.assistant_teacher_id,
            "assistant_teacher_name": assistant_teacher.name if assistant_teacher else None,
            "students": student_list,
            "is_active": classroom.is_active
        }
    finally:
        session.close()


@app.put("/api/classrooms/{classroom_id}")
async def update_classroom(
    classroom_id: int,
    head_teacher_id: Optional[int] = None,
    assistant_teacher_id: Optional[int] = None,
    art_teacher_id: Optional[int] = None
):
    """更新班級老師"""
    session = get_session()
    try:
        classroom = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        if not classroom:
            raise HTTPException(status_code=404, detail="找不到該班級")

        if head_teacher_id is not None:
            classroom.head_teacher_id = head_teacher_id if head_teacher_id > 0 else None
        if assistant_teacher_id is not None:
            classroom.assistant_teacher_id = assistant_teacher_id if assistant_teacher_id > 0 else None
        if art_teacher_id is not None:
            classroom.art_teacher_id = art_teacher_id if art_teacher_id > 0 else None

        session.commit()
        return {"message": "班級更新成功", "id": classroom.id, "name": classroom.name}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")
    finally:
        session.close()


@app.get("/api/grades")
async def get_grades():
    """取得所有年級"""
    session = get_session()
    try:
        grades = session.query(ClassGrade).filter(ClassGrade.is_active == True).order_by(ClassGrade.sort_order.desc()).all()
        return [{
            "id": g.id,
            "name": g.name,
            "age_range": g.age_range
        } for g in grades]
    finally:
        session.close()


@app.get("/api/teachers")
async def get_teachers():
    """取得所有可作為老師的員工"""
    session = get_session()
    try:
        employees = session.query(Employee).filter(Employee.is_active == True).all()
        return [{
            "id": e.id,
            "employee_id": e.employee_id,
            "name": e.name,
            "title": e.title if hasattr(e, 'title') else None
        } for e in employees]
    finally:
        session.close()


# --- 考勤處理 ---

@app.post("/api/attendance/upload")
async def upload_attendance(file: UploadFile = File(...)):
    """上傳打卡記錄 Excel（支持分開的上班/下班時間欄位）"""
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="請上傳 Excel 檔案")

    # 儲存上傳檔案
    file_path = f"data/uploads/{file.filename}"
    os.makedirs("data/uploads", exist_ok=True)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # 解析考勤記錄
    try:
        from datetime import datetime, timedelta

        # 讀取 Excel
        df = pd.read_excel(file_path)

        # 檢查欄位格式
        columns = df.columns.tolist()

        # 新格式：部門, 編號, 姓名, 日期, 星期, 上班時間, 下班時間
        if '上班時間' in columns and '下班時間' in columns:
            session = get_session()
            try:
                # 取得員工對照表
                employees = session.query(Employee).filter(Employee.is_active == True).all()
                emp_by_id = {str(emp.employee_id): emp for emp in employees}
                emp_by_name = {emp.name: emp for emp in employees}

                results_data = {
                    "total": len(df),
                    "success": 0,
                    "failed": 0,
                    "errors": [],
                    "summary": []
                }

                employee_stats = {}

                for idx, row in df.iterrows():
                    try:
                        # 取得員工
                        emp_number = str(row.get('編號', '')).strip()
                        emp_name = str(row.get('姓名', '')).strip()
                        employee = emp_by_id.get(emp_number) or emp_by_name.get(emp_name)

                        if not employee:
                            results_data["failed"] += 1
                            results_data["errors"].append(f"第 {idx+2} 行: 找不到員工 {emp_name}")
                            continue

                        # 解析日期
                        date_val = row.get('日期')
                        if pd.isna(date_val):
                            results_data["failed"] += 1
                            results_data["errors"].append(f"第 {idx+2} 行: 日期為空")
                            continue

                        if isinstance(date_val, str):
                            try:
                                attendance_date = datetime.strptime(date_val, "%Y/%m/%d").date()
                            except:
                                attendance_date = datetime.strptime(date_val, "%Y-%m-%d").date()
                        else:
                            attendance_date = pd.to_datetime(date_val).date()

                        # 解析上班時間
                        punch_in_time = None
                        punch_in_val = row.get('上班時間')
                        if not pd.isna(punch_in_val) and str(punch_in_val).strip():
                            try:
                                time_str = str(punch_in_val).strip()
                                if ':' in time_str:
                                    parts = time_str.split(':')
                                    hour = int(parts[0])
                                    minute = int(parts[1].split('.')[0]) if '.' in parts[1] else int(parts[1])
                                    punch_in_time = datetime.combine(attendance_date, datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M").time())
                            except:
                                pass

                        # 解析下班時間
                        punch_out_time = None
                        punch_out_val = row.get('下班時間')
                        if not pd.isna(punch_out_val) and str(punch_out_val).strip():
                            try:
                                time_str = str(punch_out_val).strip()
                                if ':' in time_str:
                                    parts = time_str.split(':')
                                    hour = int(parts[0])
                                    minute = int(parts[1].split('.')[0]) if '.' in parts[1] else int(parts[1])
                                    punch_out_time = datetime.combine(attendance_date, datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M").time())
                            except:
                                pass

                        # 計算考勤狀態
                        work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
                        work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
                        grace_minutes = 5

                        is_late = False
                        is_early_leave = False
                        is_missing_punch_in = punch_in_time is None
                        is_missing_punch_out = punch_out_time is None
                        late_minutes = 0
                        early_leave_minutes = 0
                        status = "normal"

                        if punch_in_time:
                            work_start_dt = datetime.combine(attendance_date, work_start)
                            grace_dt = work_start_dt + timedelta(minutes=grace_minutes)
                            if punch_in_time > grace_dt:
                                is_late = True
                                late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
                                status = "late"

                        if punch_out_time:
                            work_end_dt = datetime.combine(attendance_date, work_end)
                            if punch_out_time < work_end_dt:
                                is_early_leave = True
                                early_leave_minutes = int((work_end_dt - punch_out_time).total_seconds() / 60)
                                status = "early_leave" if status == "normal" else status + "+early_leave"

                        if is_missing_punch_in:
                            status = "missing" if status == "normal" else status + "+missing_in"
                        if is_missing_punch_out:
                            status = "missing" if status == "normal" else status + "+missing_out"

                        # 儲存到資料庫
                        department = str(row.get('部門', '')).strip()
                        existing = session.query(Attendance).filter(
                            Attendance.employee_id == employee.id,
                            Attendance.attendance_date == attendance_date
                        ).first()

                        if existing:
                            existing.punch_in_time = punch_in_time
                            existing.punch_out_time = punch_out_time
                            existing.status = status
                            existing.is_late = is_late
                            existing.is_early_leave = is_early_leave
                            existing.is_missing_punch_in = is_missing_punch_in
                            existing.is_missing_punch_out = is_missing_punch_out
                            existing.late_minutes = late_minutes
                            existing.early_leave_minutes = early_leave_minutes
                            existing.remark = f"部門: {department}"
                        else:
                            attendance = Attendance(
                                employee_id=employee.id,
                                attendance_date=attendance_date,
                                punch_in_time=punch_in_time,
                                punch_out_time=punch_out_time,
                                status=status,
                                is_late=is_late,
                                is_early_leave=is_early_leave,
                                is_missing_punch_in=is_missing_punch_in,
                                is_missing_punch_out=is_missing_punch_out,
                                late_minutes=late_minutes,
                                early_leave_minutes=early_leave_minutes,
                                remark=f"部門: {department}"
                            )
                            session.add(attendance)

                        results_data["success"] += 1

                        # 統計
                        if emp_name not in employee_stats:
                            employee_stats[emp_name] = {
                                "員工姓名": emp_name,
                                "總出勤天數": 0,
                                "正常天數": 0,
                                "遲到次數": 0,
                                "早退次數": 0,
                                "未打卡(上班)": 0,
                                "未打卡(下班)": 0,
                                "遲到總分鐘": 0
                            }

                        stats = employee_stats[emp_name]
                        stats["總出勤天數"] += 1
                        if status == "normal":
                            stats["正常天數"] += 1
                        if is_late:
                            stats["遲到次數"] += 1
                            stats["遲到總分鐘"] += late_minutes
                        if is_early_leave:
                            stats["早退次數"] += 1
                        if is_missing_punch_in:
                            stats["未打卡(上班)"] += 1
                        if is_missing_punch_out:
                            stats["未打卡(下班)"] += 1

                    except Exception as e:
                        results_data["failed"] += 1
                        results_data["errors"].append(f"第 {idx+2} 行: {str(e)}")

                session.commit()

                summary_data = list(employee_stats.values())

                return {
                    "message": f"考勤記錄匯入完成，成功 {results_data['success']} 筆，失敗 {results_data['failed']} 筆",
                    "summary": summary_data,
                    "anomaly_count": results_data["failed"],
                    "anomalies": results_data["errors"][:20]
                }

            finally:
                session.close()

        else:
            # 舊格式：使用原有解析器
            results, anomaly_df, summary_df = parse_attendance_file(file_path)

            anomaly_df.to_excel("output/anomaly_report.xlsx", index=False)
            summary_df.to_excel("output/attendance_summary.xlsx", index=False)

            summary_data = summary_df.to_dict('records')
            anomaly_data = anomaly_df.to_dict('records')

            return {
                "message": "考勤記錄解析完成",
                "summary": summary_data,
                "anomaly_count": len(anomaly_data),
                "anomalies": anomaly_data[:20]
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"解析失敗: {str(e)}")


@app.get("/api/attendance/anomaly-report")
async def download_anomaly_report():
    """下載異常清單"""
    file_path = "output/anomaly_report.xlsx"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="報表尚未產生")
    return FileResponse(file_path, filename="考勤異常清單.xlsx")


class AttendanceCSVRow(BaseModel):
    """CSV 考勤記錄格式"""
    department: str
    employee_number: str
    name: str
    date: str
    weekday: str
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None


class AttendanceUploadRequest(BaseModel):
    """CSV 考勤上傳請求"""
    records: List[AttendanceCSVRow]
    year: int
    month: int


@app.post("/api/attendance/upload-csv")
async def upload_attendance_csv(request: AttendanceUploadRequest):
    """上傳 CSV 格式考勤記錄並存入資料庫"""
    session = get_session()
    try:
        from datetime import datetime, timedelta

        results = {
            "total": len(request.records),
            "success": 0,
            "failed": 0,
            "errors": [],
            "summary": []
        }

        # 取得所有員工對照表（by employee_id 和 name）
        employees = session.query(Employee).filter(Employee.is_active == True).all()
        emp_by_id = {emp.employee_id: emp for emp in employees}
        emp_by_name = {emp.name: emp for emp in employees}

        # 用於統計的字典
        employee_stats = {}

        for row in request.records:
            try:
                # 查找員工：先用編號，再用姓名
                employee = emp_by_id.get(row.employee_number) or emp_by_name.get(row.name)

                if not employee:
                    results["failed"] += 1
                    results["errors"].append(f"找不到員工: {row.name} (編號: {row.employee_number})")
                    continue

                # 解析日期
                try:
                    attendance_date = datetime.strptime(row.date, "%Y/%m/%d").date()
                except ValueError:
                    try:
                        attendance_date = datetime.strptime(row.date, "%Y-%m-%d").date()
                    except ValueError:
                        results["failed"] += 1
                        results["errors"].append(f"日期格式錯誤: {row.date}")
                        continue

                # 解析上班時間
                punch_in_time = None
                if row.punch_in and row.punch_in.strip():
                    try:
                        time_parts = row.punch_in.strip().split(":")
                        punch_in_time = datetime.combine(
                            attendance_date,
                            datetime.strptime(row.punch_in.strip(), "%H:%M").time()
                        )
                    except ValueError:
                        pass

                # 解析下班時間
                punch_out_time = None
                if row.punch_out and row.punch_out.strip():
                    try:
                        punch_out_time = datetime.combine(
                            attendance_date,
                            datetime.strptime(row.punch_out.strip(), "%H:%M").time()
                        )
                    except ValueError:
                        pass

                # 計算考勤狀態
                work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
                work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
                grace_minutes = 5

                is_late = False
                is_early_leave = False
                is_missing_punch_in = punch_in_time is None
                is_missing_punch_out = punch_out_time is None
                late_minutes = 0
                early_leave_minutes = 0
                status = "normal"

                # 檢查遲到
                if punch_in_time:
                    work_start_dt = datetime.combine(attendance_date, work_start)
                    grace_dt = work_start_dt + timedelta(minutes=grace_minutes)
                    if punch_in_time > grace_dt:
                        is_late = True
                        late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
                        status = "late"

                # 檢查早退
                if punch_out_time:
                    work_end_dt = datetime.combine(attendance_date, work_end)
                    if punch_out_time < work_end_dt:
                        is_early_leave = True
                        early_leave_minutes = int((work_end_dt - punch_out_time).total_seconds() / 60)
                        if status == "normal":
                            status = "early_leave"
                        else:
                            status += "+early_leave"

                # 處理未打卡狀態
                if is_missing_punch_in:
                    status = "missing" if status == "normal" else status + "+missing_in"
                if is_missing_punch_out:
                    status = "missing" if status == "normal" else status + "+missing_out"

                # 檢查是否已存在該日考勤記錄
                existing = session.query(Attendance).filter(
                    Attendance.employee_id == employee.id,
                    Attendance.attendance_date == attendance_date
                ).first()

                if existing:
                    # 更新現有記錄
                    existing.punch_in_time = punch_in_time
                    existing.punch_out_time = punch_out_time
                    existing.status = status
                    existing.is_late = is_late
                    existing.is_early_leave = is_early_leave
                    existing.is_missing_punch_in = is_missing_punch_in
                    existing.is_missing_punch_out = is_missing_punch_out
                    existing.late_minutes = late_minutes
                    existing.early_leave_minutes = early_leave_minutes
                    existing.remark = f"部門: {row.department}"
                else:
                    # 新增記錄
                    attendance = Attendance(
                        employee_id=employee.id,
                        attendance_date=attendance_date,
                        punch_in_time=punch_in_time,
                        punch_out_time=punch_out_time,
                        status=status,
                        is_late=is_late,
                        is_early_leave=is_early_leave,
                        is_missing_punch_in=is_missing_punch_in,
                        is_missing_punch_out=is_missing_punch_out,
                        late_minutes=late_minutes,
                        early_leave_minutes=early_leave_minutes,
                        remark=f"部門: {row.department}"
                    )
                    session.add(attendance)

                results["success"] += 1

                # 統計
                if employee.name not in employee_stats:
                    employee_stats[employee.name] = {
                        "name": employee.name,
                        "total_days": 0,
                        "normal_days": 0,
                        "late_count": 0,
                        "early_leave_count": 0,
                        "missing_punch_in": 0,
                        "missing_punch_out": 0,
                        "total_late_minutes": 0
                    }

                stats = employee_stats[employee.name]
                stats["total_days"] += 1
                if status == "normal":
                    stats["normal_days"] += 1
                if is_late:
                    stats["late_count"] += 1
                    stats["total_late_minutes"] += late_minutes
                if is_early_leave:
                    stats["early_leave_count"] += 1
                if is_missing_punch_in:
                    stats["missing_punch_in"] += 1
                if is_missing_punch_out:
                    stats["missing_punch_out"] += 1

            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"處理記錄時發生錯誤: {str(e)}")

        session.commit()

        # 轉換統計結果
        results["summary"] = list(employee_stats.values())

        return {
            "message": f"考勤記錄匯入完成，成功 {results['success']} 筆，失敗 {results['failed']} 筆",
            "results": results
        }

    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"匯入失敗: {str(e)}")
    finally:
        session.close()


@app.get("/api/attendance/records")
async def get_attendance_records(
    year: int = Query(...),
    month: int = Query(...),
    employee_id: Optional[int] = None
):
    """查詢考勤記錄"""
    session = get_session()
    try:
        from datetime import datetime
        from calendar import monthrange

        # 計算月份的起止日期
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        query = session.query(Attendance, Employee).join(Employee).filter(
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        )

        if employee_id:
            query = query.filter(Attendance.employee_id == employee_id)

        query = query.order_by(Employee.name, Attendance.attendance_date)

        records = query.all()

        result = []
        for att, emp in records:
            result.append({
                "id": att.id,
                "employee_id": emp.id,
                "employee_name": emp.name,
                "employee_number": emp.employee_id,
                "date": att.attendance_date.isoformat(),
                "weekday": ["一", "二", "三", "四", "五", "六", "日"][att.attendance_date.weekday()],
                "punch_in": att.punch_in_time.strftime("%H:%M") if att.punch_in_time else None,
                "punch_out": att.punch_out_time.strftime("%H:%M") if att.punch_out_time else None,
                "status": att.status,
                "is_late": att.is_late,
                "is_early_leave": att.is_early_leave,
                "is_missing_punch_in": att.is_missing_punch_in,
                "is_missing_punch_out": att.is_missing_punch_out,
                "late_minutes": att.late_minutes,
                "early_leave_minutes": att.early_leave_minutes,
                "remark": att.remark
            })

        return result
    finally:
        session.close()

@app.delete("/api/attendance/records/{employee_id}/{date_str}")
def delete_single_attendance(employee_id: int, date_str: str):
    """刪除單筆考勤記錄"""
    session = get_session()
    try:
        from datetime import datetime
        
        # 嘗試解析日期
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.strptime(date_str, "%Y/%m/%d").date()
            
        record = session.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date == target_date
        ).first()
        
        if not record:
            raise HTTPException(status_code=404, detail="找不到該筆考勤記錄")
            
        session.delete(record)
        session.commit()
        return {"message": "刪除成功"}
        
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式錯誤")
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@app.get("/api/attendance/summary")
async def get_attendance_summary(
    year: int = Query(...),
    month: int = Query(...)
):
    """取得考勤統計摘要"""
    session = get_session()
    try:
        from calendar import monthrange
        from sqlalchemy import func

        # 計算月份的起止日期
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        # 取得所有員工
        employees = session.query(Employee).filter(Employee.is_active == True).all()

        result = []
        for emp in employees:
            # 查詢該員工的考勤記錄
            attendances = session.query(Attendance).filter(
                Attendance.employee_id == emp.id,
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date
            ).all()

            if not attendances:
                continue

            total_days = len(attendances)
            normal_days = sum(1 for a in attendances if a.status == "normal")
            late_count = sum(1 for a in attendances if a.is_late)
            early_leave_count = sum(1 for a in attendances if a.is_early_leave)
            missing_punch_in = sum(1 for a in attendances if a.is_missing_punch_in)
            missing_punch_out = sum(1 for a in attendances if a.is_missing_punch_out)
            total_late_minutes = sum(a.late_minutes or 0 for a in attendances)
            total_early_minutes = sum(a.early_leave_minutes or 0 for a in attendances)

            result.append({
                "employee_id": emp.id,
                "employee_name": emp.name,
                "employee_number": emp.employee_id,
                "total_days": total_days,
                "normal_days": normal_days,
                "late_count": late_count,
                "early_leave_count": early_leave_count,
                "missing_punch_in": missing_punch_in,
                "missing_punch_out": missing_punch_out,
                "total_late_minutes": total_late_minutes,
                "total_early_minutes": total_early_minutes
            })

        return result
    finally:
        session.close()


class AttendanceRecordUpdate(BaseModel):
    """單筆考勤記錄更新"""
    employee_id: int
    date: str
    punch_in: Optional[str] = None
    punch_out: Optional[str] = None


@app.post("/api/attendance/record")
async def create_or_update_attendance_record(record: AttendanceRecordUpdate):
    """新增或更新單筆考勤記錄"""
    session = get_session()
    try:
        from datetime import datetime, timedelta

        # 取得員工
        employee = session.query(Employee).filter(Employee.id == record.employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到員工")

        # 解析日期
        try:
            attendance_date = datetime.strptime(record.date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD")

        # 解析上班時間
        punch_in_time = None
        if record.punch_in and record.punch_in.strip():
            try:
                punch_in_time = datetime.combine(
                    attendance_date,
                    datetime.strptime(record.punch_in.strip(), "%H:%M").time()
                )
            except ValueError:
                raise HTTPException(status_code=400, detail="上班時間格式錯誤，請使用 HH:MM")

        # 解析下班時間
        punch_out_time = None
        if record.punch_out and record.punch_out.strip():
            try:
                punch_out_time = datetime.combine(
                    attendance_date,
                    datetime.strptime(record.punch_out.strip(), "%H:%M").time()
                )
            except ValueError:
                raise HTTPException(status_code=400, detail="下班時間格式錯誤，請使用 HH:MM")

        # 計算考勤狀態
        work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
        work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
        grace_minutes = 5

        is_late = False
        is_early_leave = False
        is_missing_punch_in = punch_in_time is None
        is_missing_punch_out = punch_out_time is None
        late_minutes = 0
        early_leave_minutes = 0
        status = "normal"

        if punch_in_time:
            work_start_dt = datetime.combine(attendance_date, work_start)
            grace_dt = work_start_dt + timedelta(minutes=grace_minutes)
            if punch_in_time > grace_dt:
                is_late = True
                late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
                status = "late"

        if punch_out_time:
            work_end_dt = datetime.combine(attendance_date, work_end)
            if punch_out_time < work_end_dt:
                is_early_leave = True
                early_leave_minutes = int((work_end_dt - punch_out_time).total_seconds() / 60)
                status = "early_leave" if status == "normal" else status + "+early_leave"

        if is_missing_punch_in:
            status = "missing" if status == "normal" else status + "+missing_in"
        if is_missing_punch_out:
            status = "missing" if status == "normal" else status + "+missing_out"

        # 查找或創建記錄
        existing = session.query(Attendance).filter(
            Attendance.employee_id == employee.id,
            Attendance.attendance_date == attendance_date
        ).first()

        if existing:
            existing.punch_in_time = punch_in_time
            existing.punch_out_time = punch_out_time
            existing.status = status
            existing.is_late = is_late
            existing.is_early_leave = is_early_leave
            existing.is_missing_punch_in = is_missing_punch_in
            existing.is_missing_punch_out = is_missing_punch_out
            existing.late_minutes = late_minutes
            existing.early_leave_minutes = early_leave_minutes
            message = "考勤記錄已更新"
        else:
            attendance = Attendance(
                employee_id=employee.id,
                attendance_date=attendance_date,
                punch_in_time=punch_in_time,
                punch_out_time=punch_out_time,
                status=status,
                is_late=is_late,
                is_early_leave=is_early_leave,
                is_missing_punch_in=is_missing_punch_in,
                is_missing_punch_out=is_missing_punch_out,
                late_minutes=late_minutes,
                early_leave_minutes=early_leave_minutes
            )
            session.add(attendance)
            message = "考勤記錄已新增"

        session.commit()

        return {
            "message": message,
            "status": status,
            "is_late": is_late,
            "late_minutes": late_minutes,
            "is_early_leave": is_early_leave,
            "early_leave_minutes": early_leave_minutes
        }

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.delete("/api/attendance/record/{employee_id}/{date}")
async def delete_single_attendance_record(employee_id: int, date: str):
    """刪除單筆考勤記錄"""
    session = get_session()
    try:
        from datetime import datetime

        attendance_date = datetime.strptime(date, "%Y-%m-%d").date()

        deleted = session.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date == attendance_date
        ).delete()

        session.commit()

        if deleted:
            return {"message": "考勤記錄已刪除"}
        else:
            raise HTTPException(status_code=404, detail="找不到該考勤記錄")

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@app.delete("/api/attendance/records/{year}/{month}")
async def delete_attendance_records(year: int, month: int):
    """刪除指定月份的所有考勤記錄"""
    session = get_session()
    try:
        from calendar import monthrange

        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        deleted = session.query(Attendance).filter(
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        ).delete()

        session.commit()

        return {"message": f"已刪除 {deleted} 筆考勤記錄"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# --- 設定管理 (津貼/扣款/獎金) ---

@app.get("/api/config/allowance-types")
async def get_allowance_types():
    session = get_session()
    try:
        return session.query(AllowanceType).filter(AllowanceType.is_active == True).order_by(AllowanceType.sort_order).all()
    finally:
        session.close()

@app.post("/api/config/allowance-types")
async def create_allowance_type(item: AllowanceTypeCreate):
    session = get_session()
    try:
        new_item = AllowanceType(**item.dict())
        session.add(new_item)
        session.commit()
        return {"message": "新增成功", "id": new_item.id}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@app.get("/api/config/deduction-types")
async def get_deduction_types():
    session = get_session()
    try:
        return session.query(DeductionType).filter(DeductionType.is_active == True).order_by(DeductionType.sort_order).all()
    finally:
        session.close()

@app.post("/api/config/deduction-types")
async def create_deduction_type(item: DeductionTypeCreate):
    session = get_session()
    try:
        new_item = DeductionType(**item.dict())
        session.add(new_item)
        session.commit()
        return {"message": "新增成功", "id": new_item.id}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

@app.get("/api/config/bonus-types")
async def get_bonus_types():
    session = get_session()
    try:
        return session.query(BonusType).filter(BonusType.is_active == True).order_by(BonusType.sort_order).all()
    finally:
        session.close()

@app.post("/api/config/bonus-types")
async def create_bonus_type(item: BonusTypeCreate):
    session = get_session()
    try:
        new_item = BonusType(**item.dict())
        session.add(new_item)
        session.commit()
        return {"message": "新增成功", "id": new_item.id}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# --- 員工津貼管理 ---

@app.get("/api/employees/{employee_id}/allowances")
async def get_employee_allowances(employee_id: int):
    session = get_session()
    try:
        allowances = session.query(EmployeeAllowance, AllowanceType).join(AllowanceType).filter(
            EmployeeAllowance.employee_id == employee_id,
            EmployeeAllowance.is_active == True
        ).all()
        
        return [{
            "id": ea.id,
            "allowance_type_id": at.id,
            "name": at.name,
            "amount": ea.amount,
            "effective_date": ea.effective_date,
            "remark": ea.remark
        } for ea, at in allowances]
    finally:
        session.close()

@app.post("/api/employees/{employee_id}/allowances")
async def add_employee_allowance(employee_id: int, data: EmployeeAllowanceCreate):
    session = get_session()
    try:
        # 簡單處理：如果已存在相同類型則更新，否則新增
        existing = session.query(EmployeeAllowance).filter(
            EmployeeAllowance.employee_id == employee_id,
            EmployeeAllowance.allowance_type_id == data.allowance_type_id,
            EmployeeAllowance.is_active == True
        ).first()

        if existing:
            existing.amount = data.amount
            existing.effective_date = data.effective_date
            existing.remark = data.remark
        else:
            new_allowance = EmployeeAllowance(
                employee_id=employee_id,
                **data.dict()
            )
            session.add(new_allowance)
        
        session.commit()
        return {"message": "儲存成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# --- 勞健保 ---

@app.post("/api/insurance/import")
async def import_insurance_table(data: InsuranceTableImport):
    """匯入勞健保級距表"""
    success = insurance_service.import_table(data=data.data, table_type=data.table_type)
    if success:
        return {"message": f"{data.table_type} 級距表匯入成功"}
    raise HTTPException(status_code=400, detail="匯入失敗")


@app.get("/api/insurance/calculate")
async def calculate_insurance(salary: float = Query(...), dependents: int = Query(0)):
    """計算勞健保"""
    result = insurance_service.calculate(salary, dependents)
    return {
        "insured_amount": result.insured_amount,
        "labor_employee": result.labor_employee,
        "labor_employer": result.labor_employer,
        "health_employee": result.health_employee,
        "health_employer": result.health_employer,
        "pension_employer": result.pension_employer,
        "total_employee": result.total_employee,
        "total_employer": result.total_employer
    }


# --- 薪資結算 ---

@app.post("/api/salary/calculate")
async def calculate_salaries(request: CalculateSalaryRequest):
    """一鍵結算薪資"""
    session = get_session()
    employees = session.query(Employee).filter(Employee.is_active == True).all()

    # 如果有新版獎金設定，先套用到 salary_engine
    if request.bonus_config:
        bonus_config_dict = request.bonus_config.dict() if hasattr(request.bonus_config, 'dict') else request.bonus_config
        salary_engine.set_bonus_config(bonus_config_dict)

    # 預先抓取所有員工的津貼設定
    all_allowances = session.query(EmployeeAllowance, AllowanceType).join(AllowanceType).filter(
        EmployeeAllowance.is_active == True
    ).all()

    # 將津貼依照 employee_id 分組
    allowance_map = {}
    for ea, at in all_allowances:
        if ea.employee_id not in allowance_map:
            allowance_map[ea.employee_id] = []
        allowance_map[ea.employee_id].append({
            "name": at.name,
            "amount": ea.amount,
            "code": at.code
        })

    # 取得班級資料（含年級）
    classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()

    # 取得年級對照表
    grades = session.query(ClassGrade).all()
    grade_map = {g.id: g.name for g in grades}

    # 建立班級在籍人數對照表（從前端傳入）
    enrollment_map = {}
    if request.class_enrollments:
        for ce in request.class_enrollments:
            enrollment_map[ce.classroom_id] = ce.current_enrollment

    # 建立班級詳細資訊對照表
    classroom_info_map = {}  # classroom_id -> classroom info
    for c in classrooms:
        # 優先使用前端傳入的在籍人數，否則從資料庫計算
        if c.id in enrollment_map:
            student_count = enrollment_map[c.id]
        else:
            student_count = session.query(Student).filter(
                Student.classroom_id == c.id,
                Student.is_active == True
            ).count()

        classroom_info_map[c.id] = {
            "id": c.id,
            "name": c.name,
            "grade_id": c.grade_id,
            "grade_name": grade_map.get(c.grade_id, ''),
            "head_teacher_id": c.head_teacher_id,
            "assistant_teacher_id": c.assistant_teacher_id,
            "art_teacher_id": c.art_teacher_id,
            "has_assistant": c.assistant_teacher_id is not None,
            "current_enrollment": student_count
        }

    # 建立員工角色對照表: emp_id -> [(classroom_id, role), ...]
    # 一個員工可能在多個班級擔任不同角色（如共用副班導跨班）
    # 注意：美師是 part-time，不參與節慶獎金計算
    emp_role_map: Dict[int, list] = {}
    for c in classrooms:
        if c.head_teacher_id:
            if c.head_teacher_id not in emp_role_map:
                emp_role_map[c.head_teacher_id] = []
            emp_role_map[c.head_teacher_id].append((c.id, 'head_teacher'))
        if c.assistant_teacher_id:
            if c.assistant_teacher_id not in emp_role_map:
                emp_role_map[c.assistant_teacher_id] = []
            emp_role_map[c.assistant_teacher_id].append((c.id, 'assistant_teacher'))
        # 美師是 part-time，不加入角色對照表，不參與獎金計算

    # 計算全校在籍人數（用於辦公室人員）
    total_school_enrollment = sum(info['current_enrollment'] for info in classroom_info_map.values())
    school_wide_overtime_target = request.school_wide_overtime_target

    results = []

    # 舊版相容：建立班級參數對照表
    class_bonus_map = {}
    if request.bonus_settings and request.bonus_settings.class_params:
        for p in request.bonus_settings.class_params:
            class_bonus_map[p.classroom_id] = {
                "target": p.target_enrollment,
                "current": p.current_enrollment
            }

    # 舊版獎金設定 (相容性保留)
    global_bonus_settings = None
    if request.bonus_settings:
        global_bonus_settings = {
            "target": request.bonus_settings.target_enrollment,
            "current": request.bonus_settings.current_enrollment,
            "festival_base": request.bonus_settings.festival_bonus_base,
            "overtime_per": request.bonus_settings.overtime_bonus_per_student
        }

    for emp in employees:
        emp_dict = {
            "name": emp.name,
            "employee_id": emp.employee_id,
            "employee_type": emp.employee_type,
            "position": emp.position,
            "title": emp.title,
            "base_salary": emp.base_salary,
            "hourly_rate": emp.hourly_rate,
            "supervisor_allowance": emp.supervisor_allowance,
            "teacher_allowance": emp.teacher_allowance,
            "meal_allowance": emp.meal_allowance,
            "transportation_allowance": emp.transportation_allowance,
            "insurance_salary": emp.insurance_salary_level or emp.base_salary,
            "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
            "is_office_staff": emp.is_office_staff or False
        }

        # 取得該員工的津貼列表
        emp_allowances = allowance_map.get(emp.id, [])

        # 建立 classroom_context（新版節慶獎金計算）
        classroom_context = None
        is_office_staff = emp.is_office_staff or False

        if emp.id in emp_role_map:
            roles = emp_role_map[emp.id]

            # 檢查是否為司機或美編（特殊處理：節慶獎金用全校比例，無超額獎金）
            # 同時檢查 position 和 title
            office_festival_base = salary_engine.get_office_festival_bonus_base(emp.position or '', emp.title or '')

            if office_festival_base is not None:
                # 司機/美編：節慶獎金用全校比例計算，無超額獎金
                is_eligible = salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
                school_festival_bonus = 0

                if is_eligible and school_wide_overtime_target > 0:
                    school_ratio = total_school_enrollment / school_wide_overtime_target
                    school_festival_bonus = office_festival_base * school_ratio

                emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
                emp_dict['_calculated_overtime_bonus'] = 0  # 司機/美編無超額獎金

            # 辦公室人員有帶班：節慶獎金用全校計算，超額獎金用班級計算
            elif is_office_staff and len(roles) > 0:
                # 檢查是否符合領取節慶獎金資格（入職滿3個月）
                is_eligible = salary_engine.is_eligible_for_festival_bonus(emp.hire_date)

                school_festival_bonus = 0
                total_overtime_bonus = 0

                if is_eligible:
                    # 節慶獎金：用全校人數計算
                    if school_wide_overtime_target > 0:
                        # 取得獎金基數（依職位和角色）
                        first_classroom_id, first_role = roles[0]
                        first_classroom_info = classroom_info_map.get(first_classroom_id)
                        if first_classroom_info:
                            role_for_bonus = first_role if first_role != 'art_teacher' else 'assistant_teacher'
                            bonus_base = salary_engine.get_festival_bonus_base(emp.position or '', role_for_bonus)
                            # 全校比例 = 全校在籍 / 全校目標
                            school_ratio = total_school_enrollment / school_wide_overtime_target
                            school_festival_bonus = bonus_base * school_ratio

                    # 超額獎金：依各班計算後加總
                    for classroom_id, role in roles:
                        classroom_info = classroom_info_map.get(classroom_id)
                        if classroom_info:
                            overtime_result = salary_engine.calculate_overtime_bonus(
                                role=role,
                                grade_name=classroom_info['grade_name'],
                                current_enrollment=classroom_info['current_enrollment'],
                                has_assistant=classroom_info['has_assistant'],
                                is_shared_assistant=(role == 'art_teacher')
                            )
                            total_overtime_bonus += overtime_result['overtime_bonus']

                emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
                emp_dict['_calculated_overtime_bonus'] = total_overtime_bonus

            # 美師可能跨多個班級，需要累計多班獎金
            # 其他角色通常只有一個班級
            elif len(roles) == 1:
                # 單一班級
                classroom_id, role = roles[0]
                classroom_info = classroom_info_map.get(classroom_id)
                if classroom_info:
                    classroom_context = {
                        'role': role,
                        'grade_name': classroom_info['grade_name'],
                        'current_enrollment': classroom_info['current_enrollment'],
                        'has_assistant': classroom_info['has_assistant'],
                        'is_shared_assistant': False
                    }
            else:
                # 多班級：依各班計算後加總
                # 判斷是否為「2班共用副班導」- assistant_teacher 跨多班
                assistant_class_count = sum(1 for _, r in roles if r == 'assistant_teacher')
                is_shared_assistant = assistant_class_count > 1

                total_festival_bonus = 0
                total_overtime_bonus = 0

                # 檢查是否符合領取節慶獎金資格（入職滿3個月）
                is_eligible = salary_engine.is_eligible_for_festival_bonus(emp.hire_date)

                if is_eligible:
                    for classroom_id, role in roles:
                        classroom_info = classroom_info_map.get(classroom_id)
                        if classroom_info:
                            # 使用 salary_engine 計算單班獎金
                            # 共用副班導使用 shared_assistant 目標人數
                            bonus_result = salary_engine.calculate_festival_bonus_v2(
                                position=emp.position or '',
                                role=role,
                                grade_name=classroom_info['grade_name'],
                                current_enrollment=classroom_info['current_enrollment'],
                                has_assistant=classroom_info['has_assistant'],
                                is_shared_assistant=(is_shared_assistant and role == 'assistant_teacher')
                            )
                            total_festival_bonus += bonus_result['festival_bonus']
                            total_overtime_bonus += bonus_result['overtime_bonus']

                # 對於多班員工，不使用 classroom_context，直接設定獎金
                emp_dict['_calculated_festival_bonus'] = total_festival_bonus
                emp_dict['_calculated_overtime_bonus'] = total_overtime_bonus

        else:
            # 員工沒有帶班
            # 檢查是否為司機或美編（特殊處理：節慶獎金用全校比例，無超額獎金）
            # 同時檢查 position 和 title
            office_festival_base = salary_engine.get_office_festival_bonus_base(emp.position or '', emp.title or '')

            if office_festival_base is not None:
                # 司機/美編：節慶獎金用全校比例計算，無超額獎金
                is_eligible = salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
                school_festival_bonus = 0

                if is_eligible and school_wide_overtime_target > 0:
                    school_ratio = total_school_enrollment / school_wide_overtime_target
                    school_festival_bonus = office_festival_base * school_ratio

                emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
                emp_dict['_calculated_overtime_bonus'] = 0  # 司機/美編無超額獎金

            elif is_office_staff:
                # 辦公室人員沒有帶班，但仍用全校比例計算節慶獎金
                is_eligible = salary_engine.is_eligible_for_festival_bonus(emp.hire_date)
                school_festival_bonus = 0

                if is_eligible and school_wide_overtime_target > 0:
                    # 使用副班導的獎金基數
                    bonus_base = salary_engine.get_festival_bonus_base(emp.position or '', 'assistant_teacher')
                    school_ratio = total_school_enrollment / school_wide_overtime_target
                    school_festival_bonus = bonus_base * school_ratio

                emp_dict['_calculated_festival_bonus'] = round(school_festival_bonus)
                emp_dict['_calculated_overtime_bonus'] = 0  # 沒有帶班，無超額獎金

        # 決定獎金設定方式
        if '_calculated_festival_bonus' in emp_dict:
            # 多班員工（共用副班導/美師等）：直接使用已計算的獎金
            breakdown = salary_engine.calculate_salary(
                emp_dict,
                request.year,
                request.month,
                bonus_settings=None,
                allowances=emp_allowances,
                classroom_context=None
            )
            breakdown.festival_bonus = emp_dict['_calculated_festival_bonus']
            breakdown.overtime_bonus = emp_dict.get('_calculated_overtime_bonus', 0)
            # 計算主管紅利（同時檢查 title 和 position）
            breakdown.supervisor_dividend = salary_engine.get_supervisor_dividend(emp.title or '', emp.position or '')
            # 重新計算應發總額
            breakdown.gross_salary = (
                breakdown.base_salary +
                breakdown.supervisor_allowance +
                breakdown.teacher_allowance +
                breakdown.meal_allowance +
                breakdown.transportation_allowance +
                breakdown.other_allowance +
                breakdown.festival_bonus +
                breakdown.overtime_bonus +
                breakdown.performance_bonus +
                breakdown.special_bonus +
                breakdown.supervisor_dividend
            )
            breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction
        elif classroom_context:
            # 使用新版計算（有 classroom_context）
            breakdown = salary_engine.calculate_salary(
                emp_dict,
                request.year,
                request.month,
                bonus_settings=None,
                allowances=emp_allowances,
                classroom_context=classroom_context
            )
        else:
            # 使用舊版計算（沒有班級角色，如園長、行政等）
            breakdown = salary_engine.calculate_salary(
                emp_dict,
                request.year,
                request.month,
                bonus_settings=global_bonus_settings,
                allowances=emp_allowances
            )

        results.append(breakdown.__dict__)

    session.close()
    return {"message": "薪資結算完成", "results": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
