"""家長端 LIFF 認證 (parent_portal/auth.py) 對應 Out schemas。

範圍：
- BindFirstChildOut / BindAdditionalChildOut / ParentRefreshOut — 三個成功路徑簡單 shape
- LiffLoginOkOut / LiffLoginNeedBindingOut — POST /liff-login 雙分支 discriminated union
  （ok = 既綁定家長；need_binding = 未綁定，回 line_user_id+name_hint 引導 bind 流程）

Out of scope：
- POST /logout 已是 Response(204) 無 body，不需 response_model
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import Field

from schemas._base import IvyBaseModel


class ParentUserInfo(IvyBaseModel):
    """家長使用者基本資訊（bind/refresh 共用 user 欄位）。"""

    user_id: int
    name: str  # pii-allow: 家長顯示名（含 LINE display_name 或 guardian.name）
    role: str  # 固定 "parent"


class BindFirstChildOut(IvyBaseModel):
    """POST /bind 首次綁定成功回傳。"""

    status: Literal["ok"]
    user: ParentUserInfo


class DeviceSetupOut(IvyBaseModel):
    """POST /auth/device-setup 兌換設定碼成功回傳（無 LINE 家長裝置登入）。"""

    status: Literal["ok"]
    user: ParentUserInfo


class BindAdditionalChildOut(IvyBaseModel):
    """POST /bind-additional 綁定多個小孩成功回傳。"""

    status: Literal["ok"]
    guardian_id: int  # pii-allow: FK 引用 (非個人 PII)
    student_id: int


class ParentRefreshOut(IvyBaseModel):
    """POST /refresh access+refresh token rotation 成功回傳。"""

    status: Literal["ok"]
    user: ParentUserInfo


class LiffLoginOkOut(IvyBaseModel):
    """POST /liff-login 既綁定家長分支：直接核發 access+refresh token。"""

    status: Literal["ok"]
    user: ParentUserInfo


class LiffLoginNeedBindingOut(IvyBaseModel):
    """POST /liff-login 未綁定分支：核發 bind 暫時 token，前端引導去 /bind。"""

    status: Literal["need_binding"]
    line_user_id: str  # pii-allow: LINE 用戶 ID，bind 流程必須
    name_hint: Optional[str] = None  # pii-allow: LINE 顯示名 hint


# Discriminated union by `status` 欄位（Pydantic v2 標準 polymorphic 寫法）。
LiffLoginOut = Annotated[
    Union[LiffLoginOkOut, LiffLoginNeedBindingOut],
    Field(discriminator="status"),
]
