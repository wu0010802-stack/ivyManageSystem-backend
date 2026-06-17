"""考勤匯入：ShiftAssignment 只載入考勤檔涵蓋週（_covered_week_starts）。

原本新格式匯入 `session.query(ShiftAssignment).all()` 全表載入歷年排班（隨年只增），
改為依檔案日期推導涵蓋的「週一」集合過濾。此處測 helper 計算正確（lookup 端
week_monday 用同一公式，故過濾後集合須為其超集）。
"""

from datetime import date

from api.attendance.upload import _covered_week_starts


def test_single_week_collapses_to_one_monday():
    # 6/15 週一、6/17 週三、6/21 週日 同屬一週 → 都歸到 6/15
    assert _covered_week_starts(
        [date(2026, 6, 15), date(2026, 6, 17), date(2026, 6, 21)]
    ) == {date(2026, 6, 15)}


def test_two_weeks_yield_two_mondays():
    assert _covered_week_starts([date(2026, 6, 17), date(2026, 6, 24)]) == {
        date(2026, 6, 15),
        date(2026, 6, 22),
    }


def test_sunday_and_next_monday_are_different_weeks():
    # 週日(6/21)歸到 6/15；隔天週一(6/22)歸到自己 → 跨週邊界正確
    assert _covered_week_starts([date(2026, 6, 21), date(2026, 6, 22)]) == {
        date(2026, 6, 15),
        date(2026, 6, 22),
    }


def test_monday_maps_to_itself():
    assert _covered_week_starts([date(2026, 6, 22)]) == {date(2026, 6, 22)}


def test_empty_input():
    assert _covered_week_starts([]) == set()
