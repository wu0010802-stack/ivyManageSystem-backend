"""
System configuration router
"""

import logging
from typing import Optional

from utils.cache_layer import get_cache
from fastapi import APIRouter, Depends, HTTPException, Request
from utils.errors import raise_safe_500
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.constants import MIN_CONFIG_YEAR, MAX_CONFIG_YEAR
from utils.finance_guards import (
    MIN_FINANCE_REASON_LENGTH,
    require_adjustment_reason,
    require_finance_approve,
    require_not_self_salary_record,
)
from pydantic import BaseModel, Field

# 標準底薪上限（NT$）— 與 SalaryManualAdjustRequest 的 _MANUAL_ADJUST_FIELD_MAX 對齊。
# Why: 防止先把標準設成天文數字，再透過 sync_position_salary 繞過 manual-adjust
# 的 le=500_000 與 require_finance_approve 簽核閾值。詳見 P1-2 安全評審。
_POSITION_SALARY_MAX = 500_000.0

from sqlalchemy import or_

from models.database import (
    get_session,
    AttendancePolicy,
    BonusConfig as DBBonusConfig,
    GradeTarget,
    InsuranceRate,
    JobTitle,
    DeductionType,
    BonusType,
    PositionSalaryConfig,
    LineConfig,
    SalaryRecord,
)

# _BONUS_FIELDS 已搬到 .bonus 子模組。

_ATTENDANCE_FIELDS = [
    "default_work_start",
    "default_work_end",
    "late_deduction",
    "early_leave_deduction",
    "missing_punch_deduction",
    "festival_bonus_months",
    "effective_date",
]

_INSURANCE_FIELDS = [
    "rate_year",
    "labor_rate",
    "labor_employee_ratio",
    "labor_employer_ratio",
    "labor_government_ratio",
    "health_rate",
    "health_employee_ratio",
    "health_employer_ratio",
    "pension_employer_rate",
    "average_dependents",
]

logger = logging.getLogger(__name__)

# scope: global — 全系統共用設定，無 per-user 隔離需求
_CACHE_TTL_CONFIG = 300  # 5 分鐘
_CACHE_KEY_TO_NAMESPACE = {
    "titles": "config_titles",
    "attendance_policy": "config_attendance_policy",
    "insurance_rates": "config_insurance_rates",
    "deduction_types": "config_deduction_types",
    "bonus_types": "config_bonus_types",
    "bonus": "config_bonus",
}


def _clear_cache(*keys: str) -> None:
    """清除指定 namespace，不指定則全部 config namespace 都清。"""
    namespaces = (
        [_CACHE_KEY_TO_NAMESPACE[k] for k in keys]
        if keys
        else list(_CACHE_KEY_TO_NAMESPACE.values())
    )
    for ns in namespaces:
        get_cache().clear_namespace(ns)


def _trigger_engine_grade_reload() -> None:
    """job_titles.bonus_grade 異動後讓 engine 重新載入 grade_map（即時生效）。

    走 load_config_from_db 順帶把所有 config 都重新讀，足以涵蓋本次需求；
    與 BonusConfig PATCH 流程一致，避免要等下次 server 重啟才生效。
    """
    if _salary_engine is not None:
        try:
            _salary_engine.load_config_from_db()
        except Exception:
            logger.warning("job_titles 異動後 reload engine 失敗", exc_info=True)
    _clear_cache("titles")


router = APIRouter(prefix="/api/config", tags=["config"])


# ============ Service Init ============

_salary_engine = None
_line_service = None


def init_config_services(salary_engine, line_service=None):
    global _salary_engine, _line_service
    _salary_engine = salary_engine
    _line_service = line_service


# ============ Pydantic Models ============


class AttendancePolicyUpdate(BaseModel):
    """考勤政策更新。

    Deprecated 欄位（不再進入薪資計算，已從 schema 移除）：
      - late_deduction / early_leave_deduction / missing_punch_deduction
        實際扣款固定以勞基法基準（每分鐘 = 月薪 / 30 / 8 / 60）計算，
        詳見 services/salary/deduction.py。DB 欄位保留以支援既有資料相容性。
    """

    default_work_start: Optional[str] = None
    default_work_end: Optional[str] = None
    # le=24:節慶獎金資格門檻單位為月,實務常見 3~6 個月;設上限避免極端值
    # (例如 999 讓全員失格、或被誤設成 0 讓新進員工立即合格)
    festival_bonus_months: Optional[int] = Field(None, ge=0, le=24)


# BonusConfigUpdate 已搬到 .bonus 子模組。


class GradeTargetUpdate(BaseModel):
    """年級目標人數更新"""

    grade_name: str
    festival_two_teachers: Optional[int] = Field(None, ge=0)
    festival_one_teacher: Optional[int] = Field(None, ge=0)
    festival_shared: Optional[int] = Field(None, ge=0)
    overtime_two_teachers: Optional[int] = Field(None, ge=0)
    overtime_one_teacher: Optional[int] = Field(None, ge=0)
    overtime_shared: Optional[int] = Field(None, ge=0)


class InsuranceRateUpdate(BaseModel):
    """勞健保費率更新"""

    rate_year: Optional[int] = Field(None, ge=MIN_CONFIG_YEAR, le=MAX_CONFIG_YEAR)
    labor_rate: Optional[float] = Field(None, ge=0, le=1)
    labor_employee_ratio: Optional[float] = Field(None, ge=0, le=1)
    labor_employer_ratio: Optional[float] = Field(None, ge=0, le=1)
    health_rate: Optional[float] = Field(None, ge=0, le=1)
    health_employee_ratio: Optional[float] = Field(None, ge=0, le=1)
    health_employer_ratio: Optional[float] = Field(None, ge=0, le=1)
    pension_employer_rate: Optional[float] = Field(None, ge=0, le=1)
    average_dependents: Optional[float] = Field(None, ge=0)


class JobTitleCreate(BaseModel):
    name: str
    # 階段 2-D（2026-05-07）：節慶獎金等級對應從 hardcode 搬到 DB
    bonus_grade: Optional[str] = Field(None, pattern="^[ABC]$")


class DeductionTypeCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    category: str = "other"
    is_employer_paid: bool = False
    sort_order: int = 0


class BonusTypeCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_separate_transfer: bool = False
    sort_order: int = 0


# PositionSalaryUpdate / PositionSalarySyncRequest 已搬到 .position_salary 子模組。
# LineConfigRead / LineConfigUpdate 已搬到 .line 子模組（檔尾 include_router 接回）。


# ============ Routes ============


@router.get("/attendance-policy")
def get_attendance_policy(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得考勤政策設定"""
    cached = get_cache().get("config_attendance_policy", "v")
    if cached is not None:
        return cached

    session = get_session()
    try:
        policy = (
            session.query(AttendancePolicy)
            .filter(AttendancePolicy.is_active == True)
            .order_by(AttendancePolicy.id.desc())
            .first()
        )
        if not policy:
            return {}
        result = {
            "id": policy.id,
            "default_work_start": policy.default_work_start,
            "default_work_end": policy.default_work_end,
            "late_deduction": policy.late_deduction,
            "early_leave_deduction": policy.early_leave_deduction,
            "missing_punch_deduction": policy.missing_punch_deduction,
            "festival_bonus_months": policy.festival_bonus_months,
        }
        get_cache().set("config_attendance_policy", "v", result, ttl=_CACHE_TTL_CONFIG)
        return result
    finally:
        session.close()


def _mark_existing_salary_stale_for_config(
    session,
    *,
    attendance_policy_id: Optional[int] = None,
    bonus_config_id: Optional[int] = None,
) -> int:
    """設定改版後將既有「未封存且非以新版本計算」的薪資標 needs_recalc。

    Why: PUT /api/config/* 只 reload engine,卻不通知既有 SalaryRecord;
        若使用者已用舊設定算過 N 月薪資,改版後 finalize 仍會通過,等同
        以舊參數封存。標 stale 後 finalize 守衛會擋下 109 並要求重算。

    封存 (is_finalized=True) 的不動,維持結帳鎖定語意。
    """
    if attendance_policy_id is None and bonus_config_id is None:
        return 0
    q = session.query(SalaryRecord).filter(SalaryRecord.is_finalized != True)
    if attendance_policy_id is not None:
        q = q.filter(
            (SalaryRecord.attendance_policy_id != attendance_policy_id)
            | (SalaryRecord.attendance_policy_id.is_(None))
        )
    if bonus_config_id is not None:
        q = q.filter(
            (SalaryRecord.bonus_config_id != bonus_config_id)
            | (SalaryRecord.bonus_config_id.is_(None))
        )
    affected = q.update({SalaryRecord.needs_recalc: True}, synchronize_session=False)
    return affected


@router.put("/attendance-policy")
def update_attendance_policy(
    data: AttendancePolicyUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """更新考勤政策設定（建立新版本，保留舊版歷程）"""
    session = get_session()
    try:
        old_policy = (
            session.query(AttendancePolicy)
            .filter(AttendancePolicy.is_active == True)
            .order_by(AttendancePolicy.id.desc())
            .first()
        )

        # 複製舊版欄位值，再套用本次變更
        new_policy = AttendancePolicy(is_active=True)
        if old_policy:
            for field in _ATTENDANCE_FIELDS:
                setattr(new_policy, field, getattr(old_policy, field, None))
            new_policy.version = (old_policy.version or 1) + 1
        else:
            new_policy.version = 1

        new_policy.changed_by = current_user.get("username")

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(new_policy, key, value)

        if old_policy:
            old_policy.is_active = False

        session.add(new_policy)
        session.flush()  # 取得 new_policy.id
        stale_marked = _mark_existing_salary_stale_for_config(
            session, attendance_policy_id=new_policy.id
        )
        session.commit()
        if stale_marked:
            logger.warning(
                "考勤政策更新後標記 %d 筆未封存薪資為 needs_recalc(舊政策 id=%s → 新 id=%s)",
                stale_marked,
                old_policy.id if old_policy else None,
                new_policy.id,
            )
        _salary_engine.load_config_from_db()
        _clear_cache("attendance_policy")
        return {
            "message": "考勤政策更新成功",
            "version": new_policy.version,
            "id": new_policy.id,
            "salary_records_marked_stale": stale_marked,
        }
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


# /bonus 路由已搬到 .bonus 子模組。


@router.get("/grade-targets")
def get_grade_targets(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得年級目標人數設定（屬於目前有效的獎金設定版本）"""
    session = get_session()
    try:
        active_bonus = (
            session.query(DBBonusConfig)
            .filter(DBBonusConfig.is_active == True)
            .order_by(DBBonusConfig.id.desc())
            .first()
        )
        if active_bonus:
            targets = (
                session.query(GradeTarget)
                .filter(GradeTarget.bonus_config_id == active_bonus.id)
                .order_by(GradeTarget.grade_name)
                .all()
            )
            # 向下相容：若新版本尚無年級目標，回退到 NULL（舊資料）
            if not targets:
                targets = (
                    session.query(GradeTarget)
                    .filter(GradeTarget.bonus_config_id == None)  # noqa: E711
                    .order_by(GradeTarget.grade_name)
                    .all()
                )
        else:
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
                "overtime_shared": t.overtime_shared,
            }
        return result
    finally:
        session.close()


@router.put("/grade-targets")
def update_grade_target(
    data: GradeTargetUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """更新年級目標人數設定（直接更新屬於目前有效獎金設定版本的行）"""
    session = get_session()
    try:
        active_bonus = (
            session.query(DBBonusConfig)
            .filter(DBBonusConfig.is_active == True)
            .order_by(DBBonusConfig.id.desc())
            .first()
        )
        active_bonus_id = active_bonus.id if active_bonus else None

        # 優先找屬於目前版本的行
        target = (
            session.query(GradeTarget)
            .filter(
                GradeTarget.grade_name == data.grade_name,
                GradeTarget.bonus_config_id == active_bonus_id,
            )
            .first()
        )

        if not target:
            # 向下相容：找舊資料（bonus_config_id=NULL）或任何同年級行作為藍本
            template = (
                session.query(GradeTarget)
                .filter(GradeTarget.grade_name == data.grade_name)
                .first()
            )
            target = GradeTarget(
                config_year=2026,
                grade_name=data.grade_name,
                bonus_config_id=active_bonus_id,
                festival_two_teachers=template.festival_two_teachers if template else 0,
                festival_one_teacher=template.festival_one_teacher if template else 0,
                festival_shared=template.festival_shared if template else 0,
                overtime_two_teachers=template.overtime_two_teachers if template else 0,
                overtime_one_teacher=template.overtime_one_teacher if template else 0,
                overtime_shared=template.overtime_shared if template else 0,
            )
            session.add(target)

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None and key != "grade_name":
                setattr(target, key, value)

        # GradeTarget 是 BonusConfig 的子表; 此端點不升 bonus_config 版本,直接 mutate
        # 現役 row。SalaryRecord 透過 bonus_config_id 間接引用此目標,改值後既有未封存
        # 薪資若不重算,finalize 會以新目標封存舊薪資。標 needs_recalc 讓守衛擋下 109。
        # 封存的維持鎖定語意; bonus_config_id 為 NULL 的舊資料不在此端點影響範圍內。
        stale_marked = 0
        if active_bonus_id is not None:
            stale_marked = (
                session.query(SalaryRecord)
                .filter(
                    SalaryRecord.is_finalized != True,
                    SalaryRecord.bonus_config_id == active_bonus_id,
                )
                .update({SalaryRecord.needs_recalc: True}, synchronize_session=False)
            )

        session.commit()
        if stale_marked:
            logger.warning(
                "年級目標 %s 更新後標記 %d 筆未封存薪資為 needs_recalc(bonus_config_id=%s)",
                data.grade_name,
                stale_marked,
                active_bonus_id,
            )
        _salary_engine.load_config_from_db()
        return {
            "message": f"{data.grade_name}目標人數更新成功",
            "salary_records_marked_stale": stale_marked,
        }
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/insurance-rates")
def get_insurance_rates(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得勞健保費率設定"""
    cached = get_cache().get("config_insurance_rates", "v")
    if cached is not None:
        return cached

    session = get_session()
    try:
        rate = (
            session.query(InsuranceRate)
            .filter(InsuranceRate.is_active == True)
            .order_by(InsuranceRate.rate_year.desc(), InsuranceRate.id.desc())
            .first()
        )
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
            "average_dependents": rate.average_dependents,
        }
        get_cache().set("config_insurance_rates", "v", result, ttl=_CACHE_TTL_CONFIG)
        return result
    finally:
        session.close()


@router.put("/insurance-rates")
def update_insurance_rates(
    data: InsuranceRateUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """更新勞健保費率設定（建立新版本，保留舊版歷程）"""
    session = get_session()
    try:
        old_rate = (
            session.query(InsuranceRate)
            .filter(InsuranceRate.is_active == True)
            .order_by(InsuranceRate.id.desc())
            .first()
        )

        new_rate = InsuranceRate(is_active=True)
        if old_rate:
            for field in _INSURANCE_FIELDS:
                setattr(new_rate, field, getattr(old_rate, field, None))
            new_rate.version = (old_rate.version or 1) + 1
        else:
            new_rate.version = 1
            new_rate.rate_year = 2026

        new_rate.changed_by = current_user.get("username")

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(new_rate, key, value)

        if old_rate:
            old_rate.is_active = False

        session.add(new_rate)
        # 保險費率改版會影響 labor / health / pension 各扣款 → 已算未封存
        # 薪資若不重算，finalize 會以舊費率封存。標 needs_recalc 讓守衛擋下。
        # 封存 (is_finalized=True) 的不動，維持結帳鎖定語意。
        stale_marked = (
            session.query(SalaryRecord)
            .filter(SalaryRecord.is_finalized != True)
            .update({SalaryRecord.needs_recalc: True}, synchronize_session=False)
        )
        session.commit()
        if stale_marked:
            logger.warning(
                "勞健保費率更新後標記 %d 筆未封存薪資為 needs_recalc(新版本 id=%s)",
                stale_marked,
                new_rate.id,
            )
        _salary_engine.load_config_from_db()
        _clear_cache("insurance_rates")
        return {
            "message": "勞健保費率更新成功",
            "version": new_rate.version,
            "id": new_rate.id,
            "salary_records_marked_stale": stale_marked,
        }
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


# /bonus/history 已搬到 .bonus 子模組。


@router.get("/attendance-policy/history")
def get_attendance_policy_history(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得考勤政策所有歷史版本（最新在前）"""
    session = get_session()
    try:
        policies = (
            session.query(AttendancePolicy)
            .order_by(AttendancePolicy.created_at.desc())
            .all()
        )
        return [
            {
                "id": p.id,
                "version": p.version,
                "is_active": p.is_active,
                "changed_by": p.changed_by,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "default_work_start": p.default_work_start,
                "default_work_end": p.default_work_end,
                "festival_bonus_months": p.festival_bonus_months,
            }
            for p in policies
        ]
    finally:
        session.close()


@router.get("/insurance-rates/history")
def get_insurance_rates_history(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得勞健保費率所有歷史版本（最新在前）"""
    session = get_session()
    try:
        rates = (
            session.query(InsuranceRate).order_by(InsuranceRate.created_at.desc()).all()
        )
        return [
            {
                "id": r.id,
                "version": r.version,
                "rate_year": r.rate_year,
                "is_active": r.is_active,
                "changed_by": r.changed_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "labor_rate": r.labor_rate,
                "health_rate": r.health_rate,
                "pension_employer_rate": r.pension_employer_rate,
            }
            for r in rates
        ]
    finally:
        session.close()


@router.post("/reload")
def reload_config(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """重新從資料庫載入設定到薪資計算引擎"""
    try:
        _salary_engine.load_config_from_db()
        _clear_cache()
        return {"message": "設定已重新載入"}
    except Exception as e:
        raise_safe_500(e)


@router.get("/all")
def get_all_configs(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得所有設定（一次性載入）"""
    session = get_session()
    try:
        # 考勤政策
        policy = (
            session.query(AttendancePolicy)
            .filter(AttendancePolicy.is_active == True)
            .order_by(AttendancePolicy.id.desc())
            .first()
        )
        attendance_policy = None
        if policy:
            attendance_policy = {
                "default_work_start": policy.default_work_start,
                "default_work_end": policy.default_work_end,
                "late_deduction": policy.late_deduction,
                "early_leave_deduction": policy.early_leave_deduction,
                "missing_punch_deduction": policy.missing_punch_deduction,
                "festival_bonus_months": policy.festival_bonus_months,
            }

        # 獎金設定
        bonus = (
            session.query(DBBonusConfig)
            .filter(DBBonusConfig.is_active == True)
            .order_by(DBBonusConfig.id.desc())
            .first()
        )
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
                "school_wide_target": bonus.school_wide_target,
            }

        # 年級目標（合併邏輯：NULL 為基礎，active bonus 版本優先覆蓋）
        grade_targets = {}
        gt_conds = [GradeTarget.bonus_config_id == None]  # noqa: E711
        if bonus:
            gt_conds.append(GradeTarget.bonus_config_id == bonus.id)
        all_tgts = session.query(GradeTarget).filter(or_(*gt_conds)).all()
        null_tgts = {t.grade_name: t for t in all_tgts if t.bonus_config_id is None}
        ver_tgts = {t.grade_name: t for t in all_tgts if t.bonus_config_id is not None}
        for grade_name, t in {**null_tgts, **ver_tgts}.items():
            grade_targets[grade_name] = {
                "festival_two_teachers": t.festival_two_teachers,
                "festival_one_teacher": t.festival_one_teacher,
                "festival_shared": t.festival_shared,
                "overtime_two_teachers": t.overtime_two_teachers,
                "overtime_one_teacher": t.overtime_one_teacher,
                "overtime_shared": t.overtime_shared,
            }

        # 勞健保費率
        rate = (
            session.query(InsuranceRate)
            .filter(InsuranceRate.is_active == True)
            .order_by(InsuranceRate.id.desc())
            .first()
        )
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
                "average_dependents": rate.average_dependents,
            }

        return {
            "attendance_policy": attendance_policy,
            "bonus_config": bonus_config,
            "grade_targets": grade_targets,
            "insurance_rates": insurance_rates,
        }
    finally:
        session.close()


# ============ Job Titles ============


@router.get("/titles")
def get_job_titles(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    cached = get_cache().get("config_titles", "v")
    if cached is not None:
        return cached

    session = get_session()
    try:
        titles = (
            session.query(JobTitle)
            .filter(JobTitle.is_active == True)
            .order_by(JobTitle.sort_order)
            .all()
        )
        result = [
            {"id": t.id, "name": t.name, "bonus_grade": t.bonus_grade} for t in titles
        ]
        get_cache().set("config_titles", "v", result, ttl=_CACHE_TTL_CONFIG)
        return result
    finally:
        session.close()


@router.post("/titles", status_code=201)
def create_job_title(
    title: JobTitleCreate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    session = get_session()
    try:
        existing = session.query(JobTitle).filter(JobTitle.name == title.name).first()
        if existing:
            if not existing.is_active:
                existing.is_active = True
                if title.bonus_grade is not None:
                    existing.bonus_grade = title.bonus_grade
                session.commit()
                _trigger_engine_grade_reload()
                return {"message": "Job title reactivated", "id": existing.id}
            raise HTTPException(status_code=400, detail="Job title already exists")

        new_title = JobTitle(
            name=title.name, is_active=True, bonus_grade=title.bonus_grade
        )
        session.add(new_title)
        session.commit()
        _clear_cache("titles")
        _trigger_engine_grade_reload()
        return {"message": "Job title created", "id": new_title.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()


@router.put("/titles/{title_id}")
def update_job_title(
    title_id: int,
    title: JobTitleCreate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    session = get_session()
    try:
        # Check if name exists for OTHER titles
        existing = (
            session.query(JobTitle)
            .filter(JobTitle.name == title.name, JobTitle.id != title_id)
            .first()
        )
        if existing:
            raise HTTPException(status_code=400, detail="Job title name already exists")

        db_title = session.query(JobTitle).filter(JobTitle.id == title_id).first()
        if not db_title:
            raise HTTPException(status_code=404, detail="Job title not found")

        old_name = db_title.name
        old_grade = db_title.bonus_grade

        db_title.name = title.name
        # Ensure it's active if we are updating it
        db_title.is_active = True
        # bonus_grade=None 視為「不變更」，要清空請另寫專屬 endpoint。
        # Why: 整個 PUT 都用 JobTitleCreate model，前端常見只想改 name 不送 bonus_grade，
        # 若把 None 當「設為 NULL」會造成意外覆蓋。
        if title.bonus_grade is not None:
            db_title.bonus_grade = title.bonus_grade

        # bonus_grade 真的變動 → 標記持該職稱員工未封存薪資 needs_recalc，否則 finalize
        # 會以舊節慶獎金等級封存。引擎 grade_map = {JobTitle.name: bonus_grade}，員工以
        # Employee.title 字串對應（engine._load_grade_map_from_db）。對稱依據：員工側
        # bonus_grade 在 _SALARY_INPUT_FIELDS（api/employees.py）改動會標 stale。
        grade_changed = title.bonus_grade is not None and title.bonus_grade != old_grade
        stale_marked = 0
        if grade_changed:
            from models.database import Employee

            affected_titles = {old_name, title.name}
            stale_marked = (
                session.query(SalaryRecord)
                .filter(
                    SalaryRecord.is_finalized != True,
                    SalaryRecord.employee_id.in_(
                        session.query(Employee.id).filter(
                            Employee.is_active == True,
                            Employee.title.in_(affected_titles),
                        )
                    ),
                )
                .update({SalaryRecord.needs_recalc: True}, synchronize_session=False)
            )

        session.commit()
        _clear_cache("titles")
        _trigger_engine_grade_reload()
        return {
            "message": "Job title updated",
            "salary_records_marked_stale": stale_marked,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()


@router.delete("/titles/{title_id}")
def delete_job_title(
    title_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    session = get_session()
    try:
        db_title = session.query(JobTitle).filter(JobTitle.id == title_id).first()
        if not db_title:
            raise HTTPException(status_code=404, detail="Job title not found")

        # Soft delete
        db_title.is_active = False
        session.commit()
        _clear_cache("titles")
        return {"message": "Job title deleted (soft delete)"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()


# ============ Type Management ============


@router.get("/deduction-types")
def get_deduction_types(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    cached = get_cache().get("config_deduction_types", "v")
    if cached is not None:
        return cached
    session = get_session()
    try:
        items = (
            session.query(DeductionType)
            .filter(DeductionType.is_active == True)
            .order_by(DeductionType.sort_order)
            .all()
        )
        result = [
            {
                "id": i.id,
                "code": i.code,
                "name": i.name,
                "category": i.category,
                "is_employer_paid": i.is_employer_paid,
                "sort_order": i.sort_order,
            }
            for i in items
        ]
        get_cache().set("config_deduction_types", "v", result, ttl=_CACHE_TTL_CONFIG)
        return result
    finally:
        session.close()


@router.post("/deduction-types", status_code=201)
def create_deduction_type(
    item: DeductionTypeCreate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    session = get_session()
    try:
        new_item = DeductionType(**item.model_dump())
        session.add(new_item)
        session.commit()
        _clear_cache("deduction_types")
        return {"message": "新增成功", "id": new_item.id}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/bonus-types")
def get_bonus_types(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    cached = get_cache().get("config_bonus_types", "v")
    if cached is not None:
        return cached
    session = get_session()
    try:
        items = (
            session.query(BonusType)
            .filter(BonusType.is_active == True)
            .order_by(BonusType.sort_order)
            .all()
        )
        result = [
            {
                "id": i.id,
                "code": i.code,
                "name": i.name,
                "is_separate_transfer": i.is_separate_transfer,
                "sort_order": i.sort_order,
            }
            for i in items
        ]
        get_cache().set("config_bonus_types", "v", result, ttl=_CACHE_TTL_CONFIG)
        return result
    finally:
        session.close()


@router.post("/bonus-types", status_code=201)
def create_bonus_type(
    item: BonusTypeCreate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    session = get_session()
    try:
        new_item = BonusType(**item.model_dump())
        session.add(new_item)
        session.commit()
        _clear_cache("bonus_types")
        return {"message": "新增成功", "id": new_item.id}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


# 職位標準底薪設定 / 比對 / 同步路由已搬到 .position_salary。
# LINE 通知設定路由已搬到 .line。

# ============ Sub-router 整併 ============
from .bonus import router as _bonus_router  # noqa: E402
from .line import router as _line_router  # noqa: E402
from .position_salary import router as _position_salary_router  # noqa: E402

router.include_router(_bonus_router)
router.include_router(_line_router)
router.include_router(_position_salary_router)
