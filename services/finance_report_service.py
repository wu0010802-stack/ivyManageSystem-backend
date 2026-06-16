"""園務統計收支整合 service。

Source provider 模式：每個收入/支出來源是一個純函式，回傳
`dict[int month, int amount]`（或薪資的巢狀 dict）。Aggregator
`build_finance_summary` 把所有 provider 聚合成 API 回傳結構。

新增來源時只需：
1. 寫一個 get_xxx_by_month provider
2. 在 build_finance_summary 對應 category list 加一筆
3. （可選）為新來源補測試
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from models.activity import ActivityPaymentRecord, ActivityRegistration
from models.classroom import Classroom
from models.employee import Employee
from models.fees import (
    StudentFeePayment,
    StudentFeeRecord,
    StudentFeeRefund,
)
from models.monthly_fixed_cost import (
    FIXED_COST_CATEGORIES,
    MonthlyFixedCost,
)
from models.salary import SalaryRecord
from models.vendor_payment import VendorPayment


def _month_totals_from(rows) -> dict[int, int]:
    return {int(m): int(a or 0) for m, a in rows if m is not None}


def _year_range(year: int) -> tuple[date, date]:
    """[start, end_exclusive) for the given year — sargable replacement for
    ``extract('year', col) == year`` so PostgreSQL can use payment_date indexes.
    """
    return date(year, 1, 1), date(year + 1, 1, 1)


def _month_range(year: int, month: int) -> tuple[date, date]:
    """[start, end_exclusive) for (year, month). Wraps to next year on month=12."""
    if month == 12:
        return date(year, 12, 1), date(year + 1, 1, 1)
    return date(year, month, 1), date(year, month + 1, 1)


def _finalized_salary_conditions():
    """財報只認封存且非 stale 的薪資（actual expenditure）。

    對齊 api/reports.py `_query_salary_monthly`：草稿（is_finalized=False）與
    待重算（needs_recalc=True）是中間態，計入會把測試重算的草稿當實際支出，
    讓財務總覽/月度損益/明細/匯出失真（形同 A 錢空間）。所有薪資 provider
    一律套用，避免新增 provider 時漏篩。
    """
    return (
        SalaryRecord.is_finalized == True,  # noqa: E712
        SalaryRecord.needs_recalc == False,  # noqa: E712
    )


# ─────────────────────────────────────────────────────────────────────────────
# 收入 providers
# ─────────────────────────────────────────────────────────────────────────────


def get_tuition_revenue_by_month(session: Session, year: int) -> dict[int, int]:
    """學費已繳金額，按 StudentFeePayment.payment_date 月份聚合（append-only 流水）。

    Why: 舊版用 StudentFeeRecord.status='paid' + payment_date 聚合會有三個問題：
    1) 分期收款第一筆的日期被後續覆寫 → 收入搬到最後月份
    2) 退款後 status 變 partial/unpaid → 整筆收入從月報消失
    3) partial 狀態的現金不計入（條件是 status='paid'）
    改讀 StudentFeePayment 每筆 append-only 流水即可正確歸月。
    """
    start, end = _year_range(year)
    rows = (
        session.query(
            extract("month", StudentFeePayment.payment_date).label("m"),
            func.sum(StudentFeePayment.amount),
        )
        .filter(
            StudentFeePayment.payment_date >= start,
            StudentFeePayment.payment_date < end,
        )
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


def get_tuition_refund_by_month(session: Session, year: int) -> dict[int, int]:
    """學費退款，按 refunded_at 月份聚合。"""
    start, end = _year_range(year)
    rows = (
        session.query(
            extract("month", StudentFeeRefund.refunded_at).label("m"),
            func.sum(StudentFeeRefund.amount),
        )
        .filter(
            StudentFeeRefund.refunded_at >= start,
            StudentFeeRefund.refunded_at < end,
        )
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


def get_activity_revenue_by_month(session: Session, year: int) -> dict[int, int]:
    """才藝繳費（type='payment'），按 payment_date 月份聚合；排除 voided 軟刪紀錄。"""
    start, end = _year_range(year)
    rows = (
        session.query(
            extract("month", ActivityPaymentRecord.payment_date).label("m"),
            func.sum(ActivityPaymentRecord.amount),
        )
        .filter(
            ActivityPaymentRecord.type == "payment",
            ActivityPaymentRecord.payment_date >= start,
            ActivityPaymentRecord.payment_date < end,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


def get_activity_refund_by_month(session: Session, year: int) -> dict[int, int]:
    """才藝退費（type='refund'），按 payment_date 月份聚合；排除 voided 軟刪紀錄。"""
    start, end = _year_range(year)
    rows = (
        session.query(
            extract("month", ActivityPaymentRecord.payment_date).label("m"),
            func.sum(ActivityPaymentRecord.amount),
        )
        .filter(
            ActivityPaymentRecord.type == "refund",
            ActivityPaymentRecord.payment_date >= start,
            ActivityPaymentRecord.payment_date < end,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 支出 providers
# ─────────────────────────────────────────────────────────────────────────────


def get_salary_expense_by_month(
    session: Session, year: int
) -> dict[int, dict[str, int]]:
    """薪資支出按月聚合。

    拆兩層方便前端分類顯示：
    - employee_gross：員工應發（gross_salary + festival_bonus + overtime_bonus）
      Why: SalaryEngine 的 gross_salary 不包含另行轉帳的節慶與超額獎金
      （bonus_separate=True 的部分），但那仍是園方實際現金流出。若不加
      進來，財務總表在獎金發放月會低估 total_expense 與 net_cashflow。
    - employer_benefit：雇主勞健保 + 雇主勞退（園方實質保費支出）
    """
    rows = (
        session.query(
            SalaryRecord.salary_month,
            func.sum(SalaryRecord.gross_salary),
            func.sum(SalaryRecord.festival_bonus),
            func.sum(SalaryRecord.overtime_bonus),
            func.sum(SalaryRecord.labor_insurance_employer),
            func.sum(SalaryRecord.health_insurance_employer),
            func.sum(SalaryRecord.pension_employer),
        )
        .filter(SalaryRecord.salary_year == year, *_finalized_salary_conditions())
        .group_by(SalaryRecord.salary_month)
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for m, gross, fest, ot_bonus, li, hi, pen in rows:
        out[int(m)] = {
            "employee_gross": int((gross or 0) + (fest or 0) + (ot_bonus or 0)),
            "employer_benefit": int((li or 0) + (hi or 0) + (pen or 0)),
        }
    return out


def get_vendor_payment_expense_by_month(session: Session, year: int) -> dict[int, int]:
    """廠商付款支出，按 payment_date 月份聚合。

    無論 status 為 pending 或 signed 都計入：對園所而言錢已付出，差別只在
    廠商是否完成簽收憑證；若只算 signed 會在「未簽收的支出」上低估現金流。
    """
    start, end = _year_range(year)
    rows = (
        session.query(
            extract("month", VendorPayment.payment_date).label("m"),
            func.sum(VendorPayment.amount),
        )
        .filter(
            VendorPayment.payment_date >= start,
            VendorPayment.payment_date < end,
        )
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 月度損益表（Monthly P&L）專屬切片 providers
# ─────────────────────────────────────────────────────────────────────────────
#
# 與既有 finance_summary 不同：finance_summary 給 dashboard 顯示「總收入 / 總支出
# / 淨現金流」三件大事；月度損益表 layout 是試算表 — 左欄 67 行細項 × 12 月，
# user 拿來對自家 Excel 用。所以這裡多幾個 by-method / by-fee-type / by-salary-field
# 切片，僅給 /monthly-pnl 端點消費，不污染 finance_summary 既有契約。
# ─────────────────────────────────────────────────────────────────────────────


# 學費 payment_method 在 DB 為自由 String，但前端目前固定填 "現金"/"轉帳"/"其他"
# （見 api/fees/_helpers.py:122 的正則）。NULL 視同「其他」。
_PAYMENT_METHOD_CASH_LITERALS = ("現金", "cash")
_PAYMENT_METHOD_TRANSFER_LITERALS = ("轉帳", "bank_transfer", "transfer")


def _classify_payment_method(method: str | None) -> str:
    """把 DB 原始 payment_method 字串歸類為 {'cash', 'bank_transfer', 'other_method'}。

    NULL / 空字串 / 未知值 一律歸 'other_method'，避免落空。
    """
    if not method:
        return "other_method"
    if method in _PAYMENT_METHOD_CASH_LITERALS:
        return "cash"
    if method in _PAYMENT_METHOD_TRANSFER_LITERALS:
        return "bank_transfer"
    return "other_method"


def get_tuition_revenue_by_payment_method(
    session: Session, year: int
) -> dict[int, dict[str, int]]:
    """按 payment_date 月份 × payment_method 分類聚合學費收入。

    回傳 dict[month, dict[{'cash','bank_transfer','other_method'}, int]]，
    缺少月份的 key 在 aggregator 端補 0。
    """
    start, end = _year_range(year)
    rows = (
        session.query(
            extract("month", StudentFeePayment.payment_date).label("m"),
            StudentFeePayment.payment_method,
            func.sum(StudentFeePayment.amount),
        )
        .filter(
            StudentFeePayment.payment_date >= start,
            StudentFeePayment.payment_date < end,
        )
        .group_by("m", StudentFeePayment.payment_method)
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for m, method, amount in rows:
        if m is None:
            continue
        bucket = _classify_payment_method(method)
        slot = out.setdefault(
            int(m), {"cash": 0, "bank_transfer": 0, "other_method": 0}
        )
        slot[bucket] += int(amount or 0)
    return out


def get_tuition_revenue_by_fee_type(
    session: Session, year: int
) -> dict[int, dict[str, int]]:
    """按 payment_date 月份 × fee_type 分類聚合學費收入。

    回傳 dict[month, dict[{'registration','material','monthly_tuition'}, int]]：
    - registration：新生註冊費
    - material：耗材費
    - monthly_tuition：monthly / tuition / miscellaneous 合併（user 自家詞彙
      與 codebase 既有 fee_type 對齊不完美，這三類在月度損益表都歸「月費／學費／雜費」列）

    insurance / 其他 fee_type 不在本切片，會在 by-method 列出現（總額不會少算）。
    """
    start, end = _year_range(year)
    rows = (
        session.query(
            extract("month", StudentFeePayment.payment_date).label("m"),
            StudentFeeRecord.fee_type,
            func.sum(StudentFeePayment.amount),
        )
        .join(StudentFeeRecord, StudentFeePayment.record_id == StudentFeeRecord.id)
        .filter(
            StudentFeePayment.payment_date >= start,
            StudentFeePayment.payment_date < end,
        )
        .group_by("m", StudentFeeRecord.fee_type)
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for m, fee_type, amount in rows:
        if m is None:
            continue
        slot = out.setdefault(
            int(m), {"registration": 0, "material": 0, "monthly_tuition": 0}
        )
        if fee_type == "registration":
            slot["registration"] += int(amount or 0)
        elif fee_type == "material":
            slot["material"] += int(amount or 0)
        elif fee_type in ("monthly", "tuition", "miscellaneous"):
            slot["monthly_tuition"] += int(amount or 0)
        # 其他 fee_type（insurance/transport/...）不入此切片
    return out


def get_classroom_count_by_month(session: Session, year: int) -> dict[int, int]:
    """每月 active classroom 數。

    限制：Classroom 模型只有 `is_active` 旗標，無月度時序欄位（created_at /
    deactivated_at 都不適合做歷史快照）。因此 Phase 1 全年 12 月都回傳當下
    `is_active=True` 的班級數 baseline。當有月份切時序時，請改用快照表。

    `year` 參數目前僅用於 API 簽章一致性與未來實作預留，不影響回傳值。
    """
    count = (
        session.query(func.count(Classroom.id))
        .filter(Classroom.is_active.is_(True))
        .scalar()
        or 0
    )
    return {m: int(count) for m in range(1, 13)}


def get_insured_employee_count_by_month(session: Session, year: int) -> dict[int, int]:
    """每月「在職且有勞保投保」的員工數。

    判定條件：
    - `hire_date <= 該月最後一天`（含當月入職）
    - `resign_date IS NULL OR resign_date > 該月第一天`（含當月離職）
    - `labor_insured_salary > 0`（NULL 或 0 不計入；prod 上有些 employee
      labor_insured_salary 為 NULL，沿用 insurance_salary_level，本切片從嚴
      只認真正有設 labor_insured_salary 的列，與「投保人數」字面意義一致）
    """
    out: dict[int, int] = {}
    for m in range(1, 13):
        month_first, month_end_exclusive = _month_range(year, m)
        # 該月最後一天 = next month 起算的前一天，用 < end_exclusive 表達
        # 條件：hire_date < month_end_exclusive AND (resign_date IS NULL OR resign_date > month_first)
        count = (
            session.query(func.count(Employee.id))
            .filter(
                Employee.hire_date.isnot(None),
                Employee.hire_date < month_end_exclusive,
                (Employee.resign_date.is_(None)) | (Employee.resign_date > month_first),
                Employee.labor_insured_salary.isnot(None),
                Employee.labor_insured_salary > 0,
            )
            .scalar()
            or 0
        )
        out[m] = int(count)
    return out


def get_salary_breakdown_by_month(
    session: Session, year: int
) -> dict[int, dict[str, int]]:
    """月度損益表用：薪資的 8 欄細項聚合。

    回傳 dict[month, dict]，每月 dict 含：
    - gross_salary：應發總額 sum
    - festival_bonus：節慶獎金 sum
    - overtime_bonus：超額獎金 sum
    - overtime_pay：加班費 sum
    - supervisor_dividend：主管紅利 sum
    - labor_insurance_employer：勞保（雇主負擔）sum
    - health_insurance_employer：健保（雇主負擔）sum
    - pension_employer：勞退（雇主提撥）sum

    **關鍵 gross_salary 組成確認（see services/salary/totals.py line 21-30）**：
    `gross_salary = base + hourly_total + performance_bonus + special_bonus
                   + supervisor_dividend + meeting_overtime_pay + birthday_bonus
                   + overtime_pay`
    → 含 overtime_pay 與 supervisor_dividend；**不含** festival_bonus / overtime_bonus。

    Spec 原本要求 `personnel_other_bonus = sum(bonus_amount)`，但 bonus_amount =
    `festival_bonus + overtime_bonus + supervisor_dividend`（見 models/salary.py
    line 236-240 與 services/salary/totals.py line 47-51 註解），若直接展列 sum
    會與 festival_bonus / overtime_bonus 三重計算。aggregator 端改用
    `supervisor_dividend` 當「其他獎金」列，並把 `personnel_base_salary` 公式
    調整為 `gross_salary - overtime_pay - supervisor_dividend`，確保 subtotal
    無雙計（細節見 monthly_pnl_service.build_monthly_pnl docstring）。
    """
    rows = (
        session.query(
            SalaryRecord.salary_month,
            func.sum(SalaryRecord.gross_salary),
            func.sum(SalaryRecord.festival_bonus),
            func.sum(SalaryRecord.overtime_bonus),
            func.sum(SalaryRecord.overtime_pay),
            func.sum(SalaryRecord.supervisor_dividend),
            func.sum(SalaryRecord.labor_insurance_employer),
            func.sum(SalaryRecord.health_insurance_employer),
            func.sum(SalaryRecord.pension_employer),
        )
        .filter(SalaryRecord.salary_year == year, *_finalized_salary_conditions())
        .group_by(SalaryRecord.salary_month)
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for (
        m,
        gross,
        festival,
        ot_bonus,
        ot_pay,
        sup_div,
        li,
        hi,
        pen,
    ) in rows:
        out[int(m)] = {
            "gross_salary": int(gross or 0),
            "festival_bonus": int(festival or 0),
            "overtime_bonus": int(ot_bonus or 0),
            "overtime_pay": int(ot_pay or 0),
            "supervisor_dividend": int(sup_div or 0),
            "labor_insurance_employer": int(li or 0),
            "health_insurance_employer": int(hi or 0),
            "pension_employer": int(pen or 0),
        }
    return out


def get_salary_breakdown_by_month_with_role(
    session: Session, year: int
) -> dict[int, dict[str, dict[str, int]]]:
    """月度損益表 Phase 2：薪資 by month by role by field 切片。

    回傳 dict[month, {regular: {...8 fields}, hourly: {...8 fields}}]，
    role = employee.employee_type ∈ {regular, hourly}；hourly 即才藝老師。

    Aggregator 用 hourly 部分組「才藝鐘點薪資」row、regular 部分組 base 薪資，
    確保 base_salary 不重複計入 hourly。

    Note: 既有 `get_salary_breakdown_by_month` 保留，回傳 sum over role；本函式
    為 Phase 2 新增，不取代既有 API。
    """
    rows = (
        session.query(
            SalaryRecord.salary_month,
            Employee.employee_type,
            func.sum(SalaryRecord.gross_salary),
            func.sum(SalaryRecord.festival_bonus),
            func.sum(SalaryRecord.overtime_bonus),
            func.sum(SalaryRecord.overtime_pay),
            func.sum(SalaryRecord.supervisor_dividend),
            func.sum(SalaryRecord.labor_insurance_employer),
            func.sum(SalaryRecord.health_insurance_employer),
            func.sum(SalaryRecord.pension_employer),
        )
        .join(Employee, Employee.id == SalaryRecord.employee_id)
        .filter(SalaryRecord.salary_year == year, *_finalized_salary_conditions())
        .group_by(SalaryRecord.salary_month, Employee.employee_type)
        .all()
    )

    def _empty_role_dict() -> dict[str, int]:
        return {
            "gross_salary": 0,
            "festival_bonus": 0,
            "overtime_bonus": 0,
            "overtime_pay": 0,
            "supervisor_dividend": 0,
            "labor_insurance_employer": 0,
            "health_insurance_employer": 0,
            "pension_employer": 0,
        }

    out: dict[int, dict[str, dict[str, int]]] = {}
    for (
        m,
        emp_type,
        gross,
        festival,
        ot_bonus,
        ot_pay,
        sup_div,
        li,
        hi,
        pen,
    ) in rows:
        month_dict = out.setdefault(
            int(m),
            {"regular": _empty_role_dict(), "hourly": _empty_role_dict()},
        )
        # 員工 employee_type 預期只有 regular/hourly；防禦性：未知值歸入 regular。
        role_key = "hourly" if emp_type == "hourly" else "regular"
        bucket = month_dict[role_key]
        bucket["gross_salary"] += int(gross or 0)
        bucket["festival_bonus"] += int(festival or 0)
        bucket["overtime_bonus"] += int(ot_bonus or 0)
        bucket["overtime_pay"] += int(ot_pay or 0)
        bucket["supervisor_dividend"] += int(sup_div or 0)
        bucket["labor_insurance_employer"] += int(li or 0)
        bucket["health_insurance_employer"] += int(hi or 0)
        bucket["pension_employer"] += int(pen or 0)
    return out


def get_monthly_fixed_cost_by_category(
    session: Session, year: int
) -> dict[int, dict[str, int]]:
    """月度損益表 Phase 2：固定費用 by month by category。

    回傳 dict[month, {category: amount}]，category 為 MonthlyFixedCost
    8 個 enum 值之一。aggregator 端拆 7 條進「變動支出」section，
    `old_pension_reserve` 進「人事支出」section（屬勞退非變動）。
    """
    rows = (
        session.query(
            MonthlyFixedCost.month,
            MonthlyFixedCost.category,
            func.sum(MonthlyFixedCost.amount),
        )
        .filter(MonthlyFixedCost.year == year)
        .group_by(MonthlyFixedCost.month, MonthlyFixedCost.category)
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for m, cat, amt in rows:
        out.setdefault(int(m), {})[str(cat)] = int(amt or 0)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────────────


def build_finance_summary(
    session: Session, year: int, month: Optional[int] = None
) -> dict:
    """聚合所有來源為 API 回傳結構。"""
    tuition_rev = get_tuition_revenue_by_month(session, year)
    tuition_ref = get_tuition_refund_by_month(session, year)
    activity_rev = get_activity_revenue_by_month(session, year)
    activity_ref = get_activity_refund_by_month(session, year)
    salary_exp = get_salary_expense_by_month(session, year)
    vendor_exp = get_vendor_payment_expense_by_month(session, year)

    months = list(range(1, 13)) if month is None else [month]

    trend = []
    for m in months:
        revenue = tuition_rev.get(m, 0) + activity_rev.get(m, 0)
        refund = tuition_ref.get(m, 0) + activity_ref.get(m, 0)
        sal = salary_exp.get(m, {"employee_gross": 0, "employer_benefit": 0})
        vendor_m = vendor_exp.get(m, 0)
        expense = sal["employee_gross"] + sal["employer_benefit"] + vendor_m
        trend.append(
            {
                "month": m,
                "revenue": revenue,
                "refund": refund,
                "expense": expense,
                "net": (revenue - refund) - expense,
            }
        )

    total_rev = sum(r["revenue"] for r in trend)
    total_ref = sum(r["refund"] for r in trend)
    total_exp = sum(r["expense"] for r in trend)

    tuition_rev_total = sum(tuition_rev.get(m, 0) for m in months)
    tuition_ref_total = sum(tuition_ref.get(m, 0) for m in months)
    activity_rev_total = sum(activity_rev.get(m, 0) for m in months)
    activity_ref_total = sum(activity_ref.get(m, 0) for m in months)
    gross_total = sum(salary_exp.get(m, {}).get("employee_gross", 0) for m in months)
    employer_total = sum(
        salary_exp.get(m, {}).get("employer_benefit", 0) for m in months
    )
    vendor_total = sum(vendor_exp.get(m, 0) for m in months)

    return {
        "period": {"year": year, "month": month},
        "summary": {
            "total_revenue": total_rev,
            "total_refund": total_ref,
            "net_revenue": total_rev - total_ref,
            "total_expense": total_exp,
            "net_cashflow": (total_rev - total_ref) - total_exp,
        },
        "revenue_by_category": [
            {
                "category": "tuition",
                "label": "學費",
                "amount": tuition_rev_total,
                "refund": tuition_ref_total,
            },
            {
                "category": "activity",
                "label": "才藝",
                "amount": activity_rev_total,
                "refund": activity_ref_total,
            },
        ],
        "expense_by_category": [
            {
                "category": "salary_gross",
                "label": "員工應發",
                "amount": gross_total,
            },
            {
                "category": "employer_benefit",
                "label": "雇主保費+勞退",
                "amount": employer_total,
            },
            {
                "category": "vendor_payment",
                "label": "廠商付款",
                "amount": vendor_total,
            },
        ],
        "monthly_trend": trend,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 月度明細（下鑽）
# ─────────────────────────────────────────────────────────────────────────────


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def get_tuition_detail(session: Session, year: int, month: int) -> list[dict]:
    """回傳該月學費繳費 + 退款的明細列。

    每筆來源以 kind 區分：'payment'（已繳學費流水）或 'refund'（退款）。
    繳費從 StudentFeePayment 逐筆取（保留原收款日），JOIN record 拿學生/項目 snapshot。
    """
    out: list[dict] = []
    start, end = _month_range(year, month)
    paid_rows = (
        session.query(StudentFeePayment, StudentFeeRecord)
        .join(StudentFeeRecord, StudentFeePayment.record_id == StudentFeeRecord.id)
        .filter(
            StudentFeePayment.payment_date >= start,
            StudentFeePayment.payment_date < end,
        )
        .order_by(StudentFeePayment.payment_date)
        .all()
    )
    for payment, record in paid_rows:
        out.append(
            {
                "kind": "payment",
                "date": _iso(payment.payment_date),
                "student_name": record.student_name,
                "classroom_name": record.classroom_name,
                "fee_item_name": record.fee_item_name,
                "amount": int(payment.amount or 0),
                "payment_method": payment.payment_method,
            }
        )
    refunds = (
        session.query(StudentFeeRefund)
        .filter(
            StudentFeeRefund.refunded_at >= start,
            StudentFeeRefund.refunded_at < end,
        )
        .all()
    )
    for r in refunds:
        out.append(
            {
                "kind": "refund",
                "date": _iso(r.refunded_at.date() if r.refunded_at else None),
                "amount": int(r.amount or 0),
                "reason": r.reason,
                "refunded_by": r.refunded_by,
            }
        )
    out.sort(key=lambda x: x.get("date") or "")
    return out


def get_activity_detail(session: Session, year: int, month: int) -> list[dict]:
    """才藝繳費/退費明細（含報名關聯的學生姓名）；排除 voided 軟刪紀錄。"""
    start, end = _month_range(year, month)
    rows = (
        session.query(ActivityPaymentRecord, ActivityRegistration)
        .outerjoin(
            ActivityRegistration,
            ActivityPaymentRecord.registration_id == ActivityRegistration.id,
        )
        .filter(
            ActivityPaymentRecord.payment_date >= start,
            ActivityPaymentRecord.payment_date < end,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .order_by(ActivityPaymentRecord.payment_date)
        .all()
    )
    result = []
    for rec, reg in rows:
        result.append(
            {
                "kind": rec.type,  # payment / refund
                "date": _iso(rec.payment_date),
                "registration_id": rec.registration_id,
                "student_name": getattr(reg, "student_name", None) if reg else None,
                "amount": int(rec.amount or 0),
                "payment_method": rec.payment_method,
                "operator": rec.operator,
                "receipt_no": rec.receipt_no,
            }
        )
    return result


def get_salary_detail(session: Session, year: int, month: int) -> list[dict]:
    """該月所有員工薪資支出明細（每人一列）。"""
    rows = (
        session.query(SalaryRecord, Employee)
        .join(Employee, SalaryRecord.employee_id == Employee.id)
        .filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
            *_finalized_salary_conditions(),
        )
        .order_by(Employee.name)
        .all()
    )
    result = []
    for rec, emp in rows:
        gross = int(rec.gross_salary or 0)
        festival = int(rec.festival_bonus or 0)
        overtime_bonus = int(rec.overtime_bonus or 0)
        employer = (
            int(rec.labor_insurance_employer or 0)
            + int(rec.health_insurance_employer or 0)
            + int(rec.pension_employer or 0)
        )
        # 對齊月摘要 get_salary_expense_by_month：employee_gross = gross + festival + overtime
        # supervisor_dividend 已含於 gross_salary，不再重複加。
        real_cost = gross + festival + overtime_bonus + employer
        result.append(
            {
                "employee_name": emp.name,
                "employee_id": emp.id,
                "gross_salary": gross,
                "festival_bonus": festival,
                "overtime_bonus": overtime_bonus,
                "net_salary": int(rec.net_salary or 0),
                "employer_benefit": employer,
                "real_cost": real_cost,
                "is_finalized": bool(rec.is_finalized),
            }
        )
    return result


def get_vendor_payment_detail(session: Session, year: int, month: int) -> list[dict]:
    """該月廠商付款明細，按付款日期排序。"""
    start, end = _month_range(year, month)
    rows = (
        session.query(VendorPayment)
        .filter(
            VendorPayment.payment_date >= start,
            VendorPayment.payment_date < end,
        )
        .order_by(VendorPayment.payment_date)
        .all()
    )
    return [
        {
            "id": r.id,
            "date": _iso(r.payment_date),
            "vendor_name": r.vendor_name,
            "amount": int(r.amount or 0),
            "payment_method": r.payment_method,
            "description": r.description,
            "invoice_number": r.invoice_number,
            "status": r.status,
        }
        for r in rows
    ]


def build_finance_detail(session: Session, year: int, month: int) -> dict:
    """下鑽明細彙總：回傳四來源的明細陣列。"""
    return {
        "period": {"year": year, "month": month},
        "tuition": get_tuition_detail(session, year, month),
        "activity": get_activity_detail(session, year, month),
        "salary": get_salary_detail(session, year, month),
        "vendor_payment": get_vendor_payment_detail(session, year, month),
    }
