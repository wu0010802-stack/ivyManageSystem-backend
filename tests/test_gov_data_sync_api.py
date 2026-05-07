"""api/gov_data_sync.py 主要 endpoint 行為測試。

策略：用 dependency_overrides 跳過實際 staff auth，讓測試專注於 router 邏輯。
"""

import pytest
from fastapi.testclient import TestClient

from main import app
from models.database import (
    GovDataSnapshot,
    InsuranceBracketsStaging,
    MinimumWageStaging,
    session_scope,
)


@pytest.fixture
def client(test_db_session):
    """TestClient + override _DEP_SALARY_WRITE dependency 為 fake admin。"""
    from api.gov_data_sync import _DEP_SALARY_WRITE

    def fake_admin_dep():
        return {"id": 1, "username": "admin", "permissions": 0}

    app.dependency_overrides[_DEP_SALARY_WRITE] = fake_admin_dep
    yield TestClient(app)
    app.dependency_overrides.pop(_DEP_SALARY_WRITE, None)


def test_get_staging_returns_summary(client, test_db_session):
    with session_scope() as s:
        s.add(
            InsuranceBracketsStaging(
                effective_year=2027,
                composed_from={},
                brackets=[],
                rates={},
                diff_summary={"added": [], "removed": [], "modified": []},
            )
        )
        s.flush()
    resp = client.get("/api/gov-data/staging")
    assert resp.status_code == 200
    body = resp.json()
    assert "sources" in body
    assert "brackets_pending" in body
    assert any(b["effective_year"] == 2027 for b in body["brackets_pending"])


def test_promote_brackets_endpoint(client, pending_brackets_staging):
    resp = client.post(
        f"/api/gov-data/staging/brackets/{pending_brackets_staging}/promote",
        json={"reason": "2027 政府公告比對無異常套用"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "promoted"


def test_promote_reason_too_short_400(client, pending_brackets_staging):
    resp = client.post(
        f"/api/gov-data/staging/brackets/{pending_brackets_staging}/promote",
        json={"reason": "短"},
    )
    # Pydantic min_length=10 validates before the route handler → 422；
    # promoter._validate_reason (len < 10 after strip) → 400。兩者皆為「輸入不合規」。
    assert resp.status_code in (400, 422)


def test_promote_idempotent_409(client, pending_brackets_staging):
    body = {"reason": "2027 政府公告比對無異常套用"}
    r1 = client.post(
        f"/api/gov-data/staging/brackets/{pending_brackets_staging}/promote", json=body
    )
    assert r1.status_code == 200
    r2 = client.post(
        f"/api/gov-data/staging/brackets/{pending_brackets_staging}/promote", json=body
    )
    assert r2.status_code == 409


def test_dismiss_brackets_endpoint(client, pending_brackets_staging):
    resp = client.post(
        f"/api/gov-data/staging/brackets/{pending_brackets_staging}/dismiss",
        json={"reason": "格式異常先忽略，等下次更新"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


def test_get_brackets_diff(client, test_db_session):
    with session_scope() as s:
        st = InsuranceBracketsStaging(
            effective_year=2028,
            composed_from={},
            brackets=[{"amount": 30000}],
            rates={"x": 1},
            diff_summary={"added": [], "removed": [], "modified": []},
        )
        s.add(st)
        s.flush()
        sid = st.id
    resp = client.get(f"/api/gov-data/staging/brackets/{sid}/diff")
    assert resp.status_code == 200
    assert resp.json()["effective_year"] == 2028


def test_sync_now_endpoint(client, monkeypatch):
    called = {"v": False}

    def fake_sync_now():
        called["v"] = True
        return {
            "snapshot_ids": {},
            "brackets_staging_id": None,
            "minimum_wage_staging_id": None,
        }

    from services import gov_data_scheduler

    monkeypatch.setattr(gov_data_scheduler, "sync_now", fake_sync_now)

    resp = client.post("/api/gov-data/sync-now")
    assert resp.status_code == 200
    assert called["v"] is True


def test_snapshots_endpoint(client, test_db_session):
    with session_scope() as s:
        s.add(
            GovDataSnapshot(
                source="mol_labor_brackets",
                source_url="x",
                http_status=200,
                payload_hash="d" * 64,
                raw_payload={},
            )
        )
        s.flush()
    resp = client.get("/api/gov-data/snapshots?source=mol_labor_brackets&limit=10")
    assert resp.status_code == 200
    assert any(sn["source"] == "mol_labor_brackets" for sn in resp.json())
