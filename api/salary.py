"""
Salary calculation and management router
"""

import io
import logging
import threading
from datetime import date, datetime, time
from typing import Optional

from cachetools import TTLCache

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from utils.errors import raise_safe_500
from services.report_cache_service import report_cache_service
from utils.auth import require_permission, require_staff_permission
from utils.error_messages import SALARY_RECORD_NOT_FOUND
from utils.finance_guards import (
    has_finance_approve,
    require_finance_approve,
    require_not_self_salary_record,
)
from utils.permissions import Permission
from utils.rate_limit import SlidingWindowLimiter


def _invalidate_finance_summary_cache() -> None:
    """薪資寫入後失效 /finance-summary 快取（TTL 30 分）。失敗不影響交易。"""
    try:
        report_cache_service.invalidate_category(None, "reports_finance_summary")
    except Exception:
        logging.getLogger(__name__).warning(
            "invalidate finance_summary cache failed", exc_info=True
        )


# 薪資計算為高 CPU 操作，每 IP 每小時最多 20 次（批次計算）
_salary_calc_limiter = SlidingWindowLimiter(
    max_calls=20,
    window_seconds=3600,
    name="salary_calculate",
    error_detail="薪資計算操作過於頻繁，請稍後再試",
)

# 批次計算硬上限，避免單次 HTTP 呼叫計算過多員工導致超時 / 記憶體異常。
# 一般園所員工規模 < 100，此值已有充足緩衝；若需計算更多，改用 async job。
MAX_BULK_EMPLOYEES_SYNC = 300
from fastapi import BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from sqlalchemy import and_, or_
from sqlalchemy.orm import joinedload
import calendar as _cal

from models.base import session_scope
from models.database import (
    get_session,
    Employee,
    Classroom,
    SalaryRecord,
    SalarySnapshot,
    Attendance,
)
from services.salary_engine import _compute_hourly_daily_hours
from services.salary.utils import calc_daily_salary
from services.salary.engine import SalaryEngine as RuntimeSalaryEngine
from services.salary.totals import recompute_record_totals
from services.salary_field_breakdown import (
    FIELD_LABELS,
    build_field_breakdown,
    build_salary_debug_snapshot,
)
from services.salary_job_registry import registry as _salary_job_registry
from services import salary_snapshot_service as _snapshot_svc
from services.student_enrollment import (
    classroom_student_count_map,
    count_students_active_on,
)
from api.salary_fields import calculate_display_bonus_total

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["salary"])


def _active_employees_in_month_filter(year: int, month: int):
    """產生 SQLAlchemy filter：在指定月份任何一天實際在職的員工。

    Why: 舊版各處獨立寫「目前 is_active 或當月離職」，導致補算歷史月份時：
    已於後月離職的員工（現在 is_active=False）被排除；未來才到職但目前
    is_active=True 的員工被誤納入。async worker 已改為以「hire_date ≤ 月底
    AND (resign_date IS NULL OR ≥ 月初)」判定；sync、async pre-scan、
    festival-bonus 三處也應用同一條件，避免 finalize 時路徑結果分叉。
    """
    _, _last = _cal.monthrange(year, month)
    _start = date(year, month, 1)
    _end = date(year, month, _last)
    return and_(
        or_(Employee.hire_date.is_(None), Employee.hire_date <= _end),
        or_(Employee.resign_date.is_(None), Employee.resign_date >= _start),
    )


# ============ Service Init ============

_salary_engine = None
_insurance_service = None
_line_service = None


def init_salary_services(salary_engine, insurance_service, line_service=None):
    global _salary_engine, _insurance_service, _line_service
    _salary_engine = salary_engine
    _insurance_service = insurance_service
    if line_service is not None:
        _line_service = line_service


# ============ Pydantic Models ============


class SalarySimulateOverride(BaseModel):
    """薪資試算覆蓋參數（None = 使用 DB 實際資料）"""

    late_count: Optional[int] = Field(None, ge=0)
    early_leave_count: Optional[int] = Field(None, ge=0)
    missing_punch_count: Optional[int] = Field(None, ge=0)
    total_late_minutes: Optional[float] = Field(None, ge=0)
    total_early_minutes: Optional[float] = Field(None, ge=0)
    work_days: Optional[int] = Field(None, ge=0, le=31)
    extra_personal_leave_hours: float = Field(0, ge=0)
    extra_sick_leave_hours: float = Field(0, ge=0)
    enrollment_override: Optional[int] = Field(None, ge=0)
    extra_overtime_pay: float = Field(0, ge=0)


class SalarySimulateRequest(BaseModel):
    employee_id: int = Field(..., ge=1)
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    overrides: SalarySimulateOverride = SalarySimulateOverride()


# ============ Lazy snapshot trigger ============

# 記憶體去重：同一 day + (year, month) 每個 worker 只觸發一次背景補拍，
# DB 仍會 idempotent 保護；此處只是避免重複排隊 background task 浪費。
_snapshot_lazy_guard: set[str] = set()
_snapshot_lazy_lock = threading.Lock()


def _previous_month(today: date) -> tuple[int, int]:
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def _trigger_past_month_snapshot_if_missing(bg: Optional[BackgroundTasks]) -> None:
    """若上個月有 SalaryRecord 但缺任何一筆 month_end 快照，排背景補拍。

    呼叫端：`/salaries/calculate` 與 `/salaries/records`。
    不阻塞主請求，錯誤僅 log。
    """
    if bg is None:
        return
    today = date.today()
    year, month = _previous_month(today)
    key = f"{today.isoformat()}:{year}-{month:02d}"
    with _snapshot_lazy_lock:
        if key in _snapshot_lazy_guard:
            return
        _snapshot_lazy_guard.add(key)
    try:
        from services.salary_snapshot_service import (
            run_month_end_snapshots_job,
        )
        from models.database import SalarySnapshot as _SnapModel

        with session_scope() as session:
            has_record = (
                session.query(SalaryRecord.id)
                .filter(
                    SalaryRecord.salary_year == year,
                    SalaryRecord.salary_month == month,
                )
                .first()
                is not None
            )
            if not has_record:
                return
            record_count = (
                session.query(SalaryRecord.employee_id)
                .filter(
                    SalaryRecord.salary_year == year,
                    SalaryRecord.salary_month == month,
                )
                .count()
            )
            snapshot_count = (
                session.query(_SnapModel.id)
                .filter(
                    _SnapModel.salary_year == year,
                    _SnapModel.salary_month == month,
                    _SnapModel.snapshot_type == "month_end",
                )
                .count()
            )
            if snapshot_count >= record_count:
                return
        bg.add_task(run_month_end_snapshots_job, year, month, "system")
        logger.info(
            "lazy snapshot trigger queued for %d/%d (records=%d, existing=%d)",
            year,
            month,
            record_count,
            snapshot_count,
        )
    except Exception as e:
        logger.warning("lazy snapshot trigger skipped: %s", e)


# ============ Access control helpers ============
#
# 由 utils/salary_access.py 統一維護（IDOR audit Phase 2 抽出）；本檔保留同名 alias，
# 避免散落於本檔的多處呼叫需要重新命名。
from utils.salary_access import (  # noqa: E402
    FULL_SALARY_ROLES,
    resolve_salary_viewer_employee_id as _resolve_salary_viewer_employee_id,
    enforce_self_or_full_salary as _enforce_self_or_full_salary,
)

# ============ Routes ============


@router.post("/salaries/calculate")
def calculate_salaries_alt(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
    year: int = Query(..., ge=2000, le=2100, description="Calculate for which year"),
    month: int = Query(..., ge=1, le=12, description="Calculate for which month"),
    _rl: None = Depends(_salary_calc_limiter.as_dependency()),
):
    """
    Calculate or Recalculate salaries for all employees for a given month.
    """
    _trigger_past_month_snapshot_if_missing(background_tasks)
    session = get_session()
    try:
        # ── 封存前置檢查：只要該月有任何已封存薪資，整批拒絕 ──────────────────
        # 理由：部分封存 + 部分重算 會讓帳冊出現新舊混合的狀態，更難稽核。
        # 應讓管理員先到薪資頁面確認並解除整月封存後，再執行重算。
        finalized_records = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .filter(
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
                SalaryRecord.is_finalized == True,
            )
            .all()
        )
        if finalized_records:
            names = "、".join(r.name for _, r in finalized_records)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{year} 年 {month} 月已有 {len(finalized_records)} 筆薪資封存，"
                    f"無法整批重算（{names}）。"
                    "請先至薪資管理頁面解除整月封存後再重試。"
                ),
            )
        # ─────────────────────────────────────────────────────────────────────────

        from services.salary.engine import SalaryEngine as Engine

        engine = Engine(load_from_db=True)

        # 1. 當月實際在職的員工（補算歷史月份時也正確）
        employees = (
            session.query(Employee)
            .filter(_active_employees_in_month_filter(year, month))
            .all()
        )
        employee_ids = [emp.id for emp in employees]
        emp_name_map = {emp.id: emp.name for emp in employees}

        if len(employee_ids) > MAX_BULK_EMPLOYEES_SYNC:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"待計算員工數 {len(employee_ids)} 超過同步上限 "
                    f"{MAX_BULK_EMPLOYEES_SYNC}，請改用 /api/salaries/calculate-async"
                ),
            )

    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e)
    finally:
        session.close()

    # 2. 批次計算（process_bulk_salary_calculation 自行管理 session）
    try:
        bulk_results, errors = engine.process_bulk_salary_calculation(
            employee_ids, year, month
        )
    except Exception as e:
        raise_safe_500(e)

    results = []
    for emp, breakdown in bulk_results:
        results.append(
            {
                "employee_id": emp.id,
                "employee_name": emp.name,
                "base_salary": breakdown.base_salary,
                "festival_bonus": breakdown.festival_bonus,
                "overtime_bonus": breakdown.overtime_bonus,
                "overtime_pay": breakdown.overtime_work_pay,
                "supervisor_dividend": breakdown.supervisor_dividend,
                "labor_insurance": breakdown.labor_insurance,
                "health_insurance": breakdown.health_insurance,
                "late_deduction": breakdown.late_deduction,
                "early_leave_deduction": breakdown.early_leave_deduction,
                "missing_punch_deduction": breakdown.missing_punch_deduction,
                "leave_deduction": breakdown.leave_deduction,
                "absence_deduction": breakdown.absence_deduction or 0,
                "attendance_deduction": (
                    (breakdown.late_deduction or 0)
                    + (breakdown.early_leave_deduction or 0)
                    + (breakdown.missing_punch_deduction or 0)
                ),
                "meeting_overtime_pay": breakdown.meeting_overtime_pay or 0,
                "meeting_absence_deduction": breakdown.meeting_absence_deduction or 0,
                "birthday_bonus": breakdown.birthday_bonus or 0,
                "pension_self": breakdown.pension_self or 0,
                "total_deduction": breakdown.total_deduction,
                "total_deductions": breakdown.total_deduction,
                "net_salary": breakdown.net_salary,
                "net_pay": breakdown.net_salary,
            }
        )

    # LINE 群組推播（薪資批次計算完成）
    if _line_service is not None and results:
        try:
            total_net = sum(r["net_pay"] for r in results)
            _line_service.notify_salary_batch_complete(
                year, month, len(results), int(total_net)
            )
        except Exception as _le:
            logger.warning("薪資批次計算 LINE 推播失敗: %s", _le)

    _invalidate_finance_summary_cache()
    return {"results": results, "errors": errors}


# ============ 批次計算 async job ============


def _breakdown_to_result_dict(emp, breakdown) -> dict:
    return {
        "employee_id": emp.id,
        "employee_name": emp.name,
        "base_salary": breakdown.base_salary,
        "festival_bonus": breakdown.festival_bonus,
        "overtime_bonus": breakdown.overtime_bonus,
        "overtime_pay": breakdown.overtime_work_pay,
        "supervisor_dividend": breakdown.supervisor_dividend,
        "labor_insurance": breakdown.labor_insurance,
        "health_insurance": breakdown.health_insurance,
        "late_deduction": breakdown.late_deduction,
        "early_leave_deduction": breakdown.early_leave_deduction,
        "missing_punch_deduction": breakdown.missing_punch_deduction,
        "leave_deduction": breakdown.leave_deduction,
        "absence_deduction": breakdown.absence_deduction or 0,
        "meeting_overtime_pay": breakdown.meeting_overtime_pay or 0,
        "meeting_absence_deduction": breakdown.meeting_absence_deduction or 0,
        "birthday_bonus": breakdown.birthday_bonus or 0,
        "pension_self": breakdown.pension_self or 0,
        "total_deduction": breakdown.total_deduction,
        "total_deductions": breakdown.total_deduction,
        "net_salary": breakdown.net_salary,
        "net_pay": breakdown.net_salary,
    }


def _run_salary_calc_job(job_id: str, year: int, month: int) -> None:
    """背景執行薪資批次計算，同步更新 job registry 狀態。"""
    _salary_job_registry.mark_running(job_id)
    try:
        with session_scope() as session:
            finalized = (
                session.query(SalaryRecord.id)
                .filter(
                    SalaryRecord.salary_year == year,
                    SalaryRecord.salary_month == month,
                    SalaryRecord.is_finalized == True,
                )
                .count()
            )
            if finalized:
                _salary_job_registry.fail(
                    job_id,
                    f"{year}/{month} 已有 {finalized} 筆薪資封存，無法整批重算",
                )
                return

            employees = (
                session.query(Employee)
                .filter(_active_employees_in_month_filter(year, month))
                .all()
            )
            employee_ids = [e.id for e in employees]

        engine = RuntimeSalaryEngine(load_from_db=True)

        def _progress(done: int, total: int, current: str) -> None:
            _salary_job_registry.update_progress(job_id, done, total, current)

        bulk_results, errors = engine.process_bulk_salary_calculation(
            employee_ids, year, month, progress_callback=_progress
        )

        results_dicts = [_breakdown_to_result_dict(e, b) for e, b in bulk_results]
        _salary_job_registry.complete(job_id, results_dicts, errors)
        _invalidate_finance_summary_cache()

        if _line_service is not None and results_dicts:
            try:
                total_net = sum(r["net_pay"] for r in results_dicts)
                _line_service.notify_salary_batch_complete(
                    year, month, len(results_dicts), int(total_net)
                )
            except Exception as _le:
                logger.warning("薪資批次計算 LINE 推播失敗: %s", _le)
    except Exception as e:
        logger.exception("async 薪資批次計算失敗 job_id=%s", job_id)
        _salary_job_registry.fail(job_id, str(e))


@router.post("/salaries/calculate-async", status_code=202)
def calculate_salaries_async_start(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    _rl: None = Depends(_salary_calc_limiter.as_dependency()),
):
    """建立 async 批次計算 job，立即回傳 job_id；實際計算於背景執行。

    前端可透過 GET /api/salaries/calculate-jobs/{job_id} 輪詢進度。
    """
    # 同 year/month 已有進行中的 job → 拒絕重複觸發（本 worker 內 guard）
    active = _salary_job_registry.find_active(year, month)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{year} 年 {month} 月已有計算中的 job（id={active.job_id}，"
                f"status={active.status}，進度 {active.done}/{active.total}），"
                "請等待目前工作完成或待其結束後再觸發"
            ),
        )

    with session_scope() as session:
        finalized = (
            session.query(SalaryRecord.id)
            .filter(
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
                SalaryRecord.is_finalized == True,
            )
            .count()
        )
        if finalized:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{year} 年 {month} 月已有 {finalized} 筆薪資封存，"
                    "請先解除整月封存再重試"
                ),
            )

        total = (
            session.query(Employee.id)
            .filter(_active_employees_in_month_filter(year, month))
            .count()
        )

    job = _salary_job_registry.create(year=year, month=month, total=total)
    background_tasks.add_task(_run_salary_calc_job, job.job_id, year, month)
    return {"job_id": job.job_id, "status": job.status, "total": job.total}


@router.get("/salaries/calculate-jobs/{job_id}")
def get_salary_calc_job(
    job_id: str,
    include_results: bool = Query(False, description="包含 results 列表"),
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """查詢 async 批次計算 job 狀態與進度。"""
    job = _salary_job_registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job 不存在或已過期")
    payload = job.to_dict()
    if include_results and job.status == "completed":
        payload["results"] = job.results
    return payload


@router.get("/salaries/logic")
def get_salary_logic(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """傾印目前的薪資計算邏輯與所有參數設定（DB 設定 + Engine runtime + 公式）。

    Why: 給薪資頁的「薪資邏輯」分頁顯示。資料純為設定/常數類，不含任何個人薪資；
    SALARY_READ 即可讀。原本只在 dev router 提供 (`/api/dev/salary-logic`)，
    導致 staging / production / 未設 ENV 一律 404。
    """
    from services.salary_logic_info import build_salary_logic_info

    if not _salary_engine:
        raise HTTPException(status_code=503, detail="薪資引擎尚未初始化")
    session = get_session()
    try:
        return build_salary_logic_info(session, _salary_engine)
    finally:
        session.close()


@router.get("/salaries/employee-salary-debug")
def get_employee_salary_debug(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    employee_id: int = Query(..., ge=1),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    """模擬計算單一員工指定月份的薪資並回傳完整明細（不寫入 DB）。

    Why: 「薪資試算」分頁右側需要顯示 DB 實際資料的逐項拆解（出勤、扣款、節慶獎金、
    勞健保、會議等）。此端點只讀 DB 不套覆蓋。原本只在 dev router 提供且權限為
    SETTINGS_READ，正式環境 404。改用 SALARY_READ + self-or-full 守衛，與其他薪資
    讀取端點一致；非全員視野的角色只能查自己。
    """
    if not _salary_engine:
        raise HTTPException(status_code=503, detail="薪資引擎尚未初始化")

    _enforce_self_or_full_salary(current_user, employee_id)

    session = get_session()
    try:
        emp = session.query(Employee).get(employee_id)
        if not emp:
            raise HTTPException(status_code=404, detail=f"員工 id={employee_id} 不存在")
        if emp.employee_type == "hourly":
            raise HTTPException(
                status_code=422,
                detail="時薪制員工請使用正式薪資計算流程，debug 端點僅支援月薪正職員工",
            )
        return build_salary_debug_snapshot(session, _salary_engine, emp, year, month)
    finally:
        session.close()


@router.get("/salaries/festival-bonus")
def get_festival_bonus(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    """
    Return breakdown of festival bonus calculation
    """
    # F-013：跨員工彙總端點，僅 admin/hr 可使用；其他持 SALARY_READ 的角色一律 403。
    # 對齊 _enforce_self_or_full_salary 的精神：非全員視野者不可看到全體節慶獎金明細。
    if _resolve_salary_viewer_employee_id(current_user) is not None:
        raise HTTPException(status_code=403, detail="僅可查詢本人薪資")
    with session_scope() as session:
        # 使用啟動時已載入設定的 singleton，避免每次請求重跑 4 次 DB 查詢
        engine = (
            _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=True)
        )

        _, _fb_last = _cal.monthrange(year, month)
        month_end = date(year, month, _fb_last)

        # 批次預先查詢共用資料，避免 N+1
        school_active = count_students_active_on(session, month_end)
        cls_count_map = classroom_student_count_map(session, month_end)
        classroom_map = {
            c.id: c
            for c in session.query(Classroom).options(joinedload(Classroom.grade)).all()
        }

        # 當月實際在職的員工（與薪資計算同一條件，避免 festival-bonus 預覽與實發分叉）
        employees = (
            session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(_active_employees_in_month_filter(year, month))
            .all()
        )

        results = []
        for emp in employees:
            ctx = {
                "session": session,
                "employee": emp,
                "classroom": (
                    classroom_map.get(emp.classroom_id) if emp.classroom_id else None
                ),
                "school_active_students": school_active,
                "classroom_count_map": cls_count_map,
            }
            bonus_data = engine.calculate_festival_bonus_breakdown(
                emp.id, year, month, _ctx=ctx
            )
            results.append(bonus_data)

        return results


@router.get("/salaries/festival-bonus/period-accrual")
def get_festival_bonus_period_accrual(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    """
    回傳該月所屬發放期「到目前為止」的節慶獎金、超額獎金、會議缺席扣款累積明細。
    發放月（2/6/9/12）回空 rows + is_distribution_month=True。
    DB 往返 O(月數)：每月一組共用資料 batch prefetch，與單月 get_festival_bonus 相同策略。
    """
    # F-013：跨員工彙總端點，僅 admin/hr 可使用；其他角色 403。
    if _resolve_salary_viewer_employee_id(current_user) is not None:
        raise HTTPException(status_code=403, detail="僅可查詢本人薪資")

    from services.salary.utils import (
        get_bonus_distribution_month,
        get_current_period_passed_months,
    )

    passed_months = get_current_period_passed_months(year, month)
    if not passed_months:
        return {
            "is_distribution_month": get_bonus_distribution_month(month),
            "period_start_year": None,
            "period_start_month": None,
            "current_year": year,
            "current_month": month,
            "distribution_year": None,
            "distribution_month": None,
            "rows": [],
        }

    # 發放月：本期之後最近的 2/6/9/12（12 月 → 次年 2）
    distribution_month = next((c for c in (2, 6, 9, 12) if c > month), 2)
    # distribution_year：查詢月 12 已於上方 is_distribution_month 分支提早返回，
    # 主路徑下 distribution_month 必定 > month 且落於同一年；保留變數以對前端語意清晰。
    distribution_year = year + 1 if month == 12 else year

    with session_scope() as session:
        engine = (
            _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=True)
        )

        # 員工過濾：與 get_festival_bonus 一致，只包含當前查詢月在職的員工。
        # 商業語意：節慶獎金以發放月當日在職為條件，期中離職者即使已累積部分獎金
        # 亦不會於發放月領取，預覽功能維持此規則，避免管理者對實發金額產生誤判。
        employees = (
            session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(_active_employees_in_month_filter(year, month))
            .all()
        )

        monthly_ctx_cache: dict[tuple[int, int], dict] = {}
        for y, m in passed_months:
            _, last_day = _cal.monthrange(y, m)
            month_end = date(y, m, last_day)
            monthly_ctx_cache[(y, m)] = {
                "month_end": month_end,
                "school_active": count_students_active_on(session, month_end),
                "cls_count_map": classroom_student_count_map(session, month_end),
                "classroom_map": {
                    c.id: c
                    for c in session.query(Classroom)
                    .options(joinedload(Classroom.grade))
                    .all()
                },
            }

        rows = []
        for emp in employees:
            monthly = []
            for y, m in passed_months:
                ctx_cache = monthly_ctx_cache[(y, m)]
                per_month_ctx = {
                    "session": session,
                    "employee": emp,
                    "classroom": (
                        ctx_cache["classroom_map"].get(emp.classroom_id)
                        if emp.classroom_id
                        else None
                    ),
                    "school_active_students": ctx_cache["school_active"],
                    "classroom_count_map": ctx_cache["cls_count_map"],
                }
                try:
                    row = engine.calculate_period_accrual_row(
                        emp.id, y, m, _ctx=per_month_ctx
                    )
                except Exception:
                    logger.exception(
                        "period-accrual 計算失敗 emp=%s year=%s month=%s",
                        emp.id,
                        y,
                        m,
                    )
                    row = {
                        "festival_bonus": 0,
                        "overtime_bonus": 0,
                        "meeting_absence_deduction": 0,
                        "category": "",
                        "error": "計算失敗",
                    }
                monthly.append({"year": y, "month": m, **row})

            fb_total = sum(r["festival_bonus"] for r in monthly)
            ot_total = sum(r["overtime_bonus"] for r in monthly)
            ded_total = sum(r["meeting_absence_deduction"] for r in monthly)
            category = next(
                (r.get("category") for r in monthly if r.get("category")), ""
            )

            rows.append(
                {
                    "employee_id": emp.id,
                    "name": emp.name,
                    "category": category,
                    "monthly": monthly,
                    "totals": {
                        "festival_bonus": fb_total,
                        "overtime_bonus": ot_total,
                        "meeting_absence_deduction": ded_total,
                        "net_estimate": max(0, fb_total + ot_total - ded_total),
                    },
                }
            )

        return {
            "is_distribution_month": False,
            "period_start_year": passed_months[0][0],
            "period_start_month": passed_months[0][1],
            "current_year": year,
            "current_month": month,
            "distribution_year": distribution_year,
            "distribution_month": distribution_month,
            "rows": rows,
        }


@router.get("/salaries/records")
def get_salary_records(
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
):
    """查詢某月薪資記錄（支援分頁，預設一次最多 500 筆）"""
    _trigger_past_month_snapshot_if_missing(background_tasks)
    with session_scope() as session:
        viewer_employee_id = _resolve_salary_viewer_employee_id(current_user)

        query = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .options(joinedload(Employee.job_title_rel))
            .filter(
                SalaryRecord.salary_year == year, SalaryRecord.salary_month == month
            )
        )
        if viewer_employee_id is not None:
            query = query.filter(SalaryRecord.employee_id == viewer_employee_id)
        records = query.order_by(Employee.name).offset(skip).limit(limit).all()

        results = []
        for record, emp in records:
            job_title = ""
            if emp.job_title_rel:
                job_title = emp.job_title_rel.name
            elif emp.title:
                job_title = emp.title

            results.append(
                {
                    "id": record.id,
                    "version": int(record.version or 1),
                    "employee_id": emp.id,
                    "employee_code": emp.employee_id,
                    "employee_name": emp.name,
                    "job_title": job_title,
                    "base_salary": record.base_salary,
                    "festival_bonus": record.festival_bonus,
                    "overtime_bonus": record.overtime_bonus,
                    "overtime_pay": record.overtime_pay,
                    "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                    "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                    "birthday_bonus": record.birthday_bonus or 0,
                    "performance_bonus": record.performance_bonus,
                    "special_bonus": record.special_bonus,
                    "supervisor_dividend": record.supervisor_dividend or 0,
                    "labor_insurance": record.labor_insurance_employee,
                    "health_insurance": record.health_insurance_employee,
                    "pension": record.pension_employee,
                    "late_deduction": record.late_deduction,
                    "early_leave_deduction": record.early_leave_deduction,
                    "missing_punch_deduction": record.missing_punch_deduction,
                    "absence_deduction": record.absence_deduction or 0,
                    "attendance_deduction": (record.late_deduction or 0)
                    + (record.early_leave_deduction or 0)
                    + (record.missing_punch_deduction or 0),
                    "leave_deduction": record.leave_deduction,
                    "other_deduction": record.other_deduction,
                    "gross_salary": record.gross_salary,
                    "total_deduction": record.total_deduction,
                    "net_salary": record.net_salary,
                    "is_finalized": record.is_finalized,
                    "finalized_at": (
                        record.finalized_at.isoformat() if record.finalized_at else None
                    ),
                    "finalized_by": record.finalized_by,
                    "remark": record.remark,
                    "calculated_at": (
                        record.updated_at.isoformat() if record.updated_at else None
                    ),
                    # 被 manual_adjust 寫過的欄位名單;前端可在欄位旁加上「人工調整」指示器,
                    # 提示該欄位不會被後續上游事件觸發的重算覆寫
                    "manual_overrides": list(record.manual_overrides or []),
                    # 前端 salaryResults 使用的欄位別名（供頁面重整後重建計算結果列表）
                    "pension_self": record.pension_employee or 0,
                    "total_deductions": record.total_deduction or 0,
                    "net_pay": record.net_salary or 0,
                }
            )

        return results


def _parse_if_match(header_value: Optional[str]) -> Optional[int]:
    """解析 If-Match header，支援 W/"3" / "3" / 3 等常見格式。回傳 int 版本號或 None。"""
    if not header_value:
        return None
    raw = header_value.strip()
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    raw = raw.strip('"').strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# 單筆欄位合理上限：從舊版 10,000,000 降為 500,000 — 涵蓋幼稚園業界合法月薪/一次性
# 獎金上限（含 outlier），超過視為誤植或舞弊嘗試。
# Why: 單欄位上限 1000 萬 × 可同時調多欄位 = 單次可偷數千萬。降至 50 萬可有效壓縮
# 舞弊上限，且合法調整不受影響。
_MANUAL_ADJUST_FIELD_MAX = 500_000.0


class SalaryManualAdjustRequest(BaseModel):
    # 必填原因：供稽核追責，避免「from 5000 to 100000」無上下文 audit log
    adjustment_reason: str = Field(
        ...,
        min_length=5,
        max_length=200,
        description="手動調整原因（至少 5 字，例：員工自請補發、主管核准一次性獎勵、誤算修正）",
    )
    base_salary: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    performance_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    special_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    festival_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    overtime_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    overtime_pay: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    supervisor_dividend: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    meeting_overtime_pay: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    birthday_bonus: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    labor_insurance_employee: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    health_insurance_employee: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    pension_employee: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    leave_deduction: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    late_deduction: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    early_leave_deduction: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    missing_punch_deduction: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    meeting_absence_deduction: Optional[float] = Field(
        None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX
    )
    absence_deduction: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    other_deduction: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)


EDITABLE_SALARY_FIELDS = {
    "base_salary": "底薪",
    "performance_bonus": "績效獎金",
    "special_bonus": "特別獎金",
    "festival_bonus": "節慶獎金",
    "overtime_bonus": "超額獎金",
    "overtime_pay": "加班津貼",
    "supervisor_dividend": "主管紅利",
    "meeting_overtime_pay": "會議加班",
    "birthday_bonus": "生日禮金",
    "labor_insurance_employee": "勞保",
    "health_insurance_employee": "健保",
    "pension_employee": "勞退自提",
    "leave_deduction": "請假扣款",
    "late_deduction": "遲到扣款",
    "early_leave_deduction": "早退扣款",
    "missing_punch_deduction": "未打卡扣款",
    "meeting_absence_deduction": "節慶獎金扣減",
    "absence_deduction": "曠職扣款",
    "other_deduction": "其他扣款",
}


@router.put("/salaries/{record_id}/manual-adjust")
def manual_adjust_salary(
    record_id: int,
    data: SalaryManualAdjustRequest,
    response: Response,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    """手動調整單筆薪資記錄。

    若請求帶有 If-Match header，需與目前 record.version 相符才能寫入（樂觀鎖）。
    不帶 If-Match 時允許寫入（舊版前端相容），仍會累加版本號。
    成功時於 ETag / X-Record-Version header 回傳新版本。
    """
    from utils.advisory_lock import acquire_salary_lock

    with session_scope() as session:
        record = (
            session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        )
        if not record:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        # ── A 錢守衛：不得調整自己的 SalaryRecord（純管理員帳號不受限）──
        require_not_self_salary_record(
            current_user, record.employee_id, action="調整自己的薪資紀錄"
        )

        # advisory lock：確保與計算引擎不會同時寫入同筆 SalaryRecord
        acquire_salary_lock(
            session,
            employee_id=record.employee_id,
            year=record.salary_year,
            month=record.salary_month,
        )
        # 鎖住後重讀，取得最新狀態
        session.refresh(record)
        if record.is_finalized:
            raise HTTPException(
                status_code=409, detail="此筆薪資已封存，請先解除封存再編輯"
            )

        client_version = _parse_if_match(if_match)
        current_version = int(record.version or 1)
        if client_version is not None and client_version != current_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"此筆薪資已被他人修改（目前版本 v{current_version}，"
                    f"你持有 v{client_version}），請重新整理後再編輯"
                ),
            )

        payload = data.model_dump(exclude_unset=True)
        # adjustment_reason 為 schema required；從 payload 拉出後只留實際要寫入的金額欄位
        adjustment_reason = (payload.pop("adjustment_reason", "") or "").strip()
        if not payload:
            raise HTTPException(status_code=400, detail="至少需要提供一個調整欄位")

        # 在套用變更前，記下舊的 festival_bonus / meeting_absence_deduction，
        # 用於 #2 連動：若管理員只改 meeting_absence_deduction，
        # 自動回推 raw festival 並重套新的扣減。
        old_festival_bonus = round(record.festival_bonus or 0)
        old_meeting_absence = round(record.meeting_absence_deduction or 0)

        changed_parts = []
        modified_fields = []  # 本次寫過的欄位名單,稍後合併入 record.manual_overrides
        total_abs_delta = 0  # 本次請求所有欄位變動絕對值合計（涵蓋拆欄繞過）
        for field, value in payload.items():
            if field not in EDITABLE_SALARY_FIELDS:
                continue
            old_value = round(getattr(record, field) or 0)
            new_value = round(value or 0)
            if old_value == new_value:
                continue
            setattr(record, field, new_value)
            changed_parts.append(
                f"{EDITABLE_SALARY_FIELDS[field]} {old_value}→{new_value}"
            )
            modified_fields.append(field)
            total_abs_delta += abs(new_value - old_value)

        if not changed_parts:
            raise HTTPException(status_code=400, detail="沒有實際變更")

        # ── A 錢守衛：本次所有欄位 |delta| 合計 > FINANCE_APPROVAL_THRESHOLD 需金流簽核 ──
        # Why: 舊版用「單欄位最大變動」作門檻，會計可一次調 N 欄各 999，總和達數千元
        # 而仍各自低於門檻，繞過 ACTIVITY_PAYMENT_APPROVE。改用合計門檻封死拆欄路徑。
        require_finance_approve(
            total_abs_delta, current_user, action_label="薪資單欄位調整總額"
        )

        # 連動：管理員只改 meeting_absence_deduction（未同時手動覆寫 festival_bonus）時，
        # festival_bonus 應跟著 raw 重算：raw = old_festival + old_meeting_absence。
        meeting_absence_in_payload = "meeting_absence_deduction" in payload
        festival_bonus_in_payload = "festival_bonus" in payload
        if meeting_absence_in_payload and not festival_bonus_in_payload:
            new_meeting_absence = round(record.meeting_absence_deduction or 0)
            inferred_raw = old_festival_bonus + old_meeting_absence
            recomputed_festival = max(0, inferred_raw - new_meeting_absence)
            if recomputed_festival != old_festival_bonus:
                record.festival_bonus = recomputed_festival
                changed_parts.append(
                    f"節慶獎金（連動）{old_festival_bonus}→{recomputed_festival}"
                )
                # 連動寫入的 festival_bonus 也視為人工調整,同一原則保留不被重算覆寫
                modified_fields.append("festival_bonus")

        # 將本次寫過的欄位名稱合併進 manual_overrides;後續上游事件觸發的重算,
        # _fill_salary_record 會跳過清單內的欄位,避免覆寫人工調整。
        existing_overrides = set(record.manual_overrides or [])
        record.manual_overrides = sorted(existing_overrides | set(modified_fields))

        _recalculate_salary_record_totals(record)

        if (record.net_salary or 0) < 0:
            raise HTTPException(
                status_code=400,
                detail=f"調整後淨薪資為負數（{record.net_salary} 元），請確認扣款設定是否正確",
            )

        operator = current_user.get("username") or current_user.get("name") or "管理員"
        audit_note = (
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 手動編輯："
            + "；".join(changed_parts)
            + f"；操作者：{operator}；原因：{adjustment_reason}"
        )
        record.remark = f"{(record.remark or '').strip()}\n{audit_note}".strip()

        record.version = current_version + 1

        logger.warning(
            "手動調整薪資：record_id=%s employee_id=%s fields=%s operator=%s version=%d→%d",
            record.id,
            record.employee_id,
            ",".join(payload.keys()),
            operator,
            current_version,
            record.version,
        )

        new_version = int(record.version)
        response.headers["ETag"] = f'"{new_version}"'
        response.headers["X-Record-Version"] = str(new_version)

        # 結構化 audit：供 AuditMiddleware 寫入 AuditLog（取代原通用「修改薪資」摘要）
        request.state.audit_entity_id = str(record.id)
        request.state.audit_summary = (
            f"手動調整薪資 #{record.id} (員工 {record.employee_id}, "
            f"{record.salary_year}/{record.salary_month:02d}) "
            f"v{current_version}→v{new_version}：" + "；".join(changed_parts)
        )

    # session_scope 退出後 commit，再失效 finance summary 快取
    _invalidate_finance_summary_cache()

    with session_scope() as session:
        record = (
            session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        )
        return {
            "message": "薪資金額已更新",
            "record": {
                "id": record.id,
                "version": new_version,
                "festival_bonus": record.festival_bonus or 0,
                "overtime_bonus": record.overtime_bonus or 0,
                "overtime_pay": record.overtime_pay or 0,
                "supervisor_dividend": record.supervisor_dividend or 0,
                "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                "birthday_bonus": record.birthday_bonus or 0,
                "leave_deduction": record.leave_deduction or 0,
                "late_deduction": record.late_deduction or 0,
                "early_leave_deduction": record.early_leave_deduction or 0,
                "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                "absence_deduction": record.absence_deduction or 0,
                "gross_salary": record.gross_salary or 0,
                "total_deduction": record.total_deduction or 0,
                "net_salary": record.net_salary or 0,
                "bonus_amount": record.bonus_amount or 0,
                "bonus_separate": bool(record.bonus_separate),
                "remark": record.remark,
                "manual_overrides": list(record.manual_overrides or []),
            },
        }


@router.get("/salaries/{record_id}/audit-log")
def get_salary_audit_log(
    record_id: int,
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資的操作歷史（來源：通用 AuditLog 表）。"""
    from models.audit import AuditLog
    from sqlalchemy import desc

    with session_scope() as session:
        record_owner = (
            session.query(SalaryRecord.employee_id)
            .filter(SalaryRecord.id == record_id)
            .scalar()
        )
        if record_owner is None:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
        _enforce_self_or_full_salary(current_user, record_owner)

        logs = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_type == "salary",
                AuditLog.entity_id == str(record_id),
            )
            .order_by(desc(AuditLog.created_at))
            .limit(limit)
            .all()
        )
        return {
            "record_id": record_id,
            "items": [
                {
                    "id": log.id,
                    "action": log.action,
                    "username": log.username,
                    "summary": log.summary,
                    "created_at": (
                        log.created_at.isoformat() if log.created_at else None
                    ),
                }
                for log in logs
            ],
        }


@router.get("/salaries/{record_id}/breakdown")
def get_salary_breakdown(
    record_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資明細。"""
    with session_scope() as session:
        record, emp = session.query(SalaryRecord, Employee).join(
            Employee, SalaryRecord.employee_id == Employee.id
        ).options(joinedload(Employee.job_title_rel)).filter(
            SalaryRecord.id == record_id
        ).first() or (
            None,
            None,
        )
        if not record or not emp:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        _enforce_self_or_full_salary(current_user, record.employee_id)

        job_title = ""
        if emp.job_title_rel:
            job_title = emp.job_title_rel.name
        elif emp.title:
            job_title = emp.title

        return {
            "employee": {
                "record_id": record.id,
                "employee_name": emp.name,
                "employee_code": emp.employee_id,
                "job_title": job_title,
                "year": record.salary_year,
                "month": record.salary_month,
            },
            "earnings": {
                "base_salary": record.base_salary or 0,
                "meeting_overtime_pay": record.meeting_overtime_pay or 0,
                "overtime_pay": record.overtime_pay or 0,
                "gross_salary": record.gross_salary or 0,
            },
            "bonuses": {
                "festival_bonus": record.festival_bonus or 0,
                "overtime_bonus": record.overtime_bonus or 0,
                "supervisor_dividend": record.supervisor_dividend or 0,
                "birthday_bonus": record.birthday_bonus or 0,
            },
            "deductions": {
                "leave_deduction": record.leave_deduction or 0,
                "late_deduction": record.late_deduction or 0,
                "early_leave_deduction": record.early_leave_deduction or 0,
                "meeting_absence_deduction": record.meeting_absence_deduction or 0,
                "absence_deduction": record.absence_deduction or 0,
                "labor_insurance": record.labor_insurance_employee or 0,
                "health_insurance": record.health_insurance_employee or 0,
                "pension": record.pension_employee or 0,
                "total_deduction": record.total_deduction or 0,
            },
            "summary": {
                "net_salary": record.net_salary or 0,
                "bonus_separate": bool(record.bonus_separate),
                "bonus_amount": record.bonus_amount or 0,
            },
            "manual_overrides": list(record.manual_overrides or []),
        }


# ── 薪資 debug snapshot 快取 ─────────────────────────────────────────────
# 同一筆薪資在 UI 切換不同欄位時，snapshot 內容不變（~13 個 DB 查詢）。
# 用 record_id 為 key、(version, data) 為 value，版本更動即失效避免陳舊資料。
_SNAPSHOT_CACHE_TTL_SEC = 60
_SNAPSHOT_CACHE_MAX_SIZE = 256
_snapshot_cache: TTLCache = TTLCache(
    maxsize=_SNAPSHOT_CACHE_MAX_SIZE, ttl=_SNAPSHOT_CACHE_TTL_SEC
)
_snapshot_cache_lock = threading.Lock()


def _snapshot_cache_get(record_id: int, version: int):
    with _snapshot_cache_lock:
        entry = _snapshot_cache.get(record_id)
        if entry is None:
            return None
        cached_version, data = entry
        if cached_version != version:
            _snapshot_cache.pop(record_id, None)
            return None
        return data


def _snapshot_cache_put(record_id: int, version: int, data: dict) -> None:
    with _snapshot_cache_lock:
        _snapshot_cache[record_id] = (version, data)


@router.get("/salaries/{record_id}/field-breakdown")
def get_salary_field_breakdown(
    record_id: int,
    field: str = Query(...),
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """查詢單筆薪資指定欄位明細。"""
    if field not in FIELD_LABELS:
        raise HTTPException(status_code=400, detail="不支援的明細欄位")

    with session_scope() as session:
        record, emp = session.query(SalaryRecord, Employee).join(
            Employee, SalaryRecord.employee_id == Employee.id
        ).options(joinedload(Employee.job_title_rel)).filter(
            SalaryRecord.id == record_id
        ).first() or (
            None,
            None,
        )
        if not record or not emp:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)

        _enforce_self_or_full_salary(current_user, record.employee_id)

        engine = (
            _salary_engine
            if _salary_engine
            else RuntimeSalaryEngine(load_from_db=False)
        )
        version = int(record.version or 1)
        snapshot = _snapshot_cache_get(record_id, version)
        if snapshot is None:
            snapshot = build_salary_debug_snapshot(
                session, engine, emp, record.salary_year, record.salary_month
            )
            _snapshot_cache_put(record_id, version, snapshot)
        return build_field_breakdown(record, emp, snapshot, field)


@router.get("/salaries/{record_id}/export")
def export_salary_slip(
    record_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    format: str = Query("pdf", pattern="^(pdf)$"),
):
    """匯出單人薪資單 PDF"""
    from services.salary_slip import generate_salary_pdf

    with session_scope() as session:
        result = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .filter(SalaryRecord.id == record_id)
            .first()
        )
        if not result:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
        record, emp = result

        _enforce_self_or_full_salary(current_user, record.employee_id)

        pdf_bytes = generate_salary_pdf(
            record, emp, record.salary_year, record.salary_month
        )

        filename = (
            f"salary_{emp.name}_{record.salary_year}_{record.salary_month:02d}.pdf"
        )
        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )


@router.get("/salaries/export-all")
def export_all_salaries(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    format: str = Query("xlsx", pattern="^(xlsx|pdf)$"),
):
    """匯出全部員工薪資（xlsx 或 pdf）僅限 admin/hr。"""
    if current_user.get("role") not in FULL_SALARY_ROLES:
        raise HTTPException(
            status_code=403, detail="僅限系統管理員或人事主管匯出全員薪資"
        )
    with session_scope() as session:
        records = (
            session.query(SalaryRecord, Employee)
            .join(Employee, SalaryRecord.employee_id == Employee.id)
            .filter(
                SalaryRecord.salary_year == year, SalaryRecord.salary_month == month
            )
            .order_by(Employee.name)
            .all()
        )

        if not records:
            raise HTTPException(status_code=404, detail="該月份無薪資記錄")

        if format == "pdf":
            from services.salary_slip import generate_salary_all_pdf

            pdf_bytes = generate_salary_all_pdf(records, year, month)
            filename = f"salary_all_{year}_{month:02d}.pdf"
            return StreamingResponse(
                io.BytesIO(pdf_bytes),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{filename}"
                },
            )

        from services.salary_slip import generate_salary_excel

        excel_bytes = generate_salary_excel(records, year, month)
        filename = f"salary_all_{year}_{month:02d}.xlsx"
        return StreamingResponse(
            io.BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )


@router.get("/salaries/history")
def get_salary_history(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    employee_id: int = Query(...),
    months: int = Query(12, ge=1, le=60),
):
    """查詢員工歷史薪資"""
    _enforce_self_or_full_salary(current_user, employee_id)
    with session_scope() as session:
        records = (
            session.query(SalaryRecord)
            .filter(SalaryRecord.employee_id == employee_id)
            .order_by(SalaryRecord.salary_year.desc(), SalaryRecord.salary_month.desc())
            .limit(months)
            .all()
        )

        results = []
        for r in records:
            total_bonus = calculate_display_bonus_total(r)
            results.append(
                {
                    "id": r.id,
                    "year": r.salary_year,
                    "month": r.salary_month,
                    "base_salary": r.base_salary,
                    "total_bonus": total_bonus,
                    "labor_insurance": r.labor_insurance_employee,
                    "health_insurance": r.health_insurance_employee,
                    "attendance_deduction": (
                        (r.late_deduction or 0)
                        + (r.early_leave_deduction or 0)
                        + (r.missing_punch_deduction or 0)
                    ),
                    "leave_deduction": r.leave_deduction or 0,
                    "gross_salary": r.gross_salary,
                    "total_deduction": r.total_deduction,
                    "total_deductions": r.total_deduction,
                    "net_salary": r.net_salary,
                    "net_pay": r.net_salary,
                }
            )

        return results


@router.get("/salaries/history-all")
def get_salary_history_all(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    """查詢全部員工年度薪資概覽（支援分頁，依員工分組）"""
    viewer_employee_id = _resolve_salary_viewer_employee_id(current_user)
    with session_scope() as session:
        # 先取得符合條件的員工 id 清單（分頁依員工為單位）
        emp_subq = (
            session.query(Employee.id, Employee.name)
            .join(SalaryRecord, SalaryRecord.employee_id == Employee.id)
            .filter(SalaryRecord.salary_year == year)
            .group_by(Employee.id, Employee.name)
            .order_by(Employee.name)
        )
        if viewer_employee_id is not None:
            emp_subq = emp_subq.filter(Employee.id == viewer_employee_id)
        total = emp_subq.count()
        emp_page = emp_subq.offset(skip).limit(limit).all()
        emp_ids = [e.id for e in emp_page]
        emp_name_map = {e.id: e.name for e in emp_page}

        if not emp_ids:
            return {"items": [], "total": total, "skip": skip, "limit": limit}

        records = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.salary_year == year,
                SalaryRecord.employee_id.in_(emp_ids),
            )
            .order_by(SalaryRecord.salary_month)
            .all()
        )

        grouped: dict = {eid: [] for eid in emp_ids}
        for r in records:
            grouped[r.employee_id].append(
                {
                    "month": r.salary_month,
                    "net_salary": r.net_salary,
                    "gross_salary": r.gross_salary,
                }
            )

        results = [
            {
                "employee_id": eid,
                "employee_name": emp_name_map[eid],
                "months": grouped[eid],
            }
            for eid in emp_ids
        ]
        return {"items": results, "total": total, "skip": skip, "limit": limit}


# ============ 薪資封存管理 ============


class FinalizeMonthRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    force: bool = Field(
        False,
        description=(
            "True 時略過「當月在職員工對齊」完整性檢查，仍會封存現有記錄。"
            "若有員工當月在職但無薪資記錄，預設會拒絕封存以避免漏發。"
        ),
    )
    force_reason: Optional[str] = Field(
        None,
        max_length=500,
        description=(
            "force=True 時必填的封存原因（≥ 10 字），會寫入每筆 record.remark "
            "與 audit_summary，供日後稽核回溯為何漏發/略過完整性檢查。"
        ),
    )

    @model_validator(mode="after")
    def _force_requires_reason(self):
        if self.force:
            cleaned = (self.force_reason or "").strip()
            if len(cleaned) < 10:
                raise ValueError(
                    "force=True 時必須在 force_reason 填寫原因（至少 10 字）"
                )
            self.force_reason = cleaned
        return self


def _recalculate_salary_record_totals(record: SalaryRecord):
    """重算 SalaryRecord 聚合欄位 — 委派至 services.salary.totals.recompute_record_totals。

    保留此 wrapper 以維持既有 test/外部 import 相容(tests/test_salary_manual_adjust.py
    與其他 module 直接 from api.salary import _recalculate_salary_record_totals)。
    """
    recompute_record_totals(record)


def _find_missing_salary_employees(session, year: int, month: int) -> list[dict]:
    """回傳當月在職但無任何 SalaryRecord 的員工清單（用於 finalize 前完整性檢查）。

    在職定義對齊 gov_reports._active_employees / salary proration 守衛：
    hire_date <= 月末 且 (resign_date 為 None 或 >= 月初)。
    """
    last_day = _cal.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    active_rows = (
        session.query(Employee.id, Employee.name)
        .filter(
            or_(Employee.hire_date.is_(None), Employee.hire_date <= month_end),
            or_(
                Employee.resign_date.is_(None),
                Employee.resign_date >= month_start,
            ),
        )
        .order_by(Employee.name)
        .all()
    )
    if not active_rows:
        return []
    existing_ids = {
        row[0]
        for row in session.query(SalaryRecord.employee_id)
        .filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .all()
    }
    return [
        {"id": e.id, "name": e.name} for e in active_rows if e.id not in existing_ids
    ]


def _find_stale_salary_employees(session, year: int, month: int) -> list[dict]:
    """回傳該月 SalaryRecord 仍標 needs_recalc=True 的員工清單。

    用途:封存完整性檢查補強。批次重算單筆失敗、假單/加班審核降級時會把
    對應 SalaryRecord 標 stale,本 helper 讓 finalize 能擋下這類記錄。
    已封存(is_finalized=True)的記錄本來就不該再變動,故排除。
    """
    rows = (
        session.query(SalaryRecord, Employee.name)
        .join(Employee, SalaryRecord.employee_id == Employee.id)
        .filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
            SalaryRecord.needs_recalc == True,
            SalaryRecord.is_finalized != True,
        )
        .order_by(Employee.name)
        .all()
    )
    return [{"id": r.employee_id, "name": name} for r, name in rows]


@router.post("/salaries/finalize-month")
def finalize_salary_month(
    data: FinalizeMonthRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """封存整月薪資（封存後禁止重新計算，需手動解封才能修改）"""
    from utils.advisory_lock import acquire_salary_lock

    # ── force=True 必須具備 ACTIVITY_PAYMENT_APPROVE ────────────────
    # Why: force 會略過 missing/stale 完整性檢查，等同允許漏發/封存舊資料；
    # 拉高權限要求避免單一 SALARY_WRITE 即可一鍵封存。reason 由 schema 強制必填。
    if data.force and not has_finance_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                "force=True 強制封存需具備『金流簽核』權限（ACTIVITY_PAYMENT_APPROVE），"
                "請改由具該權限者執行，或先補齊缺漏/重算後再封存"
            ),
        )

    with session_scope() as session:
        # 整月鎖，阻止同月任何重算發生於封存期間
        acquire_salary_lock(session, year=data.year, month=data.month)

        # 第一輪查詢:用於決定要鎖哪些 emp_id;rows 本身在 lock 取得後會 refresh。
        records = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.salary_year == data.year,
                SalaryRecord.salary_month == data.month,
                SalaryRecord.is_finalized != True,
            )
            .all()
        )
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"{data.year} 年 {data.month} 月無可封存的薪資記錄（可能尚未計算，或全部已封存）",
            )

        # 對每位員工取鎖，與 bulk/manual 重算路徑互斥。
        # 必須在 missing/stale 檢查之前完成,否則檢查與封存之間可能有並發 mark_salary_stale。
        for r in records:
            acquire_salary_lock(
                session,
                employee_id=r.employee_id,
                year=data.year,
                month=data.month,
            )

        # 取鎖後 refresh 既有 record,確保看到的 needs_recalc / is_finalized 為 lock 後最新值。
        # Why: pg_advisory_xact_lock 取得後,session.refresh 會以 READ COMMITTED 拉到最新 commit;
        #      避免「query → 並發 mark_stale → 取鎖 → 仍以舊記憶體值封存」的 TOCTOU。
        for r in records:
            session.refresh(r)
        # 過濾掉鎖前一刻被其他流程封存的 record
        records = [r for r in records if not r.is_finalized]
        if not records:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{data.year} 年 {data.month} 月在取得封存鎖時,所有候選紀錄皆已被其他流程封存"
                ),
            )

        # force=True 路徑：先快照被略過的清單，稍後寫入 audit / 每筆 remark
        # (在 lock 與 refresh 之後,避免快照與真實封存之間再發生變化)
        skipped_missing: list[dict] = []
        skipped_stale: list[dict] = []
        if data.force:
            skipped_missing = _find_missing_salary_employees(
                session, data.year, data.month
            )
            skipped_stale = _find_stale_salary_employees(session, data.year, data.month)

        # 完整性檢查：當月在職員工是否都有薪資記錄（含已封存者）
        # 在取得 per-emp lock + refresh 之後執行,確保 stale 旗標反映 lock 後狀態。
        if not data.force:
            missing = _find_missing_salary_employees(session, data.year, data.month)
            if missing:
                names = "、".join(f"{m['name']}(#{m['id']})" for m in missing[:20])
                more = f"…等 {len(missing)} 人" if len(missing) > 20 else ""
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{data.year} 年 {data.month} 月有 {len(missing)} 位在職員工尚無薪資記錄："
                        f"{names}{more}。請先完成薪資計算，或於請求帶 force=true 強制封存（漏發風險自負）。"
                    ),
                )
            # stale 檢查:批次重算單筆失敗、假單/加班審核降級會將 SalaryRecord
            # 標 needs_recalc=True;此處擋下,避免封存到「上游事件後未成功重算」
            # 的舊薪資。force=True 仍可繞過(維持原 missing 一致的逃生口)。
            stale = _find_stale_salary_employees(session, data.year, data.month)
            if stale:
                names = "、".join(f"{s['name']}(#{s['id']})" for s in stale[:20])
                more = f"…等 {len(stale)} 人" if len(stale) > 20 else ""
                logger.warning(
                    "finalize 攔截:%d 年 %d 月有 %d 筆 needs_recalc=True 薪資",
                    data.year,
                    data.month,
                    len(stale),
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{data.year} 年 {data.month} 月有 {len(stale)} 位員工的薪資需重算："
                        f"{names}{more}。請先重新計算薪資,或於請求帶 force=true 強制封存(將封存舊資料,自負漏算/錯算風險)。"
                    ),
                )

        now = datetime.now()
        operator = current_user.get("username") or current_user.get("name") or "管理員"

        # force 路徑：把被略過的清單與原因寫進每筆 record.remark，留稽核痕跡
        force_remark_suffix = ""
        if data.force:
            missing_summary = (
                "、".join(f"{m['name']}(#{m['id']})" for m in skipped_missing[:20])
                or "無"
            )
            stale_summary = (
                "、".join(f"{s['name']}(#{s['id']})" for s in skipped_stale[:20])
                or "無"
            )
            force_remark_suffix = (
                f"\n[{now.strftime('%Y-%m-%d %H:%M')}] FORCE 封存（操作者：{operator}）"
                f"\n原因：{data.force_reason}"
                f"\n略過缺漏（{len(skipped_missing)} 人）：{missing_summary}"
                f"\n略過待重算（{len(skipped_stale)} 人）：{stale_summary}"
            )

        for r in records:
            r.is_finalized = True
            r.finalized_at = now
            r.finalized_by = operator
            if force_remark_suffix:
                r.remark = (r.remark or "") + force_remark_suffix
            _snapshot_svc.create_finalize_snapshot(session, r, operator)
        logger.info(
            "整月薪資封存：%d/%d，共 %d 筆，操作者=%s%s",
            data.year,
            data.month,
            len(records),
            operator,
            (
                f"，FORCE（缺漏 {len(skipped_missing)}/待重算 {len(skipped_stale)}）"
                f"，原因={data.force_reason}"
                if data.force
                else ""
            ),
        )

        # AuditMiddleware summary：把 force 詳情塞進稽核 row（不會被 body mask 掉）
        if data.force:
            request.state.audit_summary = (
                f"FORCE 封存 {data.year}/{data.month} 共 {len(records)} 筆"
                f"（缺漏 {len(skipped_missing)}、待重算 {len(skipped_stale)}）"
                f"；原因：{data.force_reason}；by {operator}"
            )

        count = len(records)
        finalized_at_iso = now.isoformat()

    _invalidate_finance_summary_cache()
    return {
        "message": f"已封存 {data.year} 年 {data.month} 月共 {count} 筆薪資記錄",
        "count": count,
        "finalized_by": operator,
        "finalized_at": finalized_at_iso,
        "force": data.force,
        "skipped_missing": skipped_missing if data.force else [],
        "skipped_stale": skipped_stale if data.force else [],
    }


class UnfinalizeSalaryRequest(BaseModel):
    """解除單筆薪資封存的請求 schema。

    解封等同打開「結帳鎖定」窗口讓上游資料可被修改後重新封存，是高風險操作。
    比照 force 封存：要求原因 ≥10 字 + ACTIVITY_PAYMENT_APPROVE 二人覆核。
    """

    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description=(
            "解封原因（至少 10 字）。會寫入 record.remark 與 audit_summary，供日後稽核"
            "回溯為何重開結帳鎖定。"
        ),
    )


@router.delete("/salaries/{record_id}/finalize")
def unfinalize_salary(
    record_id: int,
    data: UnfinalizeSalaryRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """解除單筆薪資封存（危險操作，要求 reason + ACTIVITY_PAYMENT_APPROVE）"""
    if current_user.get("role") not in ("admin", "hr"):
        raise HTTPException(
            status_code=403, detail="薪資封存解除僅限系統管理員或人事主管操作"
        )
    # ── 比照 force 封存：解封等於重開結帳鎖定窗口，需金流簽核 ───────────────
    # Why: 原設計 unfinalize 只要 SALARY_WRITE + admin/hr，操作者可先解封單筆、
    # 改上游資料/手動調整、再重新封存，繞過原本封存代表的結帳語意。
    if not has_finance_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                "解除薪資封存需具備『金流簽核』權限（ACTIVITY_PAYMENT_APPROVE），"
                "請改由具該權限者執行"
            ),
        )
    reason_cleaned = data.reason.strip()

    from utils.advisory_lock import acquire_salary_lock

    with session_scope() as session:
        record = (
            session.query(SalaryRecord).filter(SalaryRecord.id == record_id).first()
        )
        if not record:
            raise HTTPException(status_code=404, detail=SALARY_RECORD_NOT_FOUND)
        # 不得解除自己的薪資封存（避免一人完成「封存→解封→自我調整」）
        require_not_self_salary_record(
            current_user, record.employee_id, action="解除自己的薪資封存"
        )
        acquire_salary_lock(
            session,
            employee_id=record.employee_id,
            year=record.salary_year,
            month=record.salary_month,
        )
        session.refresh(record)
        if not record.is_finalized:
            raise HTTPException(status_code=409, detail="此筆薪資尚未封存，無需解封")
        operator = current_user.get("username") or current_user.get("name") or "管理員"
        finalized_by_before = record.finalized_by or "未知"
        finalized_at_before = (
            record.finalized_at.isoformat() if record.finalized_at else "未知"
        )
        logger.warning(
            "薪資封存解除！record_id=%d，employee_id=%d，%d/%d，操作者=%s，原因=%s",
            record_id,
            record.employee_id,
            record.salary_year,
            record.salary_month,
            operator,
            reason_cleaned,
        )
        record.is_finalized = False
        audit_note = (
            f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 封存解除"
            f"（操作者：{operator}）"
            f"\n原封存：{finalized_by_before} @ {finalized_at_before}"
            f"\n原因：{reason_cleaned}"
        )
        record.remark = (record.remark or "") + audit_note
        request.state.audit_summary = (
            f"解除薪資封存：record_id={record_id} employee_id={record.employee_id} "
            f"{record.salary_year}/{record.salary_month}；原封存：{finalized_by_before}；"
            f"操作者：{operator}；原因：{reason_cleaned}"
        )
    # 解封後 finance_summary 快取需失效（已封存月份的薪資金額會回變動態）
    _invalidate_finance_summary_cache()
    return {"message": "已解除封存，操作記錄已寫入備註欄位"}


# ============ Salary Snapshots ============


class ManualSnapshotRequest(BaseModel):
    remark: Optional[str] = Field(None, max_length=500)
    employee_id: Optional[int] = Field(None, ge=1, description="空值表示整月快照")


@router.get("/salaries/snapshots")
def list_salary_snapshots(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    employee_id: Optional[int] = Query(None, ge=1),
):
    """列出某月薪資快照（精簡 metadata）。"""
    viewer_employee_id = _resolve_salary_viewer_employee_id(current_user)
    if viewer_employee_id is not None:
        if employee_id is None:
            employee_id = viewer_employee_id
        elif employee_id != viewer_employee_id:
            raise HTTPException(status_code=403, detail="僅可查詢本人薪資")
    with session_scope() as session:
        return {
            "snapshots": _snapshot_svc.list_snapshots(session, year, month, employee_id)
        }


@router.get("/salaries/snapshots/{snapshot_id}")
def get_salary_snapshot(
    snapshot_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """取得單筆快照完整欄位。"""
    with session_scope() as session:
        snap_owner = (
            session.query(SalarySnapshot.employee_id)
            .filter(SalarySnapshot.id == snapshot_id)
            .scalar()
        )
        if snap_owner is None:
            raise HTTPException(status_code=404, detail="找不到該薪資快照")
        _enforce_self_or_full_salary(current_user, snap_owner)
        data = _snapshot_svc.get_snapshot_detail(session, snapshot_id)
        if data is None:
            raise HTTPException(status_code=404, detail="找不到該薪資快照")
        return data


@router.post("/salaries/snapshots")
def create_manual_salary_snapshot(
    data: ManualSnapshotRequest,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    """手動補拍快照（type='manual'）。"""
    operator = current_user.get("username") or current_user.get("name") or "管理員"
    with session_scope() as session:
        count = _snapshot_svc.create_manual_snapshot(
            session,
            year=year,
            month=month,
            captured_by=operator,
            remark=data.remark,
            employee_id=data.employee_id,
        )
        if count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"{year} 年 {month} 月無對應薪資記錄可建立快照",
            )
        return {
            "message": f"已建立 {count} 筆手動快照",
            "count": count,
            "captured_by": operator,
        }


@router.get("/salaries/snapshots/{snapshot_id}/diff")
def get_salary_snapshot_diff(
    snapshot_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """比對快照與當前 SalaryRecord 的欄位差異。"""
    with session_scope() as session:
        snap_owner = (
            session.query(SalarySnapshot.employee_id)
            .filter(SalarySnapshot.id == snapshot_id)
            .scalar()
        )
        if snap_owner is None:
            raise HTTPException(status_code=404, detail="找不到該薪資快照")
        _enforce_self_or_full_salary(current_user, snap_owner)
        data = _snapshot_svc.diff_with_current(session, snapshot_id)
        if data is None:
            raise HTTPException(status_code=404, detail="找不到該薪資快照")
        return data


# ============ Salary Simulation ============

_SIMULATE_COMPARE_KEYS = [
    "base_salary",
    "festival_bonus",
    "overtime_bonus",
    "overtime_pay",
    "late_deduction",
    "early_leave_deduction",
    "leave_deduction",
    "absence_deduction",
    "gross_salary",
    "total_deductions",
    "net_pay",
]


def _build_salary_display_dict(**kwargs) -> dict:
    """共用薪資欄位 dict 建構器，確保兩個轉換函式的欄位名稱與順序一致。"""
    keys = [
        "base_salary",
        "festival_bonus",
        "overtime_bonus",
        "overtime_pay",
        "meeting_overtime_pay",
        "birthday_bonus",
        "supervisor_dividend",
        "labor_insurance",
        "health_insurance",
        "pension_self",
        "late_deduction",
        "early_leave_deduction",
        "missing_punch_deduction",
        "leave_deduction",
        "absence_deduction",
        "meeting_absence_deduction",
        "gross_salary",
        "total_deductions",
        "net_pay",
        "late_count",
        "early_leave_count",
        "missing_punch_count",
    ]
    result = {k: kwargs.get(k, 0) for k in keys}
    # canonical 別名，確保所有端點欄位一致
    result["net_salary"] = result["net_pay"]
    result["total_deduction"] = result["total_deductions"]
    return result


def _breakdown_to_simulate_dict(
    breakdown, late_count: int, early_count: int, missing_count: int
) -> dict:
    return _build_salary_display_dict(
        base_salary=breakdown.base_salary,
        festival_bonus=breakdown.festival_bonus,
        overtime_bonus=breakdown.overtime_bonus,
        overtime_pay=breakdown.overtime_work_pay,
        meeting_overtime_pay=breakdown.meeting_overtime_pay or 0,
        birthday_bonus=breakdown.birthday_bonus or 0,
        supervisor_dividend=breakdown.supervisor_dividend,
        labor_insurance=breakdown.labor_insurance,
        health_insurance=breakdown.health_insurance,
        pension_self=breakdown.pension_self or 0,
        late_deduction=breakdown.late_deduction,
        early_leave_deduction=breakdown.early_leave_deduction,
        missing_punch_deduction=breakdown.missing_punch_deduction,
        leave_deduction=breakdown.leave_deduction,
        absence_deduction=breakdown.absence_deduction or 0,
        meeting_absence_deduction=breakdown.meeting_absence_deduction or 0,
        gross_salary=breakdown.gross_salary,
        total_deductions=breakdown.total_deduction,
        net_pay=breakdown.net_salary,
        late_count=late_count,
        early_leave_count=early_count,
        missing_punch_count=missing_count,
    )


def _record_to_actual_dict(record) -> dict:
    return _build_salary_display_dict(
        base_salary=record.base_salary or 0,
        festival_bonus=record.festival_bonus or 0,
        overtime_bonus=record.overtime_bonus or 0,
        overtime_pay=record.overtime_pay or 0,
        meeting_overtime_pay=record.meeting_overtime_pay or 0,
        birthday_bonus=record.birthday_bonus or 0,
        supervisor_dividend=record.supervisor_dividend or 0,
        labor_insurance=record.labor_insurance_employee or 0,
        health_insurance=record.health_insurance_employee or 0,
        pension_self=record.pension_employee or 0,
        late_deduction=record.late_deduction or 0,
        early_leave_deduction=record.early_leave_deduction or 0,
        missing_punch_deduction=record.missing_punch_deduction or 0,
        leave_deduction=record.leave_deduction or 0,
        absence_deduction=record.absence_deduction or 0,
        meeting_absence_deduction=record.meeting_absence_deduction or 0,
        gross_salary=record.gross_salary or 0,
        total_deductions=record.total_deduction or 0,
        net_pay=record.net_salary or 0,
        late_count=record.late_count or 0,
        early_leave_count=record.early_leave_count or 0,
        missing_punch_count=record.missing_punch_count or 0,
    )


@router.post("/salaries/simulate")
def simulate_salary(
    req: SalarySimulateRequest,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """薪資試算（沙盒模式，不寫入 DB）——套用覆蓋參數後回傳計算結果與實際紀錄對比。"""
    # 權限檢查置於最前:即使薪資引擎未初始化,也不應對 non-admin/hr 洩漏「他人 employee_id 是否存在」之類的訊號
    _enforce_self_or_full_salary(current_user, req.employee_id)
    if not _salary_engine:
        raise HTTPException(status_code=503, detail="薪資引擎尚未初始化")

    engine: RuntimeSalaryEngine = _salary_engine
    ov = req.overrides
    year, month = req.year, req.month

    import calendar as _calendar

    _, last_day = _calendar.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    with session_scope() as session:
        emp = (
            session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(Employee.id == req.employee_id)
            .first()
        )
        if not emp:
            raise HTTPException(
                status_code=404, detail=f"員工 id={req.employee_id} 不存在"
            )
        if emp.employee_type == "hourly":
            raise HTTPException(status_code=422, detail="時薪制員工不支援試算功能")

        emp_dict = engine._load_emp_dict(emp)

        # 查詢實際考勤
        attendances = (
            session.query(Attendance)
            .filter(
                Attendance.employee_id == emp.id,
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date,
            )
            .all()
        )

        # 實際考勤統計
        actual_late_count = sum(1 for a in attendances if a.is_late)
        actual_early_count = sum(1 for a in attendances if a.is_early_leave)
        actual_missing = sum(
            1 for a in attendances if a.is_missing_punch_in or a.is_missing_punch_out
        )
        actual_late_min = sum(a.late_minutes or 0 for a in attendances if a.is_late)
        actual_early_min = sum(
            a.early_leave_minutes or 0 for a in attendances if a.is_early_leave
        )

        # 套用覆蓋
        sim_late_count = (
            ov.late_count if ov.late_count is not None else actual_late_count
        )
        sim_early_count = (
            ov.early_leave_count
            if ov.early_leave_count is not None
            else actual_early_count
        )
        sim_missing = (
            ov.missing_punch_count
            if ov.missing_punch_count is not None
            else actual_missing
        )
        sim_late_min = (
            ov.total_late_minutes
            if ov.total_late_minutes is not None
            else actual_late_min
        )
        sim_early_min = (
            ov.total_early_minutes
            if ov.total_early_minutes is not None
            else actual_early_min
        )
        sim_work_days = ov.work_days if ov.work_days is not None else len(attendances)

        # 將分鐘數均分給每次遲到（給 _calculate_deductions 用）
        emp_dict["_late_details"] = (
            [sim_late_min / sim_late_count] * sim_late_count
            if sim_late_count > 0
            else []
        )

        from services.attendance_parser import AttendanceResult

        attendance_result = AttendanceResult(
            employee_name=emp.name,
            total_days=sim_work_days,
            normal_days=max(0, sim_work_days - sim_late_count - sim_early_count),
            late_count=sim_late_count,
            early_leave_count=sim_early_count,
            missing_punch_in_count=sim_missing,
            missing_punch_out_count=0,
            total_late_minutes=int(sim_late_min),
            total_early_minutes=int(sim_early_min),
            details=[],
        )

        classroom_context, office_staff_context = engine._build_contexts(
            session, emp, end_date
        )
        if ov.enrollment_override is not None:
            if classroom_context:
                classroom_context["current_enrollment"] = ov.enrollment_override
            if office_staff_context:
                office_staff_context["school_enrollment"] = ov.enrollment_override

        daily_salary = calc_daily_salary(emp_dict["base_salary"])
        period_records = engine._load_period_records(
            session, emp, start_date, end_date, year, month, daily_salary
        )

        extra_leave_hours = ov.extra_personal_leave_hours + ov.extra_sick_leave_hours
        hourly_rate = calc_daily_salary(emp_dict["base_salary"]) / 8
        leave_deduction = (
            period_records["leave_deduction"] + extra_leave_hours * hourly_rate
        )
        personal_sick_hours = (
            period_records["personal_sick_leave_hours"] + extra_leave_hours
        )
        overtime_pay = period_records["overtime_work_pay"] + ov.extra_overtime_pay

        absent_count, absence_amount = engine._detect_absences(
            session,
            emp,
            attendances,
            period_records["approved_leaves"],
            start_date,
            end_date,
            year,
            month,
        )

        # 發放月（2/6/9/12）試算須與正式落帳口徑一致：
        # 用期間累積的 festival/overtime 覆蓋單月值，否則 simulate vs actual 的 diff
        # 在發放月會被「單月 vs 期間累積」的口徑差異污染。
        period_festival_total, period_overtime_total = (
            engine._compute_period_accrual_totals(session, emp, year, month)
        )

        breakdown = engine.calculate_salary(
            employee=emp_dict,
            year=year,
            month=month,
            attendance=attendance_result,
            leave_deduction=leave_deduction,
            classroom_context=classroom_context,
            office_staff_context=office_staff_context,
            meeting_context=period_records["meeting_context"],
            overtime_work_pay=overtime_pay,
            personal_sick_leave_hours=personal_sick_hours,
            period_festival_override=period_festival_total,
            period_overtime_override=period_overtime_total,
        )
        breakdown.absent_count = absent_count
        breakdown.absence_deduction = round(absence_amount)
        breakdown.total_deduction = round(breakdown.total_deduction + absence_amount)
        breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction

        actual_record = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == emp.id,
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
            )
            .first()
        )

        simulated = _breakdown_to_simulate_dict(
            breakdown, sim_late_count, sim_early_count, sim_missing
        )
        actual = _record_to_actual_dict(actual_record) if actual_record else None
        diff = (
            {
                k: round(simulated.get(k, 0) - actual.get(k, 0))
                for k in _SIMULATE_COMPARE_KEYS
            }
            if actual
            else None
        )

        overrides_active = [
            k for k, v in ov.model_dump().items() if v is not None and v != 0
        ]

        return {
            "employee": {
                "id": emp.id,
                "name": emp.name,
                "employee_id": emp.employee_id,
                "job_title": emp.title_name,
            },
            "period": {"year": year, "month": month},
            "overrides_active": overrides_active,
            "simulated": simulated,
            "actual": actual,
            "diff": diff,
        }
