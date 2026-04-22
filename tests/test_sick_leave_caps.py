"""病假住院/未住院雙配額（勞工請假規則第 4 條）

- 未住院：年累計不得超過 30 天（240h）
- 住院：年累計不得超過 1 年（2080h）
- 兩者合計不得超過 1 年（2080h）
"""

import pytest
from fastapi import HTTPException

from api.leaves_quota import assert_sick_leave_within_statutory_caps


class TestSickLeaveCaps:
    def test_outpatient_under_cap_passes(self):
        assert_sick_leave_within_statutory_caps(
            outpatient_used_hours=100,
            hospitalized_used_hours=0,
            new_hours=50,
            is_hospitalized=False,
        )

    def test_outpatient_at_cap_passes(self):
        assert_sick_leave_within_statutory_caps(
            outpatient_used_hours=200,
            hospitalized_used_hours=0,
            new_hours=40,
            is_hospitalized=False,  # total = 240
        )

    def test_outpatient_over_cap_raises(self):
        with pytest.raises(HTTPException) as exc:
            assert_sick_leave_within_statutory_caps(
                outpatient_used_hours=240,
                hospitalized_used_hours=0,
                new_hours=1,
                is_hospitalized=False,
            )
        assert exc.value.status_code == 400
        assert "30" in exc.value.detail or "240" in exc.value.detail

    def test_hospitalized_under_cap_passes(self):
        assert_sick_leave_within_statutory_caps(
            outpatient_used_hours=0,
            hospitalized_used_hours=500,
            new_hours=200,
            is_hospitalized=True,
        )

    def test_hospitalized_can_exceed_outpatient_cap(self):
        """住院假可超過 240h（例如 500h），不受未住院 30 天限制"""
        assert_sick_leave_within_statutory_caps(
            outpatient_used_hours=0,
            hospitalized_used_hours=0,
            new_hours=500,
            is_hospitalized=True,
        )

    def test_outpatient_full_then_hospitalized_passes(self):
        """未住院 240h（已滿）+ 新 100h 住院 → ok（合計 340 ≤ 2080）"""
        assert_sick_leave_within_statutory_caps(
            outpatient_used_hours=240,
            hospitalized_used_hours=0,
            new_hours=100,
            is_hospitalized=True,
        )

    def test_total_combined_cap_raises(self):
        """住院已用 2000h + 100h 未住院 → 合計 2100 > 2080 → 違反合計上限"""
        with pytest.raises(HTTPException) as exc:
            assert_sick_leave_within_statutory_caps(
                outpatient_used_hours=0,
                hospitalized_used_hours=2000,
                new_hours=100,
                is_hospitalized=False,
            )
        assert exc.value.status_code == 400
        assert "2080" in exc.value.detail or "1 年" in exc.value.detail

    def test_hospitalized_at_full_year_cap_raises(self):
        with pytest.raises(HTTPException):
            assert_sick_leave_within_statutory_caps(
                outpatient_used_hours=0,
                hospitalized_used_hours=2080,
                new_hours=1,
                is_hospitalized=True,
            )
