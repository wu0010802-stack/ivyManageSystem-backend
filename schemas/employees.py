"""Employees router 對應 Pydantic Out schemas。

包含：
- EmployeeOut — list / detail 共用 employee shape
- MutationResultOut — POST/PUT/DELETE 共用回傳（message + id）
- ProbationAlertItem / ProbationAlertResponseOut — 試用期警示
- TeacherOut — GET /teachers list
- OffboardResultOut — POST /{id}/offboard 向後相容 shape



涵蓋 admin GET /employees 與 GET /employees/{id} 共用的 _format_employee_response
dict shape；conditional masking 欄位用 Optional 接 None（router 端 per-user 決定遮罩，
schema 只描述形狀）。

EmployeeOut.resign_date / resign_reason 為 detail-only 欄位（list 端 resign_fields=False
時不會出現）；用 Optional 表達可缺，serialize 時若 caller 沒填即為 None。

PII 欄位（id_number / bank_account / phone / address 等）為合法 admin 端需求；
新增 PII 欄位前必須在此檔加 `# pii-allow: <reason>` inline comment，由
scripts/check_pii_in_schemas.py 接受 exempt。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class EmployeeOut(IvyBaseModel):
    """員工列表 / 詳情共用 response 形狀。

    遮罩語意：
    - id_number / bank_account_name / bank_code：當 caller 無 SALARY_WRITE，
      router 會傳 masked str 或 None
    - base_salary / hourly_rate / insurance_salary_level / pension_self_rate：
      當 caller 無 admin/hr 也非 self，router 會傳 None
    """

    id: int
    employee_id: str
    name: str
    id_number: Optional[str] = (
        None  # pii-allow: 員工身分證號（admin 端必看，遮罩在 router 層）
    )
    employee_type: str
    title: Optional[str] = None
    job_title_id: Optional[int] = None
    position: Optional[str] = None
    supervisor_role: Optional[str] = None
    bonus_grade: Optional[str] = None
    classroom_id: Optional[int] = None
    classroom_name: Optional[str] = None

    # 薪資金額（per-user gate）
    base_salary: Optional[float] = (
        None  # pii-allow: 薪資金額（admin/hr/self gate 在 router）
    )
    hourly_rate: Optional[float] = None  # pii-allow: 時薪
    insurance_salary_level: Optional[float] = None  # pii-allow: 投保薪資級距
    pension_self_rate: Optional[float] = None  # pii-allow: 勞退自提

    # 銀行帳戶（per-user gate）
    bank_code: Optional[str] = None  # pii-allow: 員工撥款銀行代碼
    bank_account: Optional[str] = None  # pii-allow: 員工撥款帳號
    bank_account_name: Optional[str] = None  # pii-allow: 員工撥款戶名

    # 排班
    work_start_time: Optional[str] = None
    work_end_time: Optional[str] = None

    # 在職狀態
    is_active: bool
    hire_date: Optional[str] = None
    probation_end_date: Optional[str] = None
    birthday: Optional[str] = None  # pii-allow: 員工生日（admin 看，計算試用期/年資）

    # 聯絡資訊
    phone: Optional[str] = None  # pii-allow: 員工聯絡電話
    address: Optional[str] = None  # pii-allow: 員工居住地址
    emergency_contact_name: Optional[str] = None  # pii-allow: 緊急聯絡人
    emergency_contact_phone: Optional[str] = None  # pii-allow: 緊急聯絡人電話
    dependents: Optional[int] = None  # pii-allow: 扶養親屬數量（影響稅務/保險計算）

    # 特殊狀態旗標
    no_employment_insurance: bool = False
    health_exempt: bool = False  # pii-allow: 健保特殊狀態旗標（非醫療資訊）
    skip_payroll_bonuses: bool = False
    skip_payroll_transfer: bool = False
    unreported_for_tax: bool = False
    extra_dependents_quarterly: int = 0  # pii-allow: 季度扶養變動（保險級距計算）
    bypass_standard_base: bool = False
    insurance_salary_override_reason: Optional[str] = (
        None  # pii-allow: 投保覆寫原因（非個人 PII）
    )

    # detail-only / resign_fields=True 才出現
    resign_date: Optional[str] = None
    resign_reason: Optional[str] = None  # pii-allow: 離職原因（admin 端看）


from schemas._common import (
    MutationResultOut,
)  # noqa: E402,F401 — backward-compat re-export


class EmployeeCreateResultOut(IvyBaseModel):
    """POST /employees 建立員工成功回傳 — 包含自動配發的工號。"""

    message: str
    id: int
    employee_id: str


class ProbationAlertItem(IvyBaseModel):
    """試用期警示單筆員工。"""

    id: int
    name: str
    employee_id: str
    probation_end_date: str
    days_remaining: int


class _ProbationAlertCounts(IvyBaseModel):
    next_month: int


class ProbationAlertResponseOut(IvyBaseModel):
    """GET /employees/probation-alerts 回傳。"""

    employees: list[ProbationAlertItem]
    alerts: _ProbationAlertCounts


class TeacherOut(IvyBaseModel):
    """GET /teachers list 內單筆。"""

    id: int
    employee_id: str
    name: str
    title: Optional[str] = None


class OffboardResultOut(IvyBaseModel):
    """POST /employees/{id}/offboard 向後相容 shape（deprecated endpoint）。"""

    message: str
    id: int
    name: str
    resign_date: str
    resign_reason: Optional[str] = None  # pii-allow: 離職原因（admin 端看）
    is_active: bool
    user_account_revoked: bool


class FinalSalaryPreviewOut(IvyBaseModel):
    """GET /employees/{id}/final-salary-preview 離職薪資預覽（含未休特休折算）。

    口徑：薪資引擎 preview_salary_calculation（不寫 DB）+ 月中離職折算 note
    + 勞基法 §38(4) 未休特休工資。SALARY_READ + self-or-FULL_SALARY_ROLES。
    """

    year: int
    month: int
    contracted_base_salary: float  # pii-allow: 員工薪資（admin/hr/self gate 在 router）
    base_salary: float  # pii-allow: 折算後底薪
    proration_note: Optional[str] = None
    festival_bonus: float  # pii-allow: 節金（含在 gross_salary）
    gross_salary: float  # pii-allow: 應發
    total_deduction: float  # pii-allow: 扣項合計
    labor_insurance: float  # pii-allow: 勞保自付
    health_insurance: float  # pii-allow: 健保自付
    pension: float  # pii-allow: 自提退休金
    net_salary: float  # pii-allow: 實發
    unused_annual_leave_hours: float
    unused_annual_leave_compensation: float  # pii-allow: 未休特休折算工資
    net_salary_with_unused_annual: float  # pii-allow: 實發 + 未休特休
