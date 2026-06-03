"""term_bounds 純函式：(學年, 學期) → 固定起訖日。"""

from datetime import date

import pytest

from utils.academic import term_bounds, _resolve_by_date


def test_first_semester_bounds():
    # 114 學年上學期：2025/8/1 ~ 2026/1/31
    assert term_bounds(114, 1) == (date(2025, 8, 1), date(2026, 1, 31))


def test_second_semester_bounds():
    # 114 學年下學期：2026/2/1 ~ 2026/7/31
    assert term_bounds(114, 2) == (date(2026, 2, 1), date(2026, 7, 31))


def test_invalid_semester_raises():
    with pytest.raises(ValueError):
        term_bounds(114, 3)


@pytest.mark.parametrize(
    "d",
    [
        date(2025, 8, 1),
        date(2025, 12, 31),
        date(2026, 1, 31),
        date(2026, 2, 1),
        date(2026, 7, 31),
    ],
)
def test_round_trip_resolve_matches_bounds(d):
    """_resolve_by_date(d) 算出的學期，其 term_bounds 必含 d。"""
    sy, sem = _resolve_by_date(d)
    start, end = term_bounds(sy, sem)
    assert start <= d <= end
