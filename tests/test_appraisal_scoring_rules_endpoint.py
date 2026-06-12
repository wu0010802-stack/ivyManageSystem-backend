"""appraisal scoring_rules CRUD endpoint 測試（8 case）。

複用 test_appraisal_current_endpoint.py 的 SQLite + login 真實 JWT fixture pattern。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.appraisal import appraisal_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.appraisal import AppraisalScoringRule
from models.auth import User
from models.database import Base
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-scoring-rules.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(appraisal_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(
    session, username, perms, password="TempPass123", role="admin"
) -> User:
    if isinstance(perms, str):
        perms = [perms]
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        permission_names=perms,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


# === GET /scoring_rules ===


class TestListScoringRules:
    def test_returns_current_effective_rules(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", Permission.APPRAISAL_READ)
            s.add_all(
                [
                    AppraisalScoringRule(
                        item_code="LATE_EARLY",
                        effective_from=date(2026, 1, 1),
                        rule_type="PER_UNIT",
                        rule_config={"per_unit_delta": "-0.25"},
                    ),
                    AppraisalScoringRule(
                        item_code="LATE_EARLY",
                        effective_from=date(2026, 7, 1),
                        rule_type="PER_UNIT",
                        rule_config={"per_unit_delta": "-0.5"},
                    ),
                ]
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.get("/api/appraisal/scoring_rules?effective_on=2026-06-01")
        assert r.status_code == 200, r.text
        rules = r.json()
        late = [x for x in rules if x["item_code"] == "LATE_EARLY"]
        assert len(late) == 1
        assert Decimal(str(late[0]["rule_config"]["per_unit_delta"])) == Decimal(
            "-0.25"
        )

    def test_default_effective_on_is_today(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", Permission.APPRAISAL_READ)
            s.add(
                AppraisalScoringRule(
                    item_code="LATE_EARLY",
                    effective_from=date(2026, 1, 1),
                    rule_type="PER_UNIT",
                    rule_config={"per_unit_delta": "-0.25"},
                )
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.get("/api/appraisal/scoring_rules")
        assert r.status_code == 200, r.text

    def test_requires_read_permission(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "noperm", Permission.CLASSROOMS_READ)
            s.commit()
        assert _login(client, "noperm").status_code == 200
        r = client.get("/api/appraisal/scoring_rules")
        assert r.status_code == 403


# === GET /scoring_rules/history ===


class TestGetScoringRuleHistory:
    def test_returns_versions_desc(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", Permission.APPRAISAL_READ)
            s.add_all(
                [
                    AppraisalScoringRule(
                        item_code="LATE_EARLY",
                        effective_from=date(2026, 1, 1),
                        rule_type="PER_UNIT",
                        rule_config={"per_unit_delta": "-0.25"},
                    ),
                    AppraisalScoringRule(
                        item_code="LATE_EARLY",
                        effective_from=date(2026, 7, 1),
                        rule_type="PER_UNIT",
                        rule_config={"per_unit_delta": "-0.5"},
                    ),
                ]
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.get("/api/appraisal/scoring_rules/history?item_code=LATE_EARLY")
        assert r.status_code == 200, r.text
        versions = r.json()
        assert len(versions) == 2
        assert versions[0]["effective_from"] == "2026-07-01"
        assert versions[1]["effective_from"] == "2026-01-01"


# === POST /scoring_rules ===


class TestCreateScoringRule:
    def test_create_per_unit_rule(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin1",
                ["APPRAISAL_RULE_WRITE", "APPRAISAL_READ"],
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        future = (date.today() + timedelta(days=30)).isoformat()
        r = client.post(
            "/api/appraisal/scoring_rules",
            json={
                "item_code": "LATE_EARLY",
                "effective_from": future,
                "rule_type": "PER_UNIT",
                "rule_config": {"per_unit_delta": "-0.3"},
            },
        )
        assert r.status_code == 201, r.text
        assert Decimal(str(r.json()["rule_config"]["per_unit_delta"])) == Decimal(
            "-0.3"
        )

    def test_reject_past_effective_from(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin1",
                ["APPRAISAL_RULE_WRITE", "APPRAISAL_READ"],
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.post(
            "/api/appraisal/scoring_rules",
            json={
                "item_code": "LATE_EARLY",
                "effective_from": "2020-01-01",
                "rule_type": "PER_UNIT",
                "rule_config": {"per_unit_delta": "-0.25"},
            },
        )
        assert r.status_code == 422, r.text

    def test_reject_duplicate_item_code_date(self, client_with_db):
        client, sf = client_with_db
        future = date.today() + timedelta(days=30)
        with sf() as s:
            _create_user(
                s,
                "admin1",
                ["APPRAISAL_RULE_WRITE", "APPRAISAL_READ"],
            )
            s.add(
                AppraisalScoringRule(
                    item_code="LATE_EARLY",
                    effective_from=future,
                    rule_type="PER_UNIT",
                    rule_config={"per_unit_delta": "-0.25"},
                )
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.post(
            "/api/appraisal/scoring_rules",
            json={
                "item_code": "LATE_EARLY",
                "effective_from": future.isoformat(),
                "rule_type": "PER_UNIT",
                "rule_config": {"per_unit_delta": "-0.5"},
            },
        )
        assert r.status_code == 409, r.text

    def test_validate_tier_must_have_min_zero(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin1",
                ["APPRAISAL_RULE_WRITE", "APPRAISAL_READ"],
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        future = (date.today() + timedelta(days=30)).isoformat()
        r = client.post(
            "/api/appraisal/scoring_rules",
            json={
                "item_code": "RETURNING_RATE_0315",
                "effective_from": future,
                "rule_type": "TIER",
                "rule_config": {
                    "input_field": "retention_rate",
                    "tiers": [{"min": "100", "delta": "6"}],  # 缺 min=0
                },
            },
        )
        assert r.status_code == 422, r.text

    def test_requires_rule_write_permission(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            # 只有 APPRAISAL_READ，沒有 APPRAISAL_RULE_WRITE
            _create_user(s, "readonly", Permission.APPRAISAL_READ)
            s.commit()
        assert _login(client, "readonly").status_code == 200
        future = (date.today() + timedelta(days=30)).isoformat()
        r = client.post(
            "/api/appraisal/scoring_rules",
            json={
                "item_code": "LATE_EARLY",
                "effective_from": future,
                "rule_type": "PER_UNIT",
                "rule_config": {"per_unit_delta": "-0.25"},
            },
        )
        assert r.status_code == 403, r.text


# === MANUAL_DELTA rule_config 驗證 ===


class TestCreateManualDeltaRule:
    def test_create_manual_delta_rule_成功(self, client_with_db):
        """config {"min_delta":-10,"max_delta":0} 建立 MANUAL_DELTA 規則應回 201。"""
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin1",
                ["APPRAISAL_RULE_WRITE", "APPRAISAL_READ"],
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        future = (date.today() + timedelta(days=30)).isoformat()
        r = client.post(
            "/api/appraisal/scoring_rules",
            json={
                "item_code": "CHILD_ACCIDENT",
                "effective_from": future,
                "rule_type": "MANUAL_DELTA",
                "rule_config": {"min_delta": -10, "max_delta": 0},
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["rule_type"] == "MANUAL_DELTA"
        assert Decimal(str(body["rule_config"]["min_delta"])) == Decimal("-10")
        assert Decimal(str(body["rule_config"]["max_delta"])) == Decimal("0")

    def test_create_manual_delta_rule_缺max_delta_422(self, client_with_db):
        """config 缺 max_delta → 422 ValidationError。"""
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin2",
                ["APPRAISAL_RULE_WRITE", "APPRAISAL_READ"],
            )
            s.commit()
        assert _login(client, "admin2").status_code == 200
        future = (date.today() + timedelta(days=30)).isoformat()
        r = client.post(
            "/api/appraisal/scoring_rules",
            json={
                "item_code": "CHILD_ACCIDENT",
                "effective_from": future,
                "rule_type": "MANUAL_DELTA",
                "rule_config": {"min_delta": -10},  # 缺 max_delta
            },
        )
        assert r.status_code == 422, r.text

    def test_create_manual_delta_rule_min_大於_max_422(self, client_with_db):
        """min_delta > max_delta → 422，detail 含「min_delta 不可大於 max_delta」。"""
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin3",
                ["APPRAISAL_RULE_WRITE", "APPRAISAL_READ"],
            )
            s.commit()
        assert _login(client, "admin3").status_code == 200
        future = (date.today() + timedelta(days=30)).isoformat()
        r = client.post(
            "/api/appraisal/scoring_rules",
            json={
                "item_code": "CHILD_ACCIDENT",
                "effective_from": future,
                "rule_type": "MANUAL_DELTA",
                "rule_config": {"min_delta": 5, "max_delta": -5},  # min > max
            },
        )
        assert r.status_code == 422, r.text
        assert "min_delta 不可大於 max_delta" in r.text
