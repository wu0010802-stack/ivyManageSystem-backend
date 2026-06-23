"""services/salary/bulk_preload.py — 批次薪資預載與年度累計查詢（F4 第一階段抽出）。

從 services/salary/engine.py 抽出 module-level helper：
- _BulkSalaryPreload — process_bulk_salary_calculation 第一階段批次預載結果 dataclass
- _get_ytd_sick_hours_before — 單員工年度累計病假時數（勞基法 43 條 240h 上限判斷）
- _get_ytd_sick_hours_bulk — 批次版本（多員工一次查詢）

engine.py 保留 re-export 維持既有 import surface
（tests/test_salary_*.py 多檔 import from services.salary.engine 仍可正常解析）。
"""

from dataclasses import dataclass, field
from datetime import date

from models.approval import ApprovalStatus


@dataclass
class _BulkSalaryPreload:
    """process_bulk_salary_calculation 第一階段批次預載結果。

    Why: 以 ~13 次批次 DB query 取代 N×13 個別查詢；將結果打包成單一 bundle
        避免向 per-employee 計算迴圈傳 16 個參數。
    """

    emp_map: dict
    att_by_emp: dict
    classroom_map: dict
    employee_to_classroom: dict
    assistant_to_classes: dict
    art_to_classes: dict
    db_count_map: dict
    total_students: int
    leaves_by_emp: dict
    att_leave_pairs_by_emp: dict
    ytd_sick_by_emp: dict
    ot_by_emp: dict
    meetings_by_emp: dict
    prior_absent_by_emp: dict
    holiday_set: set
    makeup_set: set
    shifts_by_emp: dict
    # BE-P2-1：批次預載消除 per-employee N+1（值與 per-employee query 等價）
    ytd_bonus_by_emp: dict  # {emp_id: float} — query_ytd_bonus_bulk
    appraisal_by_emp: (
        dict  # {emp_id: Decimal} — query_appraisal_year_end_bonus_bulk（非2月為空0）
    )
    skip_by_emp_month: (
        dict  # {(emp_id, y, m): bool} — should_skip_bonuses_bulk（發放月才有）
    )
    pending_payout_by_emp: (
        dict  # {emp_id: [UnusedLeavePayoutLog]} — pending_payout_logs_bulk
    )
    # qa-loop #6：發放月懲處讀取批次預載 {emp_id: [DisciplinaryAction]}，消除
    # _adjust_period_totals_for_discipline 逐員工 get_pending_actions 的 N+1。
    pending_actions_by_emp: dict = field(default_factory=dict)


def _get_ytd_sick_hours_before(
    session, employee_id: int, year: int, month: int
) -> float:
    """查詢 year 年 1/1 起至 year/month/1 前一日為止，指定員工已核准病假時數。

    用於勞基法第 43 條 30 日（240h）半薪上限判斷。跨月假單只要 end_date < 本月 1 日
    就全數納入；若跨入本月，該筆會由當月主查詢一併取到，不重複計算。
    """
    from models.database import LeaveRecord

    year_start = date(year, 1, 1)
    month_start = date(year, month, 1)
    if month_start <= year_start:
        return 0.0

    # 以 end_date 作為落年度判斷：跨年假單（如 2025-12-28 → 2026-01-03）
    # 只要 end_date 落在本年度即納入，避免跨年請假時漏計上限。
    leaves = (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.status == ApprovalStatus.APPROVED.value,
            LeaveRecord.leave_type == "sick",
            LeaveRecord.end_date >= year_start,
            LeaveRecord.end_date < month_start,
        )
        .all()
    )
    return float(sum(lv.leave_hours or 0 for lv in leaves))


def _get_ytd_sick_hours_bulk(
    session, employee_ids: list, year: int, month: int
) -> dict:
    """批次版本：一次查詢多員工的年度累計病假時數。回傳 {employee_id: hours}。"""
    from models.database import LeaveRecord
    from sqlalchemy import func

    year_start = date(year, 1, 1)
    month_start = date(year, month, 1)
    result = {emp_id: 0.0 for emp_id in employee_ids}
    if month_start <= year_start or not employee_ids:
        return result

    rows = (
        session.query(
            LeaveRecord.employee_id,
            func.coalesce(func.sum(LeaveRecord.leave_hours), 0.0),
        )
        .filter(
            LeaveRecord.employee_id.in_(employee_ids),
            LeaveRecord.status == ApprovalStatus.APPROVED.value,
            LeaveRecord.leave_type == "sick",
            LeaveRecord.end_date >= year_start,
            LeaveRecord.end_date < month_start,
        )
        .group_by(LeaveRecord.employee_id)
        .all()
    )
    for emp_id, total in rows:
        result[emp_id] = float(total or 0)
    return result
