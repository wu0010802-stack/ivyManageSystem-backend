"""appraisal penalty_catalog 測試。依賴 conftest fixtures（T17）。"""

import pytest


@pytest.mark.skipif(True, reason="conftest fixtures 將在 T17 加入")
class TestAppraisalPenaltyCatalog:

    def test_list_catalog_含_29_筆_seed(self, client, admin_headers):
        resp = client.get(
            "/api/appraisal/penalty_catalog?active_only=true",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 29

    def test_list_catalog_by_category(self, client, admin_headers):
        resp = client.get(
            "/api/appraisal/penalty_catalog?category=MERIT",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        # 3 條 merit (嘉獎/小功/大功)
        assert len(body) == 3
        assert all(c["category"] == "MERIT" for c in body)

    def test_patch_catalog_score_delta(self, client, admin_headers, db_session):
        from models.appraisal import AppraisalPenaltyCatalogItem
        from sqlalchemy import select

        item = db_session.execute(
            select(AppraisalPenaltyCatalogItem).where(
                AppraisalPenaltyCatalogItem.code == "MERIT_COMMENDATION"
            )
        ).scalar_one()
        resp = client.patch(
            f"/api/appraisal/penalty_catalog/{item.id}",
            json={"default_score_delta": "2.5"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["default_score_delta"] == "2.5"
