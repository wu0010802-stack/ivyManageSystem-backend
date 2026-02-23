"""
System configuration router
"""

import logging
from typing import Optional

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_permission
from utils.permissions import Permission
from pydantic import BaseModel

from models.database import (
    get_session, AttendancePolicy, BonusConfig as DBBonusConfig,
    GradeTarget, InsuranceRate, JobTitle,
    AllowanceType, DeductionType, BonusType
)

logger = logging.getLogger(__name__)

# 設定快取（5 分鐘 TTL，最多 16 個 key）
_cache = TTLCache(maxsize=16, ttl=300)


def _clear_cache(*keys):
    """清除指定的快取 key，不指定則全部清除"""
    if keys:
        for k in keys:
            _cache.pop(k, None)
    else:
        _cache.clear()

router = APIRouter(prefix="/api/config", tags=["config"])


# ============ Service Init ============

_salary_engine = None


def init_config_services(salary_engine):
    global _salary_engine
    _salary_engine = salary_engine


# ============ Pydantic Models ============

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


class JobTitleCreate(BaseModel):
    name: str


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


# ============ Routes ============

@router.get("/attendance-policy")
def get_attendance_policy(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    """取得考勤政策設定"""
    cached = _cache.get("attendance_policy")
    if cached is not None:
        return cached

    session = get_session()
    try:
        policy = session.query(AttendancePolicy).filter(AttendancePolicy.is_active == True).first()
        if not policy:
            return {}
        result = {
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
        _cache["attendance_policy"] = result
        return result
    finally:
        session.close()


@router.put("/attendance-policy")
def update_attendance_policy(data: AttendancePolicyUpdate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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
        _salary_engine.load_config_from_db()
        _clear_cache("attendance_policy")
        return {"message": "考勤政策更新成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/bonus")
def get_bonus_config(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    """取得獎金設定"""
    cached = _cache.get("bonus")
    if cached is not None:
        return cached

    session = get_session()
    try:
        config = session.query(DBBonusConfig).filter(DBBonusConfig.is_active == True).order_by(DBBonusConfig.config_year.desc()).first()
        if not config:
            return {}
        result = {
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
        _cache["bonus"] = result
        return result
    finally:
        session.close()


@router.put("/bonus")
def update_bonus_config(data: BonusConfigUpdate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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
        _salary_engine.load_config_from_db()
        _clear_cache("bonus")
        return {"message": "獎金設定更新成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/grade-targets")
def get_grade_targets(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
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


@router.put("/grade-targets")
def update_grade_target(data: GradeTargetUpdate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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
        _salary_engine.load_config_from_db()
        return {"message": f"{data.grade_name}目標人數更新成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/insurance-rates")
def get_insurance_rates(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    """取得勞健保費率設定"""
    cached = _cache.get("insurance_rates")
    if cached is not None:
        return cached

    session = get_session()
    try:
        rate = session.query(InsuranceRate).filter(InsuranceRate.is_active == True).order_by(InsuranceRate.rate_year.desc()).first()
        if not rate:
            return {}
        result = {
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
        _cache["insurance_rates"] = result
        return result
    finally:
        session.close()


@router.put("/insurance-rates")
def update_insurance_rates(data: InsuranceRateUpdate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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
        _salary_engine.load_config_from_db()
        _clear_cache("insurance_rates")
        return {"message": "勞健保費率更新成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/reload")
def reload_config(current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
    """重新從資料庫載入設定到薪資計算引擎"""
    try:
        _salary_engine.load_config_from_db()
        _clear_cache()
        return {"message": "設定已重新載入"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/all")
def get_all_configs(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
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


# ============ Job Titles ============

@router.get("/titles")
def get_job_titles(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    cached = _cache.get("titles")
    if cached is not None:
        return cached

    session = get_session()
    titles = session.query(JobTitle).filter(JobTitle.is_active == True).order_by(JobTitle.sort_order).all()
    result = [{"id": t.id, "name": t.name} for t in titles]
    _cache["titles"] = result
    return result


@router.post("/titles", status_code=201)
def create_job_title(title: JobTitleCreate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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
    _clear_cache("titles")
    return {"message": "Job title created", "id": new_title.id}


@router.put("/titles/{title_id}")
def update_job_title(title_id: int, title: JobTitleCreate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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
    _clear_cache("titles")
    return {"message": "Job title updated"}


@router.delete("/titles/{title_id}")
def delete_job_title(title_id: int, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
    session = get_session()
    db_title = session.query(JobTitle).filter(JobTitle.id == title_id).first()
    if not db_title:
        raise HTTPException(status_code=404, detail="Job title not found")

    # Soft delete
    db_title.is_active = False
    session.commit()
    _clear_cache("titles")
    return {"message": "Job title deleted (soft delete)"}


# ============ Type Management ============

@router.get("/allowance-types")
async def get_allowance_types(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    session = get_session()
    try:
        return session.query(AllowanceType).filter(AllowanceType.is_active == True).order_by(AllowanceType.sort_order).all()
    finally:
        session.close()


@router.post("/allowance-types", status_code=201)
async def create_allowance_type(item: AllowanceTypeCreate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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


@router.get("/deduction-types")
async def get_deduction_types(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    session = get_session()
    try:
        return session.query(DeductionType).filter(DeductionType.is_active == True).order_by(DeductionType.sort_order).all()
    finally:
        session.close()


@router.post("/deduction-types", status_code=201)
async def create_deduction_type(item: DeductionTypeCreate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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


@router.get("/bonus-types")
async def get_bonus_types(current_user: dict = Depends(require_permission(Permission.SETTINGS_READ))):
    session = get_session()
    try:
        return session.query(BonusType).filter(BonusType.is_active == True).order_by(BonusType.sort_order).all()
    finally:
        session.close()


@router.post("/bonus-types", status_code=201)
async def create_bonus_type(item: BonusTypeCreate, current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE))):
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
