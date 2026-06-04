"""年終 build loop 考勤扣款 batch wiring（QA 2026-06-04 P1-3）。

build_settlements 原對每位員工呼叫 derive_attendance_deductions → 每員工重取
_latest_active_bonus_config（N+1）。改為 build loop 預算 derive_all_attendance_deductions
（cfg 只取一次）後把 per-employee 結果以 auto= 傳入 _deductions_from_settlement。

本測試鎖定核心機制：_deductions_from_settlement 接受預算 auto 時直接採用、
不再呼叫 per-employee derive（傳 None 的 db/cycle/emp 證明已不依賴 per-emp 重算）。
build loop 的 batch wiring 由既有 year_end 整合測試把關金額不變。
"""

from decimal import Decimal

from services.year_end.auto_derive.attendance_deductions import AttendanceDeductions
from services.year_end import settlement_builder as sb


def test_deductions_from_settlement_uses_precomputed_auto(monkeypatch):
    import services.year_end.auto_derive.attendance_deductions as ad

    def _boom(*a, **k):
        raise AssertionError(
            "已傳 auto 時不應再呼叫 per-employee derive_attendance_deductions"
        )

    monkeypatch.setattr(ad, "derive_attendance_deductions", _boom)

    auto = AttendanceDeductions(
        late=Decimal("-100"),
        personal_leave=Decimal("-200"),
        sick_leave=Decimal("-300"),
        meeting=Decimal("-50"),
        calc_meta={},
    )

    # 傳 auto → 不依賴 db/cycle/emp（per-emp 重算被跳過）；existing=None → 無 override
    result = sb._deductions_from_settlement(None, None, None, None, auto=auto)

    assert result.late_early == Decimal("-100")
    assert result.personal_leave == Decimal("-200")
    assert result.sick_leave == Decimal("-300")
    assert result.meeting == Decimal("-50")
    assert result.leave_late_prev == Decimal("0")
    assert result.disciplinary == Decimal("0")
