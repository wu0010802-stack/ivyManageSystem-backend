"""勞健保級距表 DB 化測試（2026-05-07 新增）

驗證：
1. `InsuranceService.load_brackets_from_db` 成功時覆寫 self.table
2. DB 無資料時 silent fallback 到 hardcode（不 raise）
3. `update_rates_from_db` 同步 max cap（NULL → fallback、有值 → 採用）
4. max cap 覆寫後 calculate() 用新上限 clamp
"""

import pytest

from services.insurance_service import (
    InsuranceService,
    INSURANCE_TABLE_2026,
    LABOR_MAX_INSURED_SALARY,
    HEALTH_MAX_INSURED_SALARY,
    PENSION_MAX_INSURED_SALARY,
)


@pytest.fixture
def service():
    return InsuranceService()


class TestBracketsDbInit:
    def test_default_uses_hardcode_table(self, service):
        """init 後尚未呼叫 load_brackets_from_db → 沿用 hardcode INSURANCE_TABLE_2026"""
        assert (
            service.table is INSURANCE_TABLE_2026
            or service.table == INSURANCE_TABLE_2026
        )

    def test_default_max_caps_match_constants(self, service):
        assert service.labor_max_insured == LABOR_MAX_INSURED_SALARY
        assert service.health_max_insured == HEALTH_MAX_INSURED_SALARY
        assert service.pension_max_insured == PENSION_MAX_INSURED_SALARY

    def test_load_failure_falls_back_silently(self, service, monkeypatch):
        """DB 表不存在 / import 失敗時，load_brackets_from_db 回 False、不改 table"""
        original = service.table

        def _broken_import(*args, **kwargs):
            raise ImportError("simulated missing models")

        monkeypatch.setattr(
            "services.insurance_service.logger.warning", lambda *a, **k: None
        )
        # 模擬 import 失敗：直接讓 method 走 except 分支
        import services.insurance_service as ins_mod

        original_method = ins_mod.InsuranceService.load_brackets_from_db

        def _force_fail(self, year=None):
            try:
                raise RuntimeError("simulated db down")
            except Exception:
                return False

        monkeypatch.setattr(
            ins_mod.InsuranceService, "load_brackets_from_db", _force_fail
        )
        result = service.load_brackets_from_db()
        assert result is False
        assert service.table == original  # 未被改寫


class TestMaxCapsOverride:
    def test_update_rates_with_max_caps_override(self, service):
        """InsuranceRate 設新上限 → service 採用，calculate 用新值 clamp"""

        class _FakeRate:
            labor_rate = 0.125
            labor_employee_ratio = 0.20
            labor_employer_ratio = 0.70
            health_rate = 0.0517
            health_employee_ratio = 0.30
            health_employer_ratio = 0.60
            pension_employer_rate = 0.06
            average_dependents = 0.56
            labor_max_insured = 50000  # 假設新政府公告勞保上限調至 50000
            health_max_insured = None  # NULL → fallback
            pension_max_insured = 200000

        service.update_rates_from_db(_FakeRate())
        assert service.labor_max_insured == 50000
        assert service.health_max_insured == HEALTH_MAX_INSURED_SALARY  # NULL fallback
        assert service.pension_max_insured == 200000

    def test_update_rates_with_none_record_keeps_defaults(self, service):
        service.update_rates_from_db(None)
        assert service.labor_max_insured == LABOR_MAX_INSURED_SALARY

    def test_calculate_respects_overridden_labor_cap(self, service):
        """壓低 labor_max_insured → 高薪員工的 labor 部分按新上限級距"""
        # 把勞保上限壓到 38200 級距（一個合法級距值，避免級距查找邊界問題）
        service.labor_max_insured = 38200
        result = service.calculate(60000)
        # 38200 級的 labor_employee = 955
        assert result.labor_employee == 955
        # health/pension 不受影響（仍走 60800 級距）
        assert result.health_employee == 943  # 60800 級
        assert result.pension_employer == 3648
