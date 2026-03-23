"""
政府申報格式匯出 — 後端單元測試

測試重點：
  1. _estimate_withholding 扣繳估算邏輯
  2. 免稅額以下回傳 0
  3. 上限封頂（560_000 × 5% = 28_000）
  4. 勞保端點基本呼叫（mock session / insurance）
  5. 健保、勞退、扣繳端點 smoke test
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI


# ──────────────────────────────────────────────────────────────────────────────
# 直接測試純函式 _estimate_withholding
# ──────────────────────────────────────────────────────────────────────────────

from api.gov_reports import _estimate_withholding


class TestEstimateWithholding:
    def test_below_deduction_returns_zero(self):
        """全年收入低於免稅額（428,000）→ 扣繳為 0"""
        assert _estimate_withholding(400_000) == 0

    def test_exactly_at_deduction_returns_zero(self):
        assert _estimate_withholding(428_000) == 0

    def test_one_nt_above_deduction(self):
        """428,001 → taxable=1 → round(1 × 0.05) = 0"""
        assert _estimate_withholding(428_001) == 0

    def test_typical_salary(self):
        """600,000 → taxable=172,000 → round(172,000×0.05)=8,600"""
        assert _estimate_withholding(600_000) == 8_600

    def test_cap_at_560k_taxable(self):
        """超過上限 560,000 taxable → 最多 28,000"""
        # annual=1,100,000 → taxable=672,000 > 560,000 → capped 28,000
        result = _estimate_withholding(1_100_000)
        assert result == 28_000

    def test_exactly_at_cap(self):
        """428,000 + 560,000 = 988,000 → exactly 28,000"""
        assert _estimate_withholding(988_000) == 28_000

    def test_above_cap_same_result(self):
        """超過封頂線後結果不再增加"""
        r1 = _estimate_withholding(988_000)
        r2 = _estimate_withholding(2_000_000)
        assert r1 == r2 == 28_000


# ──────────────────────────────────────────────────────────────────────────────
# Smoke tests：端點在空資料庫回傳 200
# ──────────────────────────────────────────────────────────────────────────────

def _make_test_app():
    from api.gov_reports import router, init_gov_report_services
    ins_svc = MagicMock()
    ins_svc.calculate.return_value = MagicMock(
        employee_share=300,
        employer_share=600,
        govt_share=100,
        health_employee=400,
        health_employer=800,
        health_dependents=0,
        pension_employer=500,
        pension_employee=0,
        pension_employee_rate=0.0,
    )
    init_gov_report_services(ins_svc)

    app = FastAPI()

    # 覆蓋基礎 auth dependency，讓測試繞過 JWT 驗證
    from utils.auth import get_current_user

    async def _mock_user():
        return {"id": 1, "username": "test", "role": "admin", "permissions": -1}

    app.dependency_overrides[get_current_user] = _mock_user
    app.include_router(router)
    return app


@pytest.fixture(scope="module")
def gov_client():
    app = _make_test_app()
    with patch("api.gov_reports.get_session") as mock_gs:
        mock_sess = MagicMock()
        mock_sess.__enter__ = MagicMock(return_value=mock_sess)
        mock_sess.__exit__ = MagicMock(return_value=False)
        # employees query returns empty list
        mock_sess.query.return_value.filter.return_value.all.return_value = []
        mock_sess.query.return_value.filter.return_value.filter.return_value.all.return_value = []
        mock_gs.return_value = mock_sess
        yield TestClient(app, raise_server_exceptions=False)


class TestGovReportEndpoints:
    def test_labor_insurance_xlsx(self, gov_client):
        resp = gov_client.get("/api/gov-reports/labor-insurance?year=2026&month=3&fmt=xlsx")
        assert resp.status_code == 200
        assert "spreadsheet" in resp.headers.get("content-type", "")

    def test_labor_insurance_txt(self, gov_client):
        resp = gov_client.get("/api/gov-reports/labor-insurance?year=2026&month=3&fmt=txt")
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("text/plain")

    def test_health_insurance(self, gov_client):
        resp = gov_client.get("/api/gov-reports/health-insurance?year=2026&month=3")
        assert resp.status_code == 200
        assert "spreadsheet" in resp.headers.get("content-type", "")

    def test_pension(self, gov_client):
        resp = gov_client.get("/api/gov-reports/pension?year=2026&month=3")
        assert resp.status_code == 200
        assert "spreadsheet" in resp.headers.get("content-type", "")

    def test_withholding(self, gov_client):
        resp = gov_client.get("/api/gov-reports/withholding?year=2026")
        assert resp.status_code == 200
        assert "spreadsheet" in resp.headers.get("content-type", "")
