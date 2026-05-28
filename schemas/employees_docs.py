"""Employee ancillary docs (api/employees_docs.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- EducationOut    — _edu_to_dict shape
- CertificateOut  — _cert_to_dict shape
- ContractOut     — _contract_to_dict shape (salary_at_contract 可被 router masking)

list endpoints 回 list[<Type>Out]；create / update 回單筆 <Type>Out（router 直接回
_*_to_dict 結果而非 mutation message，故不用 MutationResultOut）；DELETE 已在 Phase 3
batch 接 DeleteResultOut。

合約金額（salary_at_contract）為薪資敏感欄位，router 端依 can_view_salary_of() 遮罩
為 None；schema 用 Optional[float] 接住。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class EducationOut(IvyBaseModel):
    """單筆員工學歷 (對應 _edu_to_dict)。"""

    id: int
    employee_id: int
    school_name: str  # pii-allow: 員工學歷-就讀學校
    major: Optional[str] = None  # pii-allow: 員工學歷-主修科系
    degree: str
    graduation_date: Optional[str] = None  # pii-allow: 員工學歷-畢業日期
    is_highest: bool
    remark: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CertificateOut(IvyBaseModel):
    """單筆員工證照 (對應 _cert_to_dict)。"""

    id: int
    employee_id: int
    certificate_name: str  # pii-allow: 員工證照名稱
    issuer: Optional[str] = None  # pii-allow: 證照核發單位
    certificate_number: Optional[str] = (
        None  # pii-allow: 證照編號（可能含個人識別資訊）
    )
    issued_date: Optional[str] = None  # pii-allow: 證照核發日期
    expiry_date: Optional[str] = None  # pii-allow: 證照到期日期
    remark: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ContractOut(IvyBaseModel):
    """單筆員工合約 (對應 _contract_to_dict)。

    遮罩語意：salary_at_contract 由 router 端依 can_view_salary_of()
    判斷是否遮罩為 None（與 salary.py 同門檻）。
    """

    id: int
    employee_id: int
    contract_type: str
    start_date: Optional[str] = None  # pii-allow: 合約起始日
    end_date: Optional[str] = None  # pii-allow: 合約結束日
    salary_at_contract: Optional[float] = (
        None  # pii-allow: 合約簽訂月薪（admin/hr/self gate 在 router）
    )
    remark: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
