from datetime import date

from scripts.seedgen.config import SeedConfig


def test_year_bounds_114():
    c = SeedConfig(academic_year=114)
    assert c.year_start == date(2025, 8, 1)
    assert c.year_end == date(2026, 7, 31)


def test_scale_profile_standard():
    c = SeedConfig()
    p = c.scale_profile
    assert p["classrooms"] == 7 and p["employees"] == 23 and p["students"] == 170


def test_default_today_mid_year():
    assert SeedConfig().today == date(2026, 2, 16)
