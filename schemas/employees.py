"""Employees router 對應 Pydantic Out schemas。

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
