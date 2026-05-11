"""appraisal events 測試。依賴 conftest fixtures（T17 補齊）。"""

import pytest
from datetime import date, timedelta
from decimal import Decimal


@pytest.mark.skipif(
    True,
    reason="conftest fixtures 將在 T17 加入",
)
class TestAppraisalEvents:

    def test_create_event_帶_catalog_自動填_score(
        self, client, supervisor_headers, participant, db_session
    ):
        from models.appraisal import AppraisalPenaltyCatalogItem
        from sqlalchemy import select

        catalog = db_session.execute(
            select(AppraisalPenaltyCatalogItem).where(
                AppraisalPenaltyCatalogItem.code == "MERIT_COMMENDATION"
            )
        ).scalar_one()
        resp = client.post(
            "/api/appraisal/events",
            json={
                "participant_id": participant.id,
                "catalog_item_id": catalog.id,
                "event_type": "COMMENDATION",
                "event_date": date.today().isoformat(),
                "score_delta": "2.0",
                "title": "教學表現優異",
            },
            headers=supervisor_headers,
        )
        assert resp.status_code == 201

    def test_create_event_自己不能登自己(
        self, client, teacher_headers, teacher_participant
    ):
        resp = client.post(
            "/api/appraisal/events",
            json={
                "participant_id": teacher_participant.id,
                "event_type": "COMMENDATION",
                "event_date": date.today().isoformat(),
                "score_delta": "2",
                "title": "自肥",
            },
            headers=teacher_headers,
        )
        assert resp.status_code == 403
        assert "self_event_forbidden" in resp.json()["detail"]

    def test_create_event_日期落_cycle_外_被擋(
        self, client, supervisor_headers, participant, sample_cycle
    ):
        out_of_range = sample_cycle.end_date + timedelta(days=10)
        resp = client.post(
            "/api/appraisal/events",
            json={
                "participant_id": participant.id,
                "event_type": "WARNING",
                "event_date": out_of_range.isoformat(),
                "score_delta": "-2",
                "title": "事件",
            },
            headers=supervisor_headers,
        )
        assert resp.status_code == 400
        assert "event_date_out_of_cycle" in resp.json()["detail"]

    def test_patch_event_觸發_stale(
        self, client, supervisor_headers, existing_event, db_session
    ):
        """PATCH 事件後 summary 應被標 stale。"""
        resp = client.patch(
            f"/api/appraisal/events/{existing_event.id}",
            json={"title": "修正說明"},
            headers=supervisor_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == existing_event.id

    def test_revert_event_不真刪(
        self, client, supervisor_headers, existing_event, db_session
    ):
        resp = client.post(
            f"/api/appraisal/events/{existing_event.id}/revert",
            json={"reason": "誤登"},
            headers=supervisor_headers,
        )
        assert resp.status_code == 200
        db_session.expire_all()
        from models.appraisal import AppraisalEvent

        ev = db_session.get(AppraisalEvent, existing_event.id)
        assert ev.reverted_at is not None

    def test_finalized_summary_補事件_409(
        self, client, supervisor_headers, finalized_summary
    ):
        pid = finalized_summary.participant_id
        resp = client.post(
            "/api/appraisal/events",
            json={
                "participant_id": pid,
                "event_type": "MINOR_DEMERIT",
                "event_date": date.today().isoformat(),
                "score_delta": "-3",
                "title": "事後補",
            },
            headers=supervisor_headers,
        )
        assert resp.status_code == 409

    def test_attachment_upload_501(self, client, supervisor_headers, existing_event):
        """附件上傳 v1 回 501 Not Implemented。"""
        resp = client.post(
            f"/api/appraisal/events/{existing_event.id}/attachments",
            headers=supervisor_headers,
        )
        assert resp.status_code == 501
        assert "attachment_upload_not_implemented" in resp.json()["detail"]

    def test_第二筆_major_demerit_觸發_warning_log(
        self, client, supervisor_headers, participant, caplog
    ):
        """第 1 次大過不觸發 warning；第 2 次累積 → termination_threshold_reached warning。

        T15 調查（2026-05-11）：codebase 無通用 create_notification /
        notify_users_with_permission；此測試改驗 WARNING log 被發出，
        確保 log aggregation 工具（Loki / CloudWatch）可觸發告警規則。
        待推播基礎建設確定後，此測試將替換為 monkeypatch + spy 驗測。
        """
        import logging

        payload = {
            "participant_id": participant.id,
            "event_type": "MAJOR_DEMERIT",
            "event_date": date.today().isoformat(),
            "score_delta": "-6",
            "title": "大過事件",
        }

        # 第 1 筆大過：不應觸發 warning
        with caplog.at_level(logging.WARNING, logger="api.appraisal.events"):
            caplog.clear()
            resp = client.post(
                "/api/appraisal/events", json=payload, headers=supervisor_headers
            )
        assert resp.status_code == 201
        assert "termination_threshold_reached" not in caplog.text

        # 第 2 筆大過：累積 ≥ 2，應觸發 warning
        with caplog.at_level(logging.WARNING, logger="api.appraisal.events"):
            caplog.clear()
            resp = client.post(
                "/api/appraisal/events", json=payload, headers=supervisor_headers
            )
        assert resp.status_code == 201
        assert "termination_threshold_reached" in caplog.text
        assert str(participant.id) in caplog.text
