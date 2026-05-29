"""api/salary/records.py 對應 Pydantic Out schemas（Phase 3.5）。

涵蓋 3 endpoint：
- GET /salaries/records      → list[SalaryRecordItemOut]
- GET /salaries/history      → list[SalaryHistoryItemOut]
- GET /salaries/history-all  → SalaryHistoryAllOut（分頁 + 依員工分組）

Defer（永久豁免段）：
- GET /salaries/export-all   → StreamingResponse (xlsx / pdf 二進位)

命名 prefix 用 `SalaryRecord` 但 list 單筆用 `SalaryRecordItemOut`
避免與 `models.SalaryRecord` ORM 同名衝突。

PII：員工姓名 / 薪資金額（base_salary/gross_salary/net_salary/各種獎金扣項）
為 admin/hr/self 必看欄位，router 層已透過 `_resolve_salary_viewer_employee_id` /
`_enforce_self_or_full_salary` 做 per-user gate；schema 只描述形狀，逐欄標
`# pii-allow:` 與 Sentry denylist exempt 對齊。

field 設計重點：
- 重複欄位（total_deduction/total_deductions、net_salary/net_pay、
  pension/pension_self）為前端歷史相容必須兩個 key 同時出現，建模時各自
  列為獨立欄位，不用 pydantic alias（會少回一個 key 即前端可見破壞性變更）。
- breakdown: Optional[dict[str, Any]] = None
  - shape 由 services/salary/breakdown_enrollment.compute_enrollment_breakdown
    決定，不強型別化以免綁死 service 內部演算法迭代。
- 金額在 router 端用 `or 0` 預先 coalesce，schema 用非 Optional float
  即可（pydantic validate dict literal 而非 ORM）。
- datetime 已在 router 端 `.isoformat()` 為字串，schema 用 Optional[str]。
"""

from __future__ import annotations

from typing import Any, Optional

from schemas._base import IvyBaseModel


class SalaryRecordItemOut(IvyBaseModel):
    """GET /salaries/records 單筆。

    對應 `records.py:get_salary_records` 迴圈內組裝的 dict shape。
    """

    id: int
    version: int
    employee_id: int
    employee_code: str
    employee_name: str  # pii-allow: 員工姓名（admin/hr/self gate 在 router）
    job_title: str

    # 薪資金額（每一項都是 PII — admin/hr/self gate 在 router）
    base_salary: float  # pii-allow: 底薪
    festival_bonus: float  # pii-allow: 節金
    overtime_bonus: float  # pii-allow: 加班獎金
    overtime_pay: float  # pii-allow: 加班費
    meeting_overtime_pay: float  # pii-allow: 會議加班費
    meeting_absence_deduction: float  # pii-allow: 會議缺席扣款
    birthday_bonus: float  # pii-allow: 生日福利金
    performance_bonus: float  # pii-allow: 績效獎金
    special_bonus: float  # pii-allow: 特別獎金
    supervisor_dividend: float  # pii-allow: 主管分紅
    labor_insurance: float  # pii-allow: 勞保自付
    health_insurance: float  # pii-allow: 健保自付
    supplementary_health_employee: float  # pii-allow: 二代健保補充保費自付
    pension: float  # pii-allow: 自提退休金
    late_deduction: float  # pii-allow: 遲到扣款
    early_leave_deduction: float  # pii-allow: 早退扣款
    missing_punch_deduction: float  # pii-allow: 漏打卡扣款
    absence_deduction: float  # pii-allow: 曠職扣款
    attendance_deduction: float  # pii-allow: 出勤扣款合計
    leave_deduction: float  # pii-allow: 請假扣款
    other_deduction: float  # pii-allow: 其他扣款
    gross_salary: float  # pii-allow: 應發
    total_deduction: float  # pii-allow: 扣項合計
    net_salary: float  # pii-allow: 實發

    # 封存 / 稽核
    is_finalized: bool
    finalized_at: Optional[str] = None
    finalized_by: Optional[int] = None
    remark: Optional[str] = None
    calculated_at: Optional[str] = None
    manual_overrides: list[str] = []

    # 前端 salaryResults 重建欄位別名（與上方薪資金額重複；不能 alias 合併）
    pension_self: float  # pii-allow: 自提退休金（前端 alias）
    total_deductions: float  # pii-allow: 扣項合計（前端 alias）
    net_pay: float  # pii-allow: 實發（前端 alias）

    # 學生人數展開（即時算出，未隨薪資封存）
    breakdown: Optional[dict[str, Any]] = None
    breakdown_stale: bool


class SalaryHistoryItemOut(IvyBaseModel):
    """GET /salaries/history 單筆（單員工 N 月歷史）。

    對應 `records.py:get_salary_history` 迴圈內組裝的 dict shape。
    """

    id: int
    year: int
    month: int
    base_salary: float  # pii-allow: 底薪
    total_bonus: float  # pii-allow: 獎金合計
    labor_insurance: float  # pii-allow: 勞保自付
    health_insurance: float  # pii-allow: 健保自付
    supplementary_health_employee: float  # pii-allow: 二代健保補充保費自付
    attendance_deduction: float  # pii-allow: 出勤扣款合計
    leave_deduction: float  # pii-allow: 請假扣款
    gross_salary: float  # pii-allow: 應發
    total_deduction: float  # pii-allow: 扣項合計
    total_deductions: float  # pii-allow: 扣項合計（前端 alias）
    net_salary: float  # pii-allow: 實發
    net_pay: float  # pii-allow: 實發（前端 alias）


class SalaryHistoryAllMonthOut(IvyBaseModel):
    """GET /salaries/history-all 內每月份單筆摘要（被 SalaryHistoryAllEmployeeOut.months 內嵌）。"""

    month: int
    net_salary: float  # pii-allow: 實發
    gross_salary: float  # pii-allow: 應發


class SalaryHistoryAllEmployeeOut(IvyBaseModel):
    """GET /salaries/history-all 內單筆員工（被 SalaryHistoryAllOut.items 內嵌）。"""

    employee_id: int
    employee_name: str  # pii-allow: 員工姓名（admin/hr gate 在 router）
    months: list[SalaryHistoryAllMonthOut]


class SalaryHistoryAllOut(IvyBaseModel):
    """GET /salaries/history-all 回傳 wrapper（分頁 + 依員工分組）。"""

    items: list[SalaryHistoryAllEmployeeOut]
    total: int
    skip: int
    limit: int
