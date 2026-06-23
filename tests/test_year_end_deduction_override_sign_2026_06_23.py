"""年終扣項 override 須夾為 ≤ 0，避免誤填正值由扣項翻成加項致多發（縱深防禦）。

qa-loop #9（2026-06-23）：_deductions_from_settlement 的 _resolve 在 calc_meta 存在
deduction_*_override 時直接回 Decimal(str(override)) 覆蓋 B5 自動扣款。engine
compute_deduction_total 對 6 欄直接 Σ、compute_payable_amount = subtotal + deduction_total
（扣項約定為負或 0）。若 override 誤填正值（'1000' 而非 '-1000'），該欄由扣項翻成加項 →
payable/total_amount 變大多發；原無 clamp/符號守衛。目前無 endpoint 設這些 key（純防禦/
未來路徑），補符號夾制防呆。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from services.year_end.settlement_builder import _deductions_from_settlement


def _auto(meeting=0, personal_leave=0, sick_leave=0, late=0):
    return SimpleNamespace(
        meeting=Decimal(str(meeting)),
        personal_leave=Decimal(str(personal_leave)),
        sick_leave=Decimal(str(sick_leave)),
        late=Decimal(str(late)),
    )


def _existing(calc_meta):
    return SimpleNamespace(
        calc_meta=calc_meta,
        deduction_leave_late=Decimal("0"),
        deduction_disciplinary=Decimal("0"),
    )


def test_positive_override_clamped_to_zero():
    """override 誤填正值 → 夾為 0（扣項不可變加項，否則 payable 灌大多發）。"""
    auto = _auto(meeting=-500)
    existing = _existing({"deduction_meeting_override": "1000"})
    bd = _deductions_from_settlement(None, None, None, existing, auto=auto)
    assert bd.meeting == Decimal(
        "0"
    ), f"正值 override(1000) 應夾為 0（扣項 ≤ 0），實得 {bd.meeting}（>0 → 由扣項翻加項多發）"


def test_negative_override_preserved():
    """正常負值 override 維持原值（不誤殺合法手填扣款）。"""
    auto = _auto(late=-100)
    existing = _existing({"deduction_late_override": "-900"})
    bd = _deductions_from_settlement(None, None, None, existing, auto=auto)
    assert bd.late_early == Decimal("-900")


def test_zero_override_respected():
    """override='0' 仍被尊重（is not None 判定），結果 0。"""
    auto = _auto(sick_leave=-300)
    existing = _existing({"deduction_sick_leave_override": "0"})
    bd = _deductions_from_settlement(None, None, None, existing, auto=auto)
    assert bd.sick_leave == Decimal("0")


def test_no_override_uses_auto_value():
    """無 override 時用 B5 auto 值（負值），不受夾制影響。"""
    auto = _auto(personal_leave=-250)
    existing = _existing({})
    bd = _deductions_from_settlement(None, None, None, existing, auto=auto)
    assert bd.personal_leave == Decimal("-250")
