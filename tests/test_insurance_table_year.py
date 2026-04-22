"""勞健保級距表年度更新機制

系統硬編碼年度級距表（INSURANCE_TABLE_2026）與費率。政府每年 1/1
公告新級距 / 費率，須手動更新。本測試在年度推進時強制開發者注意。
"""

from datetime import date

import pytest

from services.insurance_service import (
    CURRENT_INSURANCE_YEAR,
    InsuranceService,
    LABOR_INSURANCE_RATE,
    HEALTH_INSURANCE_RATE,
    PENSION_EMPLOYER_RATE,
)


class TestInsuranceYearMarker:
    def test_current_year_marker_matches_2026(self):
        """級距表年度標記；若政府公告新年度表請同步更新"""
        assert CURRENT_INSURANCE_YEAR == 2026

    def test_labor_insurance_rate_is_12_5pct(self):
        """2026 年勞保費率（含就保）12.5%"""
        assert LABOR_INSURANCE_RATE == 0.125

    def test_health_insurance_rate_is_5_17pct(self):
        assert HEALTH_INSURANCE_RATE == 0.0517

    def test_pension_employer_rate_is_6pct(self):
        assert PENSION_EMPLOYER_RATE == 0.06


class TestInsuranceServiceYearWarning:
    def test_constructor_warns_when_table_outdated(self, caplog):
        """若系統年度 > CURRENT_INSURANCE_YEAR，建構 InsuranceService 應發 warning"""
        import logging

        caplog.set_level(logging.WARNING)

        # 模擬晚於 CURRENT_INSURANCE_YEAR + 1 年
        import services.insurance_service as ins_mod

        original = ins_mod.CURRENT_INSURANCE_YEAR
        try:
            ins_mod.CURRENT_INSURANCE_YEAR = date.today().year - 1
            InsuranceService()
            assert any(
                "級距表" in rec.message
                or "過期" in rec.message
                or "outdated" in rec.message.lower()
                for rec in caplog.records
            )
        finally:
            ins_mod.CURRENT_INSURANCE_YEAR = original

    def test_constructor_no_warning_when_table_current(self, caplog):
        """系統年度 == CURRENT_INSURANCE_YEAR 時不應 warning"""
        import logging

        caplog.set_level(logging.WARNING)
        import services.insurance_service as ins_mod

        original = ins_mod.CURRENT_INSURANCE_YEAR
        try:
            ins_mod.CURRENT_INSURANCE_YEAR = date.today().year
            InsuranceService()
            assert not any("級距表" in rec.message for rec in caplog.records)
        finally:
            ins_mod.CURRENT_INSURANCE_YEAR = original
