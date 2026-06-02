"""tests/test_year_end_settlement_builder.py — settlement_builder helpers 單元測試（TDD）

覆蓋：
  1. festival_base_for_role  — 節慶=角色基數查表（決策④：單筆查 BonusConfig）
  2. compute_hire_months     — 在職月數（整個 cycle / 部分 / 離職在 cycle 中）
  3. resolve_org_achievement_rate — 組織績效率（滿年平均 / 僅一學期）
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.config import BonusConfig
from services.year_end import settlement_builder as sb

# ============ Fixtures ============


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


# ============ helpers ============


def _bonus_config(session, **overrides) -> BonusConfig:
    """建一筆 BonusConfig 並 flush；只需傳入要覆蓋的欄位。"""
    defaults = dict(
        config_year=2025,
        version=1,
        is_active=True,
        head_teacher_ab=2000,
        head_teacher_c=1500,
        assistant_teacher_ab=1200,
        assistant_teacher_c=1200,
        principal_festival=6500,
        director_festival=3500,
        leader_festival=2000,
        driver_festival=1000,
        designer_festival=1000,
        admin_festival=2000,
        art_teacher_festival=2000,
    )
    defaults.update(overrides)
    bc = BonusConfig(**defaults)
    session.add(bc)
    session.flush()
    return bc


def _emp(hire_date=None, resign_date=None):
    """輕量 stub：只需 hire_date / resign_date 兩個屬性。"""
    return SimpleNamespace(hire_date=hire_date, resign_date=resign_date)


# ============ Test: festival_base_for_role ============


class TestFestivalBaseForRole:
    def test_head_teacher_ab(self, session):
        _bonus_config(session, head_teacher_ab=2000)
        assert sb.festival_base_for_role(session, "head_teacher_ab") == Decimal("2000")

    def test_principal(self, session):
        _bonus_config(session, principal_festival=6500)
        assert sb.festival_base_for_role(session, "principal") == Decimal("6500")

    def test_director(self, session):
        _bonus_config(session, director_festival=3500)
        assert sb.festival_base_for_role(session, "director") == Decimal("3500")

    def test_art_teacher_festival(self, session):
        _bonus_config(session, art_teacher_festival=1800)
        assert sb.festival_base_for_role(session, "art_teacher") == Decimal("1800")

    def test_unknown_role_returns_zero(self, session):
        _bonus_config(session)
        assert sb.festival_base_for_role(session, "unknown_role_xyz") == Decimal("0")

    def test_no_config_returns_zero(self, session):
        # DB 裡完全沒有 BonusConfig 時回 Decimal("0")
        assert sb.festival_base_for_role(session, "head_teacher_ab") == Decimal("0")

    def test_festival_base_for_role_handles_null_field(self, session):
        # art_teacher_festival 是 nullable=True 欄位；設為 None 時 getattr/None 守衛應回 Decimal("0")
        _bonus_config(session, art_teacher_festival=None)
        assert sb.festival_base_for_role(session, "art_teacher") == Decimal("0")

    def test_uses_latest_by_id(self, session):
        """多筆 BonusConfig 時取 id 最大（最新）那筆。"""
        _bonus_config(session, head_teacher_ab=1000)
        _bonus_config(session, head_teacher_ab=2000)
        assert sb.festival_base_for_role(session, "head_teacher_ab") == Decimal("2000")


# ============ Test: compute_hire_months ============


class TestComputeHireMonths:
    CYCLE_START = date(2025, 2, 1)
    CYCLE_END = date(2026, 1, 31)

    def test_full_year_no_hire_or_resign(self):
        emp = _emp(hire_date=None, resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("12")

    def test_full_year_hired_before_cycle(self):
        emp = _emp(hire_date=date(2020, 1, 1), resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("12")

    def test_partial_hired_midcycle(self):
        # hire 2025-04-01：cycle_start=2025-02-01, first work month=2025-04
        # months: 2025-04 to 2026-01 = 10 months
        emp = _emp(hire_date=date(2025, 4, 1), resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("10")

    def test_resigned_midcycle(self):
        # resign 2025-10-31：2025-02 to 2025-10 = 9 months
        emp = _emp(hire_date=None, resign_date=date(2025, 10, 31))
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("9")

    def test_short_tenure_both_hire_and_resign(self):
        # hire 2025-07-01, resign 2025-09-30 → 3 months
        emp = _emp(hire_date=date(2025, 7, 1), resign_date=date(2025, 9, 30))
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("3")

    def test_resign_before_cycle_returns_zero(self):
        # 離職在 cycle 開始前：沒重疊
        emp = _emp(hire_date=None, resign_date=date(2025, 1, 15))
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("0")

    def test_hire_after_cycle_returns_zero(self):
        # 到職在 cycle 結束後：沒重疊
        emp = _emp(hire_date=date(2026, 3, 1), resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("0")

    def test_max_clamp_at_12(self):
        # 即使在職橫跨超過 12 個月（異常資料），回傳 12
        emp = _emp(hire_date=date(2010, 1, 1), resign_date=None)
        result = sb.compute_hire_months(emp, self.CYCLE_START, self.CYCLE_END)
        assert result == Decimal("12")


# ============ Test: resolve_org_achievement_rate ============


class TestResolveOrgAchievementRate:
    def test_full_year_both_semesters(self):
        # 兩學期平均：(75.6 + 91.5) / 2 = 83.55 → round to 1 decimal = 83.6
        result = sb.resolve_org_achievement_rate(
            Decimal("75.6"),
            Decimal("91.5"),
            worked_first=True,
            worked_second=True,
        )
        assert result == Decimal("83.6")

    def test_partial_only_second_semester(self):
        # 只在職第二學期：直接取第二學期
        result = sb.resolve_org_achievement_rate(
            Decimal("75.6"),
            Decimal("91.5"),
            worked_first=False,
            worked_second=True,
        )
        assert result == Decimal("91.5")

    def test_partial_only_first_semester(self):
        # 只在職第一學期：直接取第一學期
        result = sb.resolve_org_achievement_rate(
            Decimal("75.6"),
            Decimal("91.5"),
            worked_first=True,
            worked_second=False,
        )
        assert result == Decimal("75.6")

    def test_neither_semester_returns_zero(self):
        # 兩個都沒做（異常資料）：回 Decimal("0.0")
        result = sb.resolve_org_achievement_rate(
            Decimal("75.6"),
            Decimal("91.5"),
            worked_first=False,
            worked_second=False,
        )
        assert result == Decimal("0.0")

    def test_rounding_to_one_decimal(self):
        # 確保四捨五入到小數點第一位（ROUND_HALF_UP，非 banker's rounding）
        # (80.5 + 80.6) / 2 = 80.55 → ROUND_HALF_UP → 80.6（banker's rounding 會得 80.6 也對，但 2dp 下會錯）
        # 真正鑑別：Decimal("80.55").quantize(0.1, ROUND_HALF_UP)=80.6，ROUND_HALF_EVEN=80.6 也對，
        # 但若 inputs 為 Decimal("80.4") + Decimal("80.5") → avg=80.45 → HALF_UP=80.5, HALF_EVEN=80.4
        # 使用 80.4 / 80.6 確保 avg=80.5 整，而 (80.5+80.6)/2=80.55 → 只 HALF_UP 進位
        result = sb.resolve_org_achievement_rate(
            Decimal("80.5"),
            Decimal("80.6"),
            worked_first=True,
            worked_second=True,
        )
        assert result == Decimal("80.6")
