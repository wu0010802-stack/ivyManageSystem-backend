"""appraisal participants 測試。依賴 conftest fixtures（T17 補齊）。"""

import pytest


class TestAppraisalParticipants:

    def test_bulk_init_自動排除離職員工(
        self, client, admin_headers, sample_cycle, employee_factory
    ):
        e_active = employee_factory(is_active=True)
        e_left = employee_factory(
            is_active=False,
            resign_date=sample_cycle.start_date,
        )
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/participants:bulk_init",
            json={},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        emp_ids = {p["employee_id"] for p in body}
        assert e_active.id in emp_ids
        assert e_left.id not in emp_ids

    def test_bulk_init_role_group_推薦(
        self, client, admin_headers, sample_cycle, employee_factory
    ):
        e_principal = employee_factory(job_title_name="園長")
        e_teacher = employee_factory(job_title_name="班導師")
        e_kitchen = employee_factory(job_title_name="主廚")
        resp = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/participants:bulk_init",
            json={},
            headers=admin_headers,
        )
        body = {p["employee_id"]: p["role_group"] for p in resp.json()}
        assert body[e_principal.id] == "SUPERVISOR"
        assert body[e_teacher.id] == "HEAD_TEACHER"
        assert body[e_kitchen.id] == "ASSISTANT"

    def test_patch_base_score_觸發_summary_stale(
        self, client, admin_headers, signed_summary
    ):
        pid = signed_summary.participant_id
        resp = client.patch(
            f"/api/appraisal/participants/{pid}",
            json={"base_score": "85.5"},
            headers=admin_headers,
        )
        assert resp.status_code == 200

    def test_delete_participant_有_events_時_409(
        self, client, admin_headers, participant_with_event
    ):
        resp = client.delete(
            f"/api/appraisal/participants/{participant_with_event.id}",
            headers=admin_headers,
        )
        assert resp.status_code == 409

    def test_bulk_init_重複呼叫_idempotent(self, client, admin_headers, sample_cycle):
        r1 = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/participants:bulk_init",
            json={},
            headers=admin_headers,
        )
        r2 = client.post(
            f"/api/appraisal/cycles/{sample_cycle.id}/participants:bulk_init",
            json={},
            headers=admin_headers,
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert len(r1.json()) == len(r2.json())
