"""api/salary/detail.py 單筆薪資查詢 Out schemas。

Phase 3.5 範圍（本檔）：
- GET /salaries/{id}/audit-log                 → SalaryDetailAuditLogOut
- GET /salaries/{id}/breakdown                 → SalaryDetailBreakdownOut
- GET /salaries/{id}/field-breakdown           → SalaryDetailFieldBreakdownOut
- GET /salaries/{id}/unused-leave-payout-detail → SalaryDetailUnusedLeavePayoutOut

Out of scope（永久豁免段）：
- GET /salaries/{id}/export → StreamingResponse (PDF)

PII 標註原則：薪資 / 獎金 / 健保 / 勞保 / 退休金 / 折算金額為 SALARY_READ 必看欄位，
標 `pii-allow:` 與 Sentry denylist exempt 對齊。員工姓名亦同（薪資頁必看）。
"""

from __future__ import annotations

from typing import Any, Optional

from schemas._base import IvyBaseModel

# ──────────────────────────────────────────────────────────────────────
# 共用 building blocks
# ──────────────────────────────────────────────────────────────────────


class SalaryDetailEmployeeOut(IvyBaseModel):
    """薪資明細 / 欄位明細 共用的員工資訊區塊。"""

    record_id: int
    employee_name: str  # pii-allow: 薪資頁必看員工姓名
    employee_code: str
    job_title: str = ""
    year: int
    month: int


# ──────────────────────────────────────────────────────────────────────
# GET /salaries/{id}/audit-log → SalaryDetailAuditLogOut
# ──────────────────────────────────────────────────────────────────────


class SalaryDetailAuditLogItemOut(IvyBaseModel):
    """單筆稽核記錄。"""

    id: int
    action: str
    username: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[str] = None


class SalaryDetailAuditLogOut(IvyBaseModel):
    """GET /salaries/{id}/audit-log — 薪資操作歷史。"""

    record_id: int
    items: list[SalaryDetailAuditLogItemOut]


# ──────────────────────────────────────────────────────────────────────
# GET /salaries/{id}/breakdown → SalaryDetailBreakdownOut
# ──────────────────────────────────────────────────────────────────────


class SalaryDetailEarningsOut(IvyBaseModel):
    """薪資明細 — 應領區塊。"""

    base_salary: float  # pii-allow: 薪資頁必看底薪
    meeting_overtime_pay: float
    overtime_pay: float
    gross_salary: float  # pii-allow: 薪資頁必看應發總額


class SalaryDetailBonusesOut(IvyBaseModel):
    """薪資明細 — 獎金區塊。"""

    festival_bonus: float
    overtime_bonus: float
    supervisor_dividend: float
    birthday_bonus: float  # pii-allow: 薪資頁必看生日獎金


class SalaryDetailDeductionsOut(IvyBaseModel):
    """薪資明細 — 扣款區塊。"""

    leave_deduction: float
    late_deduction: float
    early_leave_deduction: float
    meeting_absence_deduction: float
    absence_deduction: float
    labor_insurance: float  # pii-allow: 薪資頁必看勞保自付額
    health_insurance: float  # pii-allow: 薪資頁必看健保自付額
    supplementary_health_employee: float  # pii-allow: 薪資頁必看二代健保自付
    pension: float
    total_deduction: float


class SalaryDetailSummaryOut(IvyBaseModel):
    """薪資明細 — 結算區塊。"""

    net_salary: float  # pii-allow: 薪資頁必看淨領
    bonus_separate: bool
    bonus_amount: float  # pii-allow: 薪資頁必看分開扣稅獎金額


class SalaryDetailBreakdownOut(IvyBaseModel):
    """GET /salaries/{id}/breakdown — 單筆薪資明細。"""

    employee: SalaryDetailEmployeeOut
    earnings: SalaryDetailEarningsOut
    bonuses: SalaryDetailBonusesOut
    deductions: SalaryDetailDeductionsOut
    summary: SalaryDetailSummaryOut
    manual_overrides: list[str] = []


# ──────────────────────────────────────────────────────────────────────
# GET /salaries/{id}/field-breakdown → SalaryDetailFieldBreakdownOut
# ──────────────────────────────────────────────────────────────────────


class SalaryDetailFieldColumnOut(IvyBaseModel):
    """欄位明細 — table column header。"""

    key: str
    label: str


class SalaryDetailFieldSummaryOut(IvyBaseModel):
    """欄位明細 — summary 區塊（amount 為該欄位最終金額）。"""

    amount: float  # pii-allow: 薪資欄位金額


class SalaryDetailFieldBreakdownOut(IvyBaseModel):
    """GET /salaries/{id}/field-breakdown — 單欄位明細。

    rows 為動態 shape（依 field 而異：item/value/remark 或 name/matched/...），
    用 dict[str, Any] 接住，前端 a11y 仰賴 columns 描述 row schema。
    """

    title: str
    field: str
    employee: SalaryDetailEmployeeOut
    columns: list[SalaryDetailFieldColumnOut]
    rows: list[dict[str, Any]]
    summary: SalaryDetailFieldSummaryOut
    note: str = ""


# ──────────────────────────────────────────────────────────────────────
# GET /salaries/{id}/unused-leave-payout-detail
# ──────────────────────────────────────────────────────────────────────


class SalaryDetailUnusedLeavePayoutLogOut(IvyBaseModel):
    """未休假折算工資單筆 log。"""

    log_id: int
    source_type: str
    hours: float
    hourly_wage: float  # pii-allow: 薪資頁必看時薪
    amount: float  # pii-allow: 薪資頁必看折算金額
    wage_basis_date: str
    meta: dict[str, Any] = {}


class SalaryDetailUnusedLeavePayoutOut(IvyBaseModel):
    """GET /salaries/{id}/unused-leave-payout-detail — 未休假折算工資明細。"""

    salary_record_id: (
        int  # pii-allow: 薪資記錄 id（非金額；trigger denylist 'salary' substring）
    )
    employee_id: int
    total_amount: float  # pii-allow: 薪資頁必看折算總額
    logs: list[SalaryDetailUnusedLeavePayoutLogOut]
