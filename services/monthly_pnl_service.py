"""月度損益表 aggregator。

把 finance_report_service 的 provider 函式組合成 API 回傳結構。Layout 為
試算表：左欄為項目名、12 月欄、合計欄；分為四 section（統計指標 / 收入 /
人事支出 / 變動支出），加 totals + pending_items。

Phase 1 範圍：只整合已有資料來源的 ~22 列，其餘 user 自家詞彙 / 紅利細項 /
固定費用 / 個別廠商分項列在 `pending_items`，不假裝填 0。

設計決策（與 spec 偏差請看 `personnel_*` 區塊註解）：
1. `personnel_base_salary = gross_salary - overtime_pay - supervisor_dividend`
   而非 spec 原 `gross_salary - festival_bonus - overtime_bonus - bonus_amount`。
   原因：DB 的 gross_salary 已含 overtime_pay 與 supervisor_dividend，**不含**
   festival_bonus / overtime_bonus；bonus_amount = festival+overtime+supervisor_dividend
   是 display 用聚合，若按 spec 字面 sum 會與單欄列三重計算且 subtotal 含意混淆。
2. `personnel_other_bonus = supervisor_dividend`：唯一真正「其他獎金」（已包進
   gross_salary 但與 festival / overtime / overtime_pay 區隔）。
3. `income_subtotal` 只加總 by-payment-method 切片（cash + transfer + other +
   activity），不加 by-fee-type，避免雙重計算（兩組是同一筆繳費的正交切片）。
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy.orm import Session

from services.finance_report_service import (
    get_activity_refund_by_month,
    get_activity_revenue_by_month,
    get_classroom_count_by_month,
    get_insured_employee_count_by_month,
    get_salary_breakdown_by_month,
    get_tuition_refund_by_month,
    get_tuition_revenue_by_fee_type,
    get_tuition_revenue_by_payment_method,
    get_vendor_payment_expense_by_month,
)

_MONTHS: tuple[int, ...] = tuple(range(1, 13))


def _row(
    key: str,
    label: str,
    unit: str,
    monthly: list[int],
    *,
    include_total: bool = True,
    is_subtotal: bool = False,
) -> dict:
    """建立一筆 row dict。

    - amount 列 include_total=True 計合計；統計列 unit='person'/'class' 不算合計
      （傳 include_total=False，total 為 None）。
    """
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "monthly": list(monthly),
        "total": sum(monthly) if include_total else None,
        "is_subtotal": is_subtotal,
    }


def _zeros() -> list[int]:
    return [0] * 12


def _sum_lists(lists: Iterable[list[int]]) -> list[int]:
    out = _zeros()
    for lst in lists:
        for i, v in enumerate(lst):
            out[i] += v
    return out


def _diff_lists(a: list[int], b: list[int]) -> list[int]:
    return [a[i] - b[i] for i in range(12)]


_PENDING_ITEMS: tuple[str, ...] = (
    "全校節慶人數（user 自訂指標，未對應 schema）",
    "預繳收入（fee_adjustments=prepayment 為折抵非收入；招生階段 deposit 不在 fee_payment 流水）",
    "畢業紀念冊（無對應 fee_type，建議於 vendor_payments 登錄收入或自訂 fee_type）",
    "紅利細項：年終／主管分紅／招生／教課鼓勵／自主成長契約／出國尾牙／註冊預繳獎金（salary 模型只有 festival_bonus/overtime_bonus/bonus_amount 三欄）",
    "舊制勞退準備金（固定每月支出，無自動來源）",
    "二代健保（無對應欄位）",
    "才藝鐘點薪資（需依 employee.employee_type 區分，Phase 1 未拆分）",
    "租金／辦公室零用金／廚房零用金／餐點採購／水電費（請於 vendor_payments 登錄）",
    "個別廠商分項列（vendor_payments.vendor_name 為自由文字，無 row-level 分類；Phase 2 可加 vendor 分類映射表）",
)


def build_monthly_pnl(session: Session, year: int) -> dict:
    """聚合月度損益表回傳結構。"""

    # ── 收入切片 ─────────────────────────────────────────────────────────
    by_method = get_tuition_revenue_by_payment_method(session, year)
    by_fee_type = get_tuition_revenue_by_fee_type(session, year)
    activity_rev = get_activity_revenue_by_month(session, year)
    tuition_ref = get_tuition_refund_by_month(session, year)
    activity_ref = get_activity_refund_by_month(session, year)

    cash_monthly = [by_method.get(m, {}).get("cash", 0) for m in _MONTHS]
    transfer_monthly = [by_method.get(m, {}).get("bank_transfer", 0) for m in _MONTHS]
    other_method_monthly = [
        by_method.get(m, {}).get("other_method", 0) for m in _MONTHS
    ]
    registration_monthly = [
        by_fee_type.get(m, {}).get("registration", 0) for m in _MONTHS
    ]
    material_monthly = [by_fee_type.get(m, {}).get("material", 0) for m in _MONTHS]
    monthly_tuition_monthly = [
        by_fee_type.get(m, {}).get("monthly_tuition", 0) for m in _MONTHS
    ]
    activity_monthly = [int(activity_rev.get(m, 0)) for m in _MONTHS]

    income_subtotal_monthly = _sum_lists(
        [
            cash_monthly,
            transfer_monthly,
            other_method_monthly,
            activity_monthly,
        ]
    )
    refund_monthly = [
        int(tuition_ref.get(m, 0)) + int(activity_ref.get(m, 0)) for m in _MONTHS
    ]

    # ── 統計切片 ─────────────────────────────────────────────────────────
    classroom_map = get_classroom_count_by_month(session, year)
    insured_map = get_insured_employee_count_by_month(session, year)
    classroom_monthly = [int(classroom_map.get(m, 0)) for m in _MONTHS]
    insured_monthly = [int(insured_map.get(m, 0)) for m in _MONTHS]

    # ── 人事支出切片 ─────────────────────────────────────────────────────
    salary_breakdown = get_salary_breakdown_by_month(session, year)

    def _salary_field(field: str) -> list[int]:
        return [int(salary_breakdown.get(m, {}).get(field, 0)) for m in _MONTHS]

    gross_monthly = _salary_field("gross_salary")
    festival_monthly = _salary_field("festival_bonus")
    overtime_bonus_monthly = _salary_field("overtime_bonus")
    overtime_pay_monthly = _salary_field("overtime_pay")
    supervisor_dividend_monthly = _salary_field("supervisor_dividend")
    labor_ins_monthly = _salary_field("labor_insurance_employer")
    health_ins_monthly = _salary_field("health_insurance_employer")
    pension_monthly = _salary_field("pension_employer")

    # 公式詳見模組 docstring：gross 已含 overtime_pay 與 supervisor_dividend
    base_salary_monthly = _diff_lists(
        _diff_lists(gross_monthly, overtime_pay_monthly),
        supervisor_dividend_monthly,
    )

    personnel_subtotal_monthly = _sum_lists(
        [
            base_salary_monthly,
            festival_monthly,
            overtime_bonus_monthly,
            overtime_pay_monthly,
            supervisor_dividend_monthly,
            labor_ins_monthly,
            health_ins_monthly,
            pension_monthly,
        ]
    )

    # ── 變動支出 ─────────────────────────────────────────────────────────
    vendor_map = get_vendor_payment_expense_by_month(session, year)
    vendor_monthly = [int(vendor_map.get(m, 0)) for m in _MONTHS]
    variable_subtotal_monthly = list(vendor_monthly)  # 目前只一列

    # ── totals ───────────────────────────────────────────────────────────
    expense_total_monthly = _sum_lists(
        [personnel_subtotal_monthly, variable_subtotal_monthly]
    )
    income_total_monthly = list(income_subtotal_monthly)
    refund_total_monthly = list(refund_monthly)
    net_cashflow_monthly = [
        income_total_monthly[i] - refund_total_monthly[i] - expense_total_monthly[i]
        for i in range(12)
    ]

    # ── 組裝 sections ─────────────────────────────────────────────────────
    sections = [
        {
            "key": "stats",
            "label": "統計指標",
            "rows": [
                _row(
                    "classroom_count",
                    "班級數",
                    "class",
                    classroom_monthly,
                    include_total=False,
                ),
                _row(
                    "insured_employee_count",
                    "教職員投保人數",
                    "person",
                    insured_monthly,
                    include_total=False,
                ),
            ],
        },
        {
            "key": "income",
            "label": "收入",
            "rows": [
                _row("income_cash", "現金繳費", "amount", cash_monthly),
                _row("income_transfer", "轉帳繳費", "amount", transfer_monthly),
                _row(
                    "income_other_method",
                    "其他繳費方式",
                    "amount",
                    other_method_monthly,
                ),
                _row(
                    "income_registration",
                    "新生註冊費",
                    "amount",
                    registration_monthly,
                ),
                _row("income_material", "耗材費", "amount", material_monthly),
                _row(
                    "income_monthly_tuition",
                    "月費／學費／雜費",
                    "amount",
                    monthly_tuition_monthly,
                ),
                _row("income_activity", "課後才藝", "amount", activity_monthly),
                _row(
                    "income_subtotal",
                    "收入合計（毛收入）",
                    "amount",
                    income_subtotal_monthly,
                    is_subtotal=True,
                ),
                _row(
                    "income_refund",
                    "退款（含學費／才藝退費）",
                    "amount",
                    refund_monthly,
                ),
            ],
        },
        {
            "key": "personnel_expense",
            "label": "人事支出",
            "rows": [
                _row(
                    "personnel_base_salary",
                    "薪資（基本，已扣節慶／超額／其他獎金）",
                    "amount",
                    base_salary_monthly,
                ),
                _row(
                    "personnel_festival_bonus",
                    "節慶獎金",
                    "amount",
                    festival_monthly,
                ),
                _row(
                    "personnel_overtime_bonus",
                    "超額獎金",
                    "amount",
                    overtime_bonus_monthly,
                ),
                _row(
                    "personnel_overtime_pay",
                    "加班費",
                    "amount",
                    overtime_pay_monthly,
                ),
                _row(
                    "personnel_other_bonus",
                    "其他獎金（主管紅利）",
                    "amount",
                    supervisor_dividend_monthly,
                ),
                _row(
                    "personnel_labor_insurance",
                    "勞保（雇主負擔）",
                    "amount",
                    labor_ins_monthly,
                ),
                _row(
                    "personnel_health_insurance",
                    "健保（雇主負擔）",
                    "amount",
                    health_ins_monthly,
                ),
                _row(
                    "personnel_pension",
                    "勞退（雇主提撥）",
                    "amount",
                    pension_monthly,
                ),
                _row(
                    "personnel_subtotal",
                    "人事小計",
                    "amount",
                    personnel_subtotal_monthly,
                    is_subtotal=True,
                ),
            ],
        },
        {
            "key": "variable_expense",
            "label": "變動支出",
            "rows": [
                _row(
                    "variable_vendor",
                    "廠商付款（含租金/零用金/餐點等需登錄之項目）",
                    "amount",
                    vendor_monthly,
                ),
                _row(
                    "variable_subtotal",
                    "變動支出小計",
                    "amount",
                    variable_subtotal_monthly,
                    is_subtotal=True,
                ),
            ],
        },
    ]

    totals = {
        "income_total": {
            "monthly": income_total_monthly,
            "total": sum(income_total_monthly),
        },
        "refund_total": {
            "monthly": refund_total_monthly,
            "total": sum(refund_total_monthly),
        },
        "expense_total": {
            "monthly": expense_total_monthly,
            "total": sum(expense_total_monthly),
        },
        "net_cashflow": {
            "monthly": net_cashflow_monthly,
            "total": sum(net_cashflow_monthly),
        },
    }

    return {
        "year": year,
        "sections": sections,
        "totals": totals,
        "pending_items": list(_PENDING_ITEMS),
    }
