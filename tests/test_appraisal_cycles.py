"""appraisal cycles router 測試。

注意：依賴 conftest fixture（sample_cycle、admin_headers、regular_user_headers 等）
將在 Task 17 加入。本 task 寫好測試但整批 skip，等 T17 conftest 補齊後啟用。

Import 路徑慣例（與 cycles.py 一致）：
  - 權限守衛：require_staff_permission（管理端限定，拒絕 teacher/parent role）
  - CycleUnlockRequest.reason 最少 4 字（Field min_length=4），422 透過 schema 強制
"""

import pytest


class TestAppraisalCycles:
    """考核週期 CRUD + 狀態轉換測試。

    T17 補齊 conftest 後，移除 @pytest.mark.skipif 即可全數啟用。
    """

    # ── 建立 ──────────────────────────────────────────────────────────────

    def test_create_cycle_預設日期帶入(self, client, admin_headers):
        """POST /api/appraisal/cycles：未帶 base_score_calc_date 時由 default_cycle_dates 帶入。"""
        resp = client.post(
            "/api/appraisal/cycles",
            json={"academic_year": 114, "semester": "FIRST"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["start_date"] == "2025-08-01"
        assert body["end_date"] == "2026-01-31"
        assert body["base_score_calc_date"] == "2025-09-15"
        assert body["status"] == "OPEN"

    def test_create_cycle_unique_year_semester(self, client, admin_headers):
        """同一學年/學期重複建立應回 409。"""
        payload = {"academic_year": 114, "semester": "SECOND"}
        r1 = client.post("/api/appraisal/cycles", json=payload, headers=admin_headers)
        r2 = client.post("/api/appraisal/cycles", json=payload, headers=admin_headers)
        assert r1.status_code == 201
        assert r2.status_code == 409
        assert "cycle_already_exists" in r2.json()["detail"]

    def test_create_cycle_custom_base_score_calc_date(self, client, admin_headers):
        """手動指定 base_score_calc_date 應覆蓋預設值。"""
        resp = client.post(
            "/api/appraisal/cycles",
            json={
                "academic_year": 113,
                "semester": "FIRST",
                "base_score_calc_date": "2024-10-01",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["base_score_calc_date"] == "2024-10-01"

    # ── 列表 ──────────────────────────────────────────────────────────────

    def test_list_cycles_含_status_filter(self, client, admin_headers, sample_cycle):
        """GET /api/appraisal/cycles?status=OPEN 應包含 sample_cycle（OPEN 狀態）。"""
        resp = client.get("/api/appraisal/cycles?status=OPEN", headers=admin_headers)
        assert resp.status_code == 200
        assert any(c["id"] == sample_cycle.id for c in resp.json())

    def test_list_cycles_無_filter_回全部(self, client, admin_headers, sample_cycle):
        """未帶 status filter 時回傳所有週期。"""
        resp = client.get("/api/appraisal/cycles", headers=admin_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    # ── 修改 ──────────────────────────────────────────────────────────────

    def test_patch_cycle_更新_base_score_calc_date(
        self, client, admin_headers, sample_cycle
    ):
        """PATCH /api/appraisal/cycles/{id}：更新 base_score_calc_date。"""
        resp = client.patch(
            f"/api/appraisal/cycles/{sample_cycle.id}",
            json={"base_score_calc_date": "2025-10-01"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["base_score_calc_date"] == "2025-10-01"

    def test_patch_cycle_CLOSED_回_400(
        self, client, admin_headers, sample_closed_cycle
    ):
        """CLOSED 週期不可修改，應回 400。"""
        resp = client.patch(
            f"/api/appraisal/cycles/{sample_closed_cycle.id}",
            json={"base_score_calc_date": "2025-10-01"},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert "cycle_closed" in resp.json()["detail"]

    # ── lock ──────────────────────────────────────────────────────────────

    def test_lock_cycle_OPEN_to_LOCKED(self, client, admin_headers, sample_cycle):
        """OPEN → LOCKED 成功。"""
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/lock", headers=admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "LOCKED"

    def test_lock_cycle_非_OPEN_回_400(self, client, admin_headers, sample_cycle):
        """已 LOCKED 的週期再 lock 應回 400。"""
        client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/lock", headers=admin_headers
        )
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/lock", headers=admin_headers
        )
        assert resp.status_code == 400
        assert "cycle_status_invalid" in resp.json()["detail"]

    def test_lock_cycle_缺_FINALIZE_權限_403(
        self, client, regular_user_headers, sample_cycle
    ):
        """無 APPRAISAL_FINALIZE 權限應回 403。"""
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/lock",
            headers=regular_user_headers,
        )
        assert resp.status_code == 403

    # ── unlock ────────────────────────────────────────────────────────────

    def test_unlock_cycle_LOCKED_to_OPEN(self, client, admin_headers, sample_cycle):
        """LOCKED → OPEN 成功（reason 足夠長）。"""
        client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/lock", headers=admin_headers
        )
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/unlock",
            json={"reason": "補登事件需解封"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "OPEN"

    def test_unlock_cycle_reason_太短_422(self, client, admin_headers, sample_cycle):
        """reason 少於 4 字應被 schema 拒絕，回 422。"""
        client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/lock", headers=admin_headers
        )
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/unlock",
            json={"reason": ""},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_unlock_cycle_非_LOCKED_回_400(self, client, admin_headers, sample_cycle):
        """OPEN 週期直接 unlock 應回 400。"""
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/unlock",
            json={"reason": "測試解封非 LOCKED 週期"},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert "cycle_status_invalid" in resp.json()["detail"]

    # ── close ─────────────────────────────────────────────────────────────

    def test_close_cycle_全部_finalized_成功(
        self, client, admin_headers, sample_cycle_all_finalized
    ):
        """所有 summary FINALIZED 後可正常封存。"""
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle_all_finalized.id}/close",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "CLOSED"

    def test_close_cycle_幂等(self, client, admin_headers, sample_closed_cycle):
        """已 CLOSED 的週期再 close 應幂等回 200。"""
        resp = client.post(
            f"/api/appraisal/cycles/{sample_closed_cycle.id}/close",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "CLOSED"

    def test_close_cycle_要求所有_summary_finalized(
        self, client, admin_headers, sample_cycle_with_unfinalized_summary
    ):
        """仍有未 FINALIZED 的 summary 應回 400。"""
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle_with_unfinalized_summary.id}/close",
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert "not_all_finalized" in resp.json()["detail"]

    def test_close_cycle_不存在_404(self, client, admin_headers):
        """不存在的 cycle_id 應回 404。"""
        resp = client.post("/api/appraisal/cycles/999999/close", headers=admin_headers)
        assert resp.status_code == 404
