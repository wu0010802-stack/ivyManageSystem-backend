"""schemas/dsr.py — admin DSR queue response/request models（P2-3）。

個資法資料主體權利請求（DSR）admin 決議端點的 Pydantic 契約。
"""

from __future__ import annotations

from pydantic import BaseModel


class DsrRequestAdminOut(BaseModel):
    """admin queue 單筆 DSR 請求輸出（datetime 欄位序列化為 isoformat str）。"""

    id: int
    user_id: int
    request_type: str
    status: str
    subject_entity_type: str | None = None
    subject_entity_id: int | None = None
    scope: str | None = None
    field_name: str | None = None
    new_value: str | None = None
    reason: str | None = None
    submitted_at: str
    decided_at: str | None = None
    decided_by: int | None = None
    decision_note: str | None = None


class DsrDecisionIn(BaseModel):
    """admin approve / reject 的決議說明。"""

    decision_note: str
