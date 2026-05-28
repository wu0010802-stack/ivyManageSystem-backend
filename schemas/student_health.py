"""學童健康（過敏 + 用藥）router 對應 Out schemas。

涵蓋 api/student_health.py 全 11 endpoint。

PII / 健康資料：本檔多欄位含學童醫療資訊，全 inline `# pii-allow:`
（admin/教師端必看，存取 router 端已 Permission gate）。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from schemas._base import IvyBaseModel

# ── Allergy ──────────────────────────────────────────────────────────────


class AllergyOut(IvyBaseModel):
    """過敏紀錄單筆 (對應 _allergy_to_dict)。"""

    id: int
    student_id: int
    allergen: str  # pii-allow: 過敏原（醫療資訊，教師端必看）
    severity: Optional[str] = None  # pii-allow: 嚴重程度
    reaction_symptom: Optional[str] = None  # pii-allow: 反應症狀
    first_aid_note: Optional[str] = None  # pii-allow: 急救說明
    active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AllergyListOut(IvyBaseModel):
    """GET /students/{id}/allergies 回傳。"""

    items: list[AllergyOut]
    total: int


class AllergyDeleteOut(IvyBaseModel):
    """DELETE /students/{id}/allergies/{alg_id} 回傳。"""

    message: str


# ── Medication ───────────────────────────────────────────────────────────


class MedicationLogOut(IvyBaseModel):
    """單筆用藥執行紀錄 (對應 _log_to_dict)。"""

    id: int
    order_id: int
    scheduled_time: Optional[str] = None
    status: Literal["pending", "administered", "skipped", "correction"]
    administered_at: Optional[str] = None
    administered_by: Optional[int] = None
    skipped: bool
    skipped_reason: Optional[str] = None  # pii-allow: 跳過原因（用藥審計留痕）
    note: Optional[str] = None  # pii-allow: 執行備註
    correction_of: Optional[int] = None
    created_at: Optional[str] = None


class MedicationOrderOut(IvyBaseModel):
    """單份用藥單 (對應 _order_to_dict)。"""

    id: int
    student_id: int
    order_date: str
    medication_name: str  # pii-allow: 藥名
    dose: Optional[str] = None  # pii-allow: 劑量
    time_slots: list[str]
    note: Optional[str] = None  # pii-allow: 給藥備註
    source: Optional[str] = None
    created_by: Optional[int] = None
    logs: list[MedicationLogOut]
    created_at: Optional[str] = None


class MedicationOrderListOut(IvyBaseModel):
    """GET /students/{id}/medication-orders 回傳。"""

    items: list[MedicationOrderOut]
    total: int


class MedicationOrderWithStudentOut(MedicationOrderOut):
    """today-medication 加 student_name + classroom_id 兩欄。"""

    student_name: Optional[str] = None  # pii-allow: 學童姓名
    classroom_id: Optional[int] = None


class TodayMedicationSummaryOut(IvyBaseModel):
    """GET /portfolio/today-medication 回傳。"""

    date: str
    pending: int
    administered: int
    skipped: int
    orders: list[MedicationOrderWithStudentOut]
