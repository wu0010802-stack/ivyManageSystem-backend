"""
api/salary/calculate.py — 薪資批次計算（同步 / 非同步）

含 3 個 endpoint + 1 個限流器 + 2 個 helper：
- POST /salaries/calculate                       同步批次計算（員工數 ≤ MAX_BULK_EMPLOYEES_SYNC）
- POST /salaries/calculate-async                 啟動非同步 job
- GET  /salaries/calculate-jobs/{job_id}         查詢 job 狀態 / 進度

公開 symbol（由 api.salary.__init__ re-export 維持原 surface）：
- _run_salary_calc_job        test_salary_async_job.py 用 monkeypatch.setattr
                              把它換成 fake；calculate-async endpoint 在
                              add_task 時走 `from api.salary import ...`
                              lazy lookup，確保 monkeypatch 生效。

跨組 helper 仍在 api.salary.__init__（test_salary_snapshot 對
_today_taipei 的 monkeypatch 必須留作用點）：
- _active_employees_in_month_filter
- _trigger_past_month_snapshot_if_missing
- _invalidate_finance_summary_cache
- _salary_engine / _line_service（service injection state）
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from models.base import session_scope
from models.database import Employee, SalaryRecord, get_session
from services.salary.engine import SalaryEngine as RuntimeSalaryEngine
from services.salary_job_registry import (
    ActiveJobExistsError,
    registry as _salary_job_registry,
)
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)
router = APIRouter()


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
    # Lazy import：取 __init__ 上的 service-state / cross-group helpers。
    from . import (
        _active_employees_in_month_filter,
        _invalidate_finance_summary_cache,
        _line_service,
    )

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
    # Lazy import：跨組 helper 與 service injection state 留 __init__。
    from . import (
        _active_employees_in_month_filter,
        _invalidate_finance_summary_cache,
        _line_service,
        _trigger_past_month_snapshot_if_missing,
    )

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
    # Lazy lookup _run_salary_calc_job：test_salary_async_job 會
    # `monkeypatch.setattr(salary_module, "_run_salary_calc_job", fake)`，
    # 必須走 api.salary 模組屬性才能讓 monkeypatch 生效。
    from . import _active_employees_in_month_filter
    from api import salary as _salary_module

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

    try:
        job = _salary_job_registry.create(year=year, month=month, total=total)
    except ActiveJobExistsError as race:
        # 跨 worker 同時 POST：本 worker 的 find_active 看不到對方 in-flight 的 job，
        # 但 DB partial unique index 攔截了第二筆 insert。回傳對方已建立的 job。
        existing = race.existing
        raise HTTPException(
            status_code=409,
            detail=(
                f"{year} 年 {month} 月已有計算中的 job（id={existing.job_id}，"
                f"status={existing.status}），請等待目前工作完成或待其結束後再觸發"
            ),
        )
    background_tasks.add_task(
        _salary_module._run_salary_calc_job, job.job_id, year, month
    )
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
