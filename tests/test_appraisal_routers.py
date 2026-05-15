"""半年考核 router 整合測試（SQLite in-memory）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest_plugins = ["tests.conftest_appraisal"]


class TestCycleLifecycle:
    def test_建立週期_預設帶日期(self, client, admin_headers):
        resp = client.post(
            "/api/appraisal/cycles",
            headers=admin_headers,
            json={"academic_year": 114, "semester": "FIRST"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["academic_year"] == 114
        assert body["start_date"] == "2025-08-01"
        assert body["end_date"] == "2026-01-31"
        assert body["base_score_calc_date"] == "2025-09-15"
        assert body["status"] == "OPEN"

    def test_重複_學期_衝突_409(self, client, admin_headers):
        client.post(
            "/api/appraisal/cycles",
            headers=admin_headers,
            json={"academic_year": 114, "semester": "FIRST"},
        )
        resp = client.post(
            "/api/appraisal/cycles",
            headers=admin_headers,
            json={"academic_year": 114, "semester": "FIRST"},
        )
        assert resp.status_code == 409


class TestScoreItemCatalog:
    def test_catalog_有_16_條(self, client, admin_headers):
        resp = client.get(
            "/api/appraisal/score_item_catalog", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 16


class TestScoreItemsUpsertFlow:
    def _new_cycle(self, client, headers):
        r = client.post(
            "/api/appraisal/cycles",
            headers=headers,
            json={"academic_year": 114, "semester": "SECOND"},
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def test_upsert_score_item_重算_summary(
        self, client, admin_headers, db_session, employee_factory
    ):
        from models.appraisal import (
            AppraisalParticipant,
            AppraisalSummary,
            RoleGroup,
        )
        from sqlalchemy import select

        cycle_id = self._new_cycle(client, admin_headers)

        emp = employee_factory(name="班導A")
        p = AppraisalParticipant(
            cycle_id=cycle_id,
            employee_id=emp.id,
            role_group=RoleGroup.HEAD_TEACHER,
            base_score=Decimal("85"),
        )
        db_session.add(p)
        db_session.commit()
        s = AppraisalSummary(
            participant_id=p.id,
            cycle_id=cycle_id,
            base_score=Decimal("85"),
        )
        db_session.add(s)
        db_session.commit()
        pid = p.id

        # 第一次 upsert：+5 分
        r = client.post(
            "/api/appraisal/score_items",
            headers=admin_headers,
            json={
                "participant_id": pid,
                "item_code": "REWARD_PUNISH",
                "score_delta": "5",
                "note": "嘉獎 1 次",
            },
        )
        assert r.status_code == 201, r.text

        # summary 應重算到 90 → OUTSTANDING
        db_session.expire_all()
        s2 = db_session.execute(
            select(AppraisalSummary).where(AppraisalSummary.participant_id == pid)
        ).scalar_one()
        assert s2.item_score_sum == Decimal("5.00")
        assert s2.total_score == Decimal("90.00")
        assert s2.grade.value == "OUTSTANDING"
        # base 8000 (HEAD_TEACHER × OUTSTANDING) × 100% = 8000
        assert s2.bonus_amount == Decimal("8000.00")

        # 同 item_code 再 upsert：改成 -3 分
        r = client.post(
            "/api/appraisal/score_items",
            headers=admin_headers,
            json={
                "participant_id": pid,
                "item_code": "REWARD_PUNISH",
                "score_delta": "-3",
            },
        )
        assert r.status_code == 201, r.text
        db_session.expire_all()
        s3 = db_session.execute(
            select(AppraisalSummary).where(AppraisalSummary.participant_id == pid)
        ).scalar_one()
        assert s3.item_score_sum == Decimal("-3.00")
        assert s3.total_score == Decimal("82.00")
        assert s3.grade.value == "GOOD"
        # base 4000 × 80% = 3200
        assert s3.bonus_amount == Decimal("3200.00")


class TestYearEndCycles:
    def test_建立_yearend_cycle(self, client, admin_headers):
        resp = client.post(
            "/api/year_end/cycles",
            headers=admin_headers,
            json={"academic_year": 114},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["academic_year"] == 114
        assert body["status"] == "DRAFT"

    def test_org_settings_upsert(self, client, admin_headers):
        r = client.post(
            "/api/year_end/cycles",
            headers=admin_headers,
            json={"academic_year": 114},
        )
        cid = r.json()["id"]
        r2 = client.put(
            f"/api/year_end/cycles/{cid}/org_settings",
            headers=admin_headers,
            json={
                "total_enrollment_target": 200,
                "achievement_rate_first": "85",
                "achievement_rate_second": "90",
                "org_achievement_rate": "83.6",
                "festival_bonus_total_amount": "100000",
                "org_meeting_deduction": "500",
            },
        )
        assert r2.status_code == 200, r2.text
        # Decimal(5,2) 序列化會補 trailing zero
        assert Decimal(r2.json()["org_achievement_rate"]) == Decimal("83.6")

    def test_special_bonus_unique_constraint(
        self, client, admin_headers, employee_factory
    ):
        r = client.post(
            "/api/year_end/cycles",
            headers=admin_headers,
            json={"academic_year": 113},
        )
        cid = r.json()["id"]
        emp = employee_factory(name="員工X")
        payload = {
            "employee_id": emp.id,
            "bonus_type": "EXCESS_ENROLLMENT",
            "period_label": "2025-09",
            "amount": "1500",
            "calc_meta": {"excess_count": 3},
        }
        r1 = client.post(
            f"/api/year_end/cycles/{cid}/special_bonuses",
            headers=admin_headers,
            json=payload,
        )
        assert r1.status_code == 201, r1.text
        # 同 (cycle, employee, bonus_type, period_label) → upsert（不 conflict）
        payload["amount"] = "2000"
        r2 = client.post(
            f"/api/year_end/cycles/{cid}/special_bonuses",
            headers=admin_headers,
            json=payload,
        )
        assert r2.status_code == 201, r2.text
        assert r2.json()["amount"] == "2000.00"
        assert r1.json()["id"] == r2.json()["id"]  # 同一筆


class TestPermissionsCoverage:
    def test_未認證_401(self, client):
        r = client.get("/api/appraisal/cycles")
        assert r.status_code in (401, 403)
        r2 = client.get("/api/year_end/cycles")
        assert r2.status_code in (401, 403)
