"""教師端（portal）profile router 對應 Out schemas。

Phase 3.5 範圍：
- GET /portal/profile → PortalProfileOut（教師自身個資 + 銀行帳號 masked）
- GET /portal/profile/line-binding → PortalProfileLineBindingOut
- PUT /portal/profile/line-binding → PortalProfileLineBindingUpdateOut
- DELETE /portal/profile/line-binding → DeleteResultOut（共用）
- PUT /portal/profile → DeleteResultOut（共用，純 message）

PII 規範：
- 個人資料為「self 可看」(教師看自己) 合法用途；身分證號 / 完整銀行帳號
  router 端走遮罩（_mask_bank_account）寫入 schema 欄位。
- bank_account 雖 router 已 mask，仍為 PII 路徑，需 `# pii-allow:` 標記。
- 緊急聯絡人 / 地址 / 電話為員工自身可見的 PII。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class PortalProfileOut(IvyBaseModel):
    """GET /portal/profile — 教師自身個資彙整。

    bank_account 為遮罩字串（router 端用 _mask_bank_account 處理）。
    """

    employee_id: str  # pii-allow: 員工編號（self 可看）
    name: str  # pii-allow: 員工姓名（self 可看）
    job_title: Optional[str] = None
    position: Optional[str] = None
    classroom: Optional[str] = None
    hire_date: Optional[str] = None
    work_start_time: Optional[str] = None
    work_end_time: Optional[str] = None
    phone: Optional[str] = None  # pii-allow: 員工本人聯絡電話（self 可看）
    address: Optional[str] = None  # pii-allow: 員工本人居住地址（self 可看）
    emergency_contact_name: Optional[str] = (
        None  # pii-allow: 緊急聯絡人姓名（self 可看）
    )
    emergency_contact_phone: Optional[str] = (
        None  # pii-allow: 緊急聯絡人電話（self 可看）
    )
    bank_code: Optional[str] = None  # pii-allow: 員工撥款銀行代碼（self 可看）
    bank_account: Optional[str] = None  # pii-allow: 員工撥款帳號（router 端已 mask）
    bank_account_name: Optional[str] = None  # pii-allow: 員工撥款戶名（self 可看）


class PortalProfileLineBindingOut(IvyBaseModel):
    """GET /portal/profile/line-binding — 目前綁定狀態。"""

    line_user_id: Optional[str] = None  # pii-allow: 員工 LINE userId（self 可看）


class PortalProfileLineBindingUpdateOut(IvyBaseModel):
    """PUT /portal/profile/line-binding — 綁定成功回傳。"""

    message: str
    line_user_id: str  # pii-allow: 員工 LINE userId（self 可看）
