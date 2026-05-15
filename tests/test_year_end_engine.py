"""年終獎金計算引擎單元測試（純函式）。

對應 services/year_end/engine.py。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from models.appraisal import RoleGroup
from services.year_end.engine import (
    compute_avg_performance,
    compute_gross_amount,
    compute_payable_subtotal,
    compute_subtotal,
    compute_total_amount,
)


@pytest.fixture
def org_settings():
    return SimpleNamespace(
        achievement_rate_first=Decimal("80"),
        achievement_rate_second=Decimal("90"),
        org_achievement_rate=Decimal("83.6"),
    )


@pytest.fixture
def class_target():
    return SimpleNamespace(
        returning_rate_first=Decimal("85"),
        returning_rate_second=Decimal("88"),
        achievement_rate_first=Decimal("82"),
        achievement_rate_second=Decimal("87"),
    )


class TestStep1AvgPerformance:
    def test_班導_用_6_欄(self, org_settings, class_target):
        avg, meta = compute_avg_performance(
            org_settings, class_target, RoleGroup.HEAD_TEACHER
        )
        # (80+90+85+88+82+87) / 6 = 512/6 = 85.33...
        assert avg == Decimal("85.33")
        assert meta["count"] == 6

    def test_廚工_只用_全校_2_欄(self, org_settings, class_target):
        avg, meta = compute_avg_performance(
            org_settings, class_target, RoleGroup.COOK
        )
        # (80+90)/2 = 85
        assert avg == Decimal("85.00")
        assert meta["count"] == 2

    def test_職員_無班級_只用_全校_2_欄(self, org_settings):
        avg, meta = compute_avg_performance(org_settings, None, RoleGroup.STAFF)
        assert avg == Decimal("85.00")
        assert meta["count"] == 2

    def test_班導_缺_class_target_退化_2_欄(self, org_settings):
        avg, meta = compute_avg_performance(
            org_settings, None, RoleGroup.HEAD_TEACHER
        )
        assert meta["count"] == 2


class TestStep2GrossAmount:
    def test_base_30000_festival_8000_avg_85(self):
        # (30000 + 8000) × 85% = 32300
        gross = compute_gross_amount(
            Decimal("30000"), Decimal("8000"), Decimal("85")
        )
        assert gross == Decimal("32300.00")


class TestStep3Subtotal:
    def test_gross_30000_rate_83_6(self):
        # 30000 × 83.6% = 25080
        subtotal = compute_subtotal(Decimal("30000"), Decimal("83.6"))
        assert subtotal == Decimal("25080.00")


class TestStep5PayableSubtotal:
    def test_全年滿勤_等於_subtotal_減扣項(self):
        payable = compute_payable_subtotal(
            Decimal("30000"), Decimal("500"), Decimal("12")
        )
        # (30000 - 500) × 12/12 = 29500
        assert payable == Decimal("29500.00")

    def test_到職_6_個月_拿一半(self):
        payable = compute_payable_subtotal(
            Decimal("30000"), Decimal("0"), Decimal("6")
        )
        assert payable == Decimal("15000.00")

    def test_到職_3_個月_拿四分之一(self):
        payable = compute_payable_subtotal(
            Decimal("30000"), Decimal("0"), Decimal("3")
        )
        assert payable == Decimal("7500.00")

    def test_未到職_0_個月_拿_0(self):
        payable = compute_payable_subtotal(
            Decimal("30000"), Decimal("0"), Decimal("0")
        )
        assert payable == Decimal("0.00")


class TestStep6TotalAmount:
    def test_應領_加_特別獎金(self):
        total = compute_total_amount(Decimal("25000"), Decimal("3000"))
        assert total == Decimal("28000.00")

    def test_特別獎金_可為負_多退少補(self):
        # 節慶差額多領回退
        total = compute_total_amount(Decimal("25000"), Decimal("-500"))
        assert total == Decimal("24500.00")
