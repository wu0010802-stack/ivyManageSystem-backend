"""appraisal summaries 三階簽核測試。依賴 conftest fixtures（T17）。"""

import pytest


@pytest.mark.skipif(
    True,
    reason="conftest fixtures 將在 T17 加入",
)
class TestAppraisalSummaries:

    def test_recompute_建立_DRAFT_summary(
        self, client, admin_headers, locked_cycle_with_participants
    ):
        resp = client.post(
            f"/api/appraisal/cycles/{locked_cycle_with_participants.id}/summaries:recompute",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert all(s["status"] == "DRAFT" for s in body)

    def test_sign_supervisor_DRAFT_to_SIGNED(
        self, client, supervisor_headers, draft_summary
    ):
        resp = client.post(
            f"/api/appraisal/summaries/{draft_summary.id}/sign_supervisor",
            json={"comment": "本學期表現良好"},
            headers=supervisor_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "SUPERVISOR_SIGNED"

    def test_sign_順序_必須_supervisor_先(
        self, client, accountant_headers, draft_summary
    ):
        resp = client.post(
            f"/api/appraisal/summaries/{draft_summary.id}/sign_accounting",
            json={"comment": ""},
            headers=accountant_headers,
        )
        assert resp.status_code == 400
        assert "stage_invalid" in resp.json()["detail"]

    def test_finalize_要求雙簽_reason(
        self, client, principal_headers, accounting_signed_summary
    ):
        r1 = client.post(
            f"/api/appraisal/summaries/{accounting_signed_summary.id}/finalize",
            json={"comment": "", "reason": ""},
            headers=principal_headers,
        )
        assert r1.status_code == 422

        r2 = client.post(
            f"/api/appraisal/summaries/{accounting_signed_summary.id}/finalize",
            json={"comment": "已確認", "reason": "115 上學期定稿"},
            headers=principal_headers,
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "FINALIZED"

    def test_reject_回_DRAFT_清簽核戳記(
        self, client, principal_headers, accounting_signed_summary
    ):
        resp = client.post(
            f"/api/appraisal/summaries/{accounting_signed_summary.id}/reject",
            json={"reason": "分數有誤"},
            headers=principal_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "DRAFT"
        assert body["supervisor_signed_at"] is None
        assert body["accounting_signed_at"] is None

    def test_recompute_FINALIZED_summary_被擋(
        self, client, admin_headers, finalized_summary
    ):
        resp = client.post(
            f"/api/appraisal/cycles/{finalized_summary.cycle_id}/summaries:recompute",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        # finalized 應被跳過保留
        skipped = [s for s in resp.json() if s["id"] == finalized_summary.id]
        assert skipped and skipped[0]["status"] == "FINALIZED"

    def test_並行_sign_第二筆_400_stage_already_changed(
        self, client, supervisor_headers, draft_summary
    ):
        p1 = client.post(
            f"/api/appraisal/summaries/{draft_summary.id}/sign_supervisor",
            json={"comment": ""},
            headers=supervisor_headers,
        )
        p2 = client.post(
            f"/api/appraisal/summaries/{draft_summary.id}/sign_supervisor",
            json={"comment": ""},
            headers=supervisor_headers,
        )
        assert p1.status_code == 200
        assert p2.status_code == 400
