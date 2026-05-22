"""純函式單元測試：academic_year mapping + period_label mapping。"""

import pytest

from models.year_end import SpecialBonusType
from services.year_end.appraisal_sync import (
    civil_year_to_target_academic_year,
    map_bonus_type_to_period_label,
)


@pytest.mark.parametrize(
    "civil_year,expected_academic_year",
    [
        (2024, 112),
        (2025, 113),
        (2026, 114),
        (2027, 115),
        (2028, 116),
    ],
)
def test_civil_year_to_target_academic_year(civil_year, expected_academic_year):
    """payout 發放國曆年 N → 對應本學年 (N - 1911 - 1)。"""
    assert civil_year_to_target_academic_year(civil_year) == expected_academic_year


def test_map_bonus_type_to_period_label_first_is_earlier():
    """FIRST = 較早 = 前一學年下學期 → label 'N-1下'"""
    assert (
        map_bonus_type_to_period_label(
            SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
            target_academic_year=114,
        )
        == "113下"
    )


def test_map_bonus_type_to_period_label_second_is_later():
    """SECOND = 較晚 = 本學年上學期 → label 'N上'"""
    assert (
        map_bonus_type_to_period_label(
            SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
            target_academic_year=114,
        )
        == "114上"
    )


def test_map_bonus_type_to_period_label_rejects_non_appraisal_type():
    with pytest.raises(ValueError):
        map_bonus_type_to_period_label(
            SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
            target_academic_year=114,
        )
