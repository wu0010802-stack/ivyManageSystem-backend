"""appraisal bonus_rates 測試。依賴 conftest fixtures（T17）。"""

import pytest


class TestAppraisalBonusRates:

    def test_list_bonus_rates(self, client, admin_headers):
        resp = client.get("/api/appraisal/bonus_rates", headers=admin_headers)
        assert resp.status_code == 200
        # seed 6 筆（migration T4）
        assert len(resp.json()) >= 6

    def test_create_new_version_不改舊版(self, client, admin_headers):
        resp = client.post(
            "/api/appraisal/bonus_rates",
            json={
                "effective_from": "2027-08-01",
                "role_group": "SUPERVISOR",
                "grade": "OUTSTANDING",
                "base_amount": "12000",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201

    def test_create_duplicate_conflict_409(self, client, admin_headers):
        payload = {
            "effective_from": "2026-08-01",  # 已 seed
            "role_group": "SUPERVISOR",
            "grade": "OUTSTANDING",
            "base_amount": "9999",
        }
        resp = client.post(
            "/api/appraisal/bonus_rates", json=payload, headers=admin_headers
        )
        assert resp.status_code == 409
