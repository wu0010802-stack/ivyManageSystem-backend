"""appraisal_service pure-function 測試（grade/bonus 切點、role_group 推薦、預設日期）。"""

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import Grade, RoleGroup, Semester
from services.appraisal_service import (
    classify_grade,
    compute_bonus_amount,
    default_calc_date,
    default_cycle_dates,
    suggest_role_group,
)


@pytest.mark.parametrize(
    "score,expected",
    [
        (Decimal("100"), Grade.OUTSTANDING),
        (Decimal("90"), Grade.OUTSTANDING),
        (Decimal("89.9"), Grade.GOOD),
        (Decimal("80"), Grade.GOOD),
        (Decimal("79.5"), Grade.PASS),
        (Decimal("70"), Grade.PASS),
        (Decimal("69.9"), Grade.WARN),
        (Decimal("60"), Grade.WARN),
        (Decimal("59.9"), Grade.FAIL),
        (Decimal("0"), Grade.FAIL),
    ],
)
def test_classify_grade_切點(score, expected):
    assert classify_grade(score) is expected


def test_compute_bonus_優等主管_10000_x_分數百分比():
    rates_map = {
        (RoleGroup.SUPERVISOR, Grade.OUTSTANDING): Decimal("10000"),
        (RoleGroup.SUPERVISOR, Grade.GOOD): Decimal("5000"),
    }
    # 95 分 → 10000 × 0.95 = 9500
    assert compute_bonus_amount(
        Decimal("95"), Grade.OUTSTANDING, RoleGroup.SUPERVISOR, rates_map
    ) == Decimal("9500.00")


def test_compute_bonus_甲等_8000_x_分數百分比():
    rates_map = {
        (RoleGroup.HEAD_TEACHER, Grade.OUTSTANDING): Decimal("8000"),
        (RoleGroup.HEAD_TEACHER, Grade.GOOD): Decimal("4000"),
    }
    # 85 分 → 4000 × 0.85 = 3400
    assert compute_bonus_amount(
        Decimal("85"), Grade.GOOD, RoleGroup.HEAD_TEACHER, rates_map
    ) == Decimal("3400.00")


def test_compute_bonus_乙等以下歸零():
    rates_map = {(RoleGroup.HEAD_TEACHER, Grade.OUTSTANDING): Decimal("8000")}
    for grade in (Grade.PASS, Grade.WARN, Grade.FAIL):
        assert compute_bonus_amount(
            Decimal("75"), grade, RoleGroup.HEAD_TEACHER, rates_map
        ) == Decimal("0")


def test_compute_bonus_缺_rate_時_歸零():
    """rates_map 沒對應 (role_group, grade) 時，回 0 而不是 raise。"""
    rates_map = {}
    assert compute_bonus_amount(
        Decimal("95"), Grade.OUTSTANDING, RoleGroup.ASSISTANT, rates_map
    ) == Decimal("0")


def test_default_cycle_dates_第一學期():
    # academic_year = 114（民國 114 = 2025 西元）
    s, e, calc = default_cycle_dates(academic_year=114, semester="FIRST")
    assert s == date(2025, 8, 1)
    assert e == date(2026, 1, 31)
    assert calc == date(2025, 9, 15)


def test_default_cycle_dates_第二學期():
    s, e, calc = default_cycle_dates(academic_year=114, semester="SECOND")
    assert s == date(2026, 2, 1)
    assert e == date(2026, 7, 31)
    assert calc == date(2026, 3, 15)


def test_default_cycle_dates_接受_Semester_enum():
    s1, _, _ = default_cycle_dates(academic_year=114, semester=Semester.FIRST)
    s2, _, _ = default_cycle_dates(academic_year=114, semester="FIRST")
    assert s1 == s2


def test_default_calc_date():
    assert default_calc_date("FIRST", 2025) == date(2025, 9, 15)
    assert default_calc_date(Semester.SECOND, 2026) == date(2026, 3, 15)


@pytest.mark.parametrize(
    "title,expected",
    [
        ("園長", RoleGroup.SUPERVISOR),
        ("主任", RoleGroup.SUPERVISOR),
        ("執行長", RoleGroup.SUPERVISOR),
        ("總園長", RoleGroup.SUPERVISOR),
        ("班導師", RoleGroup.HEAD_TEACHER),
        ("行政會計", RoleGroup.HEAD_TEACHER),
        ("會計", RoleGroup.HEAD_TEACHER),
        ("班導", RoleGroup.HEAD_TEACHER),
        ("副班導師", RoleGroup.ASSISTANT),
        ("主廚", RoleGroup.ASSISTANT),
        ("司機", RoleGroup.ASSISTANT),
        ("儲備教師", RoleGroup.ASSISTANT),
        ("未知職稱", RoleGroup.ASSISTANT),
        ("", RoleGroup.ASSISTANT),
        (None, RoleGroup.ASSISTANT),
    ],
)
def test_suggest_role_group(title, expected):
    assert suggest_role_group(title) is expected
