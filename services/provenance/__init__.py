"""provenance 服務層：自動推導值的 DerivedValue + 逐筆來源。"""

from services.provenance.attendance_provider import derive_attendance_provenance

__all__ = ["derive_attendance_provenance"]
