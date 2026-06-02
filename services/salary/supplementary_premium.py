"""二代健保補充保費 — 獎金路徑（健保法 §31 第 1 項第 1 款）。

法規定義：「所領取之全年累計逾當月投保金額四倍部分之獎金」扣 2.11%。
- 「全年累計」= 該年度（1/1~12/31）所有非經常性給予獎金加總
- 「當月投保金額」= 該獎金發放當月的健保投保薪資（threshold = 4 倍）
- 「逾部分」= 採 per-payment 增額制，避免重複扣繳

本檔僅處理「獎金路徑」；既有「兼職薪資路徑（月累計 ≥ 基本工資）」在
`services/salary/engine.py:1567` 維持不變，未列入本檔重構。

入累計獎金（業主確認分類，2026-05-26）：
- festival_bonus       三節獎金
- overtime_bonus       超額獎金
- performance_bonus    績效獎金
- special_bonus        特別獎金/紅利
- supervisor_dividend  主管紅利（業主視為非經常性獎金性質）

不入累計：
- appraisal_year_end_bonus  考核年終（決策⑥B：已移至年終獨立轉帳，表外，不計入補充保費）
- birthday_bonus       生日禮金（福利金性質）
- overtime_pay / meeting_overtime_pay  加班費（經常性給予）
- base_salary / 各 deduction
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.salary import SalaryRecord
from services.salary.breakdown import SalaryBreakdown
from services.salary.insurance_salary import resolve_insurance_salary_raw
from utils.rounding import round_half_up

# 列入年累計的 SalaryRecord 欄位（非經常性給予獎金）
BONUS_FIELDS_FOR_YTD = (
    "festival_bonus",
    "overtime_bonus",
    "performance_bonus",
    "special_bonus",
    "supervisor_dividend",
)


def query_ytd_bonus_before(
    session: Session, employee_id: int, year: int, month: int
) -> float:
    """查該員工該年度 1 月至 (month-1) 月已落 SalaryRecord 的獎金累計。

    重算情境：例如 3 月重算 1 月，month=1 → 回 0（1 月之前無紀錄）。
    """
    columns = [getattr(SalaryRecord, name) for name in BONUS_FIELDS_FOR_YTD]
    row_total = sum(func.coalesce(col, 0) for col in columns)
    stmt = select(func.coalesce(func.sum(row_total), 0)).where(
        SalaryRecord.employee_id == employee_id,
        SalaryRecord.salary_year == year,
        SalaryRecord.salary_month < month,
    )
    result = session.execute(stmt).scalar()
    return float(result or 0)


def query_ytd_bonus_bulk(
    session: Session, employee_ids: list[int], year: int, month: int
) -> dict[int, float]:
    """批次版 query_ytd_bonus_before：一次 GROUP BY 查回 {employee_id: ytd_bonus}。

    語意與 per-employee 版完全一致（同欄位、同 year/month<month 條件）；缺紀錄者回 0.0。
    """
    result = {eid: 0.0 for eid in employee_ids}
    if not employee_ids:
        return result
    columns = [getattr(SalaryRecord, name) for name in BONUS_FIELDS_FOR_YTD]
    row_total = sum(func.coalesce(col, 0) for col in columns)
    rows = session.execute(
        select(
            SalaryRecord.employee_id,
            func.coalesce(func.sum(row_total), 0),
        )
        .where(
            SalaryRecord.employee_id.in_(employee_ids),
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month < month,
        )
        .group_by(SalaryRecord.employee_id)
    ).all()
    for eid, total in rows:
        result[eid] = float(total or 0)
    return result


def calculate_bonus_supplementary_fee(
    session: Session,
    employee_id: int,
    year: int,
    month: int,
    *,
    breakdown_bonus_total: float,
    health_insured_salary: float,
    rate: float = 0.0211,
    ytd_before: float | None = None,
) -> float:
    """計算本月應扣的「獎金補充保費」。

    公式（per-payment incremental）：
        current_month_total = breakdown_bonus_total  # 不含 appraisal（決策⑥B）
        ytd_before = ∑ SalaryRecord(bonus_fields) WHERE year==year AND month<this_month
        ytd_after  = ytd_before + current_month_total
        threshold  = 4 × health_insured_salary
        basis      = max(ytd_before, threshold)
            # 第一次破門檻：ytd_before < threshold，basis=threshold（僅扣超門檻部分）
            # 累計已破門檻：ytd_before ≥ threshold，basis=ytd_before（本月全額扣）
        excess     = max(0, ytd_after - basis)
        fee        = round_half_up(excess × rate)

    Args:
        session: SQLAlchemy session
        employee_id: 員工 ID（int，對應 SalaryRecord.employee_id）
        year, month: 計算年月
        breakdown_bonus_total: 當月 breakdown 已算的列入累計獎金合計
            = festival_bonus + overtime_bonus + performance_bonus
            + special_bonus + supervisor_dividend
            （不含 appraisal_year_end_bonus；決策⑥B 已移至年終獨立轉帳，不計入補充保費）
        health_insured_salary: 當月健保投保薪資（NULL 時由 caller 用 fallback 算好傳入）
        rate: 補充保費費率，預設 0.0211（115 年）

    Returns:
        本月應扣補充保費（int 元）；不扣回 0。
    """
    if health_insured_salary <= 0 or rate <= 0:
        return 0

    current_month_total = float(breakdown_bonus_total)
    if current_month_total <= 0:
        return 0

    threshold = 4.0 * float(health_insured_salary)
    ytd = (
        ytd_before
        if ytd_before is not None
        else query_ytd_bonus_before(session, employee_id, year, month)
    )
    ytd_after = ytd + current_month_total
    basis = max(ytd, threshold)
    excess = max(0.0, ytd_after - basis)
    if excess <= 0:
        return 0
    return round_half_up(excess * rate)


def _resolve_health_insured_salary(emp_dict: dict, insurance_service) -> float:
    """解出當月健保投保薪資（已 bracket 正規化）。

    優先序：emp_dict["health_insured_salary"] → bracket(resolved_raw)。
    無投保者（raw <= 0）回 0，caller 視為「不計補充保費」。
    """
    raw = resolve_insurance_salary_raw(
        employee_type=emp_dict.get("employee_type") or "regular",
        base_salary=emp_dict.get("base_salary", 0) or 0,
        insurance_salary_level=emp_dict.get("insurance_salary"),
        hourly_rate=emp_dict.get("hourly_rate", 0),
    )
    if raw <= 0:
        return 0.0
    bracket_amount = float(insurance_service.get_bracket(raw)["amount"])
    health_ins = emp_dict.get("health_insured_salary")
    return float(health_ins) if health_ins is not None else bracket_amount


def apply_bonus_supplementary_to_breakdown(
    session: Session,
    emp_dict: dict,
    breakdown: SalaryBreakdown,
    year: int,
    month: int,
    insurance_service,
    employee_pk: int,
    ytd_before: float | None = None,
) -> int:
    """計算獎金補充保費並 mutates breakdown 四個欄位：
    health_insurance / supplementary_health_employee / total_deduction / net_salary。

    回傳本月應扣金額（int 元；0 表示不扣）。
    時薪制既有「兼職薪資路徑」（engine.py:1567）已將其 supplementary_health_employee
    設值，本函式以 += 累計，兩條路徑共存（hourly 員工同時拿獎金時兩者都會扣）。
    """
    rate = float(
        getattr(insurance_service, "supplementary_health_rate", 0.0211) or 0.0211
    )
    health_insured_salary = _resolve_health_insured_salary(emp_dict, insurance_service)
    if health_insured_salary <= 0 or rate <= 0:
        return 0

    breakdown_bonus_total = (
        float(breakdown.festival_bonus or 0)
        + float(breakdown.overtime_bonus or 0)
        + float(breakdown.performance_bonus or 0)
        + float(breakdown.special_bonus or 0)
        + float(breakdown.supervisor_dividend or 0)
    )

    fee = calculate_bonus_supplementary_fee(
        session,
        employee_pk,
        year,
        month,
        breakdown_bonus_total=breakdown_bonus_total,
        health_insured_salary=health_insured_salary,
        rate=rate,
        ytd_before=ytd_before,
    )
    if fee <= 0:
        return 0

    breakdown.health_insurance = round_half_up(
        float(breakdown.health_insurance or 0) + fee
    )
    breakdown.supplementary_health_employee = (
        float(breakdown.supplementary_health_employee or 0) + fee
    )
    breakdown.total_deduction = round_half_up(
        float(breakdown.total_deduction or 0) + fee
    )
    breakdown.net_salary = float(breakdown.gross_salary or 0) - float(
        breakdown.total_deduction or 0
    )
    return fee
