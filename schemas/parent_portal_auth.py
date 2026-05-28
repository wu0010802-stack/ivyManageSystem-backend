"""家長端 LIFF 認證 (parent_portal/auth.py) 對應 Out schemas。

Phase 1b 範圍（本檔）：
- BindFirstChildOut / BindAdditionalChildOut / ParentRefreshOut — 三個成功路徑簡單 shape

Out of scope（Phase 1c）：
- POST /liff-login（polymorphic：need_binding | ok | invalid_token 多種 status）
- POST /logout 已是 Response(204) 無 body，不需 response_model
"""

from __future__ import annotations

from typing import Literal, Optional

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


class BindAdditionalChildOut(IvyBaseModel):
    """POST /bind-additional 綁定多個小孩成功回傳。"""

    status: Literal["ok"]
    guardian_id: int  # pii-allow: FK 引用 (非個人 PII)
    student_id: int


class ParentRefreshOut(IvyBaseModel):
    """POST /refresh access+refresh token rotation 成功回傳。"""

    status: Literal["ok"]
    user: ParentUserInfo
