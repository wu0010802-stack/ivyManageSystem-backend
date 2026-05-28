"""教師端（portal）students router 對應 Out schemas。

Phase 1b 範圍（本檔）：
- StudentMeasurementSnapshotItem / StudentMeasurementSnapshotOut — GET /students/measurements-latest
- RevealPhoneOut — POST /students/{id}/reveal-phone

Out of scope（Phase 1c）：
- GET /my-students（巢狀 classrooms→students 含 14+ field/student，複雜 nested）
- GET /students/{id}/detail（含 guardians/allergies/medications/attendance/incidents/observations/
  assessments/contact_book/transfer_history 約 10 個 nested model 共 200+ 行 schema）
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class LastMeasurement(IvyBaseModel):
    """單筆量測快照；string 值對齊 router 內 `str(Decimal)` 序列化."""

    measured_on: str
    height_cm: Optional[str] = None  # pii-allow: 學童身高（健康資料，admin/teacher 看）
    weight_kg: Optional[str] = None  # pii-allow: 學童體重
    head_circumference_cm: Optional[str] = None
    vision_left: Optional[str] = None
    vision_right: Optional[str] = None


class StudentMeasurementSnapshotItem(IvyBaseModel):
    """GET /students/measurements-latest list 單筆。"""

    student_id: int
    name: str  # pii-allow: 學童姓名（教師端必看）
    classroom_id: Optional[int] = None
    last_measurement: Optional[LastMeasurement] = None


class RevealPhoneOut(IvyBaseModel):
    """POST /students/{id}/reveal-phone 揭露電話。"""

    target: str  # "parent" / "emergency" / "guardian"
    guardian_id: Optional[int] = None  # pii-allow: FK 引用 (非個人 PII)
    phone: str  # pii-allow: 揭露的完整電話（高敏感事件已 audit）
