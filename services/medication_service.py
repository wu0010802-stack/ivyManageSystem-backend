"""services/medication_service.py — 用藥單核心邏輯（staff/parent 共用）

把 `api/student_health.py` 原本內嵌於 endpoint 的 create-and-fan-out 流程抽出，
讓 staff 端（teacher/supervisor）與 parent 端（家長家用 portal）共用同一份
寫入邏輯，避免雙寫漂移。

只負責「寫」與「讀邏輯」；權限判定、IDOR、audit 由 endpoint 端自行處理。
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from models.database import (
    StudentAllergy,
    StudentMedicationLog,
    StudentMedicationOrder,
)


def create_order_with_logs(
    session,
    *,
    student_id: int,
    order_date: date,
    medication_name: str,
    dose: str,
    time_slots: Iterable[str],
    note: Optional[str],
    created_by: Optional[int],
    source: str,
) -> StudentMedicationOrder:
    """建立用藥單並依 time_slots 預建 N 筆 pending logs。

    呼叫端應已開啟 transaction（session_scope / get_session）。本函式只 add+flush
    + refresh，不 commit。

    Args:
        source: 'teacher' 或 'parent'（見 models.portfolio.MEDICATION_SOURCES）
    """
    slots = list(time_slots)
    order = StudentMedicationOrder(
        student_id=student_id,
        order_date=order_date,
        medication_name=medication_name,
        dose=dose,
        time_slots=slots,
        note=note,
        created_by=created_by,
        source=source,
    )
    session.add(order)
    session.flush()

    for slot in slots:
        session.add(
            StudentMedicationLog(
                order_id=order.id,
                scheduled_time=slot,
            )
        )
    session.flush()
    session.refresh(order)
    return order


def find_allergy_conflicts(
    session, *, student_id: int, medication_name: str
) -> list[StudentAllergy]:
    """找出 medication_name 字面與 student 已知過敏原相關的 active allergies。

    回傳結構讓 endpoint 端可組「ALLERGY_WARNING」軟警告 detail。
    比對採子字串包含，雙向皆算（藥名含 allergen，或 allergen 含藥名片段）；
    大小寫不敏感。
    """
    if not medication_name:
        return []
    name_lower = medication_name.strip().lower()
    if not name_lower:
        return []

    rows = (
        session.query(StudentAllergy)
        .filter(
            StudentAllergy.student_id == student_id,
            StudentAllergy.active.is_(True),
        )
        .all()
    )
    hits: list[StudentAllergy] = []
    for a in rows:
        allergen_lower = (a.allergen or "").strip().lower()
        if not allergen_lower:
            continue
        if allergen_lower in name_lower or name_lower in allergen_lower:
            hits.append(a)
    return hits
