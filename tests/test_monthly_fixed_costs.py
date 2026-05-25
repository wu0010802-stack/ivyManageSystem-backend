"""monthly_fixed_costs CRUD endpoint 測試。

對齊 test_monthly_pnl.py 的 fixture pattern（SQLite in-memory + 完整 login flow），
涵蓋：權限、單筆 upsert、批次 upsert（atomic + 重複鍵）、刪除、audit state、
cache invalidation。
"""

import os
import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import (
    router as auth_router,
    _account_failures,
    _ip_attempts,
)
from api.monthly_fixed_costs import router as fixed_costs_router
from models.database import Base, User
from models.monthly_fixed_cost import MonthlyFixedCost
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "fixed_cost.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(fixed_costs_router)
    with TestClient(app) as c:
        yield c, session_factory
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _login(c, sf, *, permission_names=["*"]):
    if isinstance(permission_names, str):
        permission_names = [permission_names]
    with sf() as s:
        s.add(
            User(
                username="fc_admin",
                password_hash=hash_password("FcPass123"),
                role="admin",
                permission_names=permission_names,
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
    r = c.post(
        "/api/auth/login", json={"username": "fc_admin", "password": "FcPass123"}
    )
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────
# Model 層測試
# ─────────────────────────────────────────────────────────────────────────


class TestMonthlyFixedCostModel:
    def test_unique_constraint_year_month_category(self, client):
        _, sf = client
        with sf() as s:
            s.add(MonthlyFixedCost(year=2026, month=3, category="rent", amount=500000))
            s.commit()
        with sf() as s, pytest.raises(IntegrityError):
            s.add(MonthlyFixedCost(year=2026, month=3, category="rent", amount=600000))
            s.commit()

    def test_check_constraint_month_range(self, client):
        _, sf = client
        with sf() as s, pytest.raises(IntegrityError):
            s.add(MonthlyFixedCost(year=2026, month=13, category="rent", amount=100))
            s.commit()


# ─────────────────────────────────────────────────────────────────────────
# Endpoint 層測試
# ─────────────────────────────────────────────────────────────────────────


class TestListEndpoint:
    def test_requires_auth(self, client):
        c, _ = client
        r = c.get("/api/monthly-fixed-costs?year=2026")
        assert r.status_code in (401, 403)

    def test_returns_empty_for_year_with_no_data(self, client):
        c, sf = client
        _login(c, sf)
        r = c.get("/api/monthly-fixed-costs?year=2026")
        assert r.status_code == 200
        data = r.json()
        assert data["year"] == 2026
        assert data["items"] == []
        assert "valid_categories" in data
        assert "rent" in data["valid_categories"]

    def test_filters_by_year_and_sorts(self, client):
        c, sf = client
        _login(c, sf)
        with sf() as s:
            s.add_all(
                [
                    MonthlyFixedCost(year=2026, month=3, category="water", amount=5989),
                    MonthlyFixedCost(
                        year=2026, month=1, category="rent", amount=500000
                    ),
                    MonthlyFixedCost(
                        year=2025, month=3, category="rent", amount=400000
                    ),
                ]
            )
            s.commit()
        r = c.get("/api/monthly-fixed-costs?year=2026")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 2
        # 排序：month asc, category asc
        assert items[0]["month"] == 1 and items[0]["category"] == "rent"
        assert items[1]["month"] == 3 and items[1]["category"] == "water"


class TestUpsertEndpoint:
    def test_create_new_entry(self, client):
        c, sf = client
        _login(c, sf)
        with patch(
            "api.monthly_fixed_costs.invalidate_finance_summary_cache"
        ) as mock_inv:
            r = c.put(
                "/api/monthly-fixed-costs",
                json={
                    "year": 2026,
                    "month": 3,
                    "category": "rent",
                    "amount": "500000.00",
                    "notes": "test",
                },
            )
        assert r.status_code == 200
        body = r.json()
        assert body["message"] == "更新成功"
        assert "id" in body
        assert body["item"]["amount"] == 500000.0
        assert body["item"]["notes"] == "test"
        # cache invalidation triggered
        mock_inv.assert_called_once()

    def test_update_existing_entry_by_period_category(self, client):
        c, sf = client
        _login(c, sf)
        # 先建一筆
        c.put(
            "/api/monthly-fixed-costs",
            json={
                "year": 2026,
                "month": 3,
                "category": "rent",
                "amount": "500000.00",
            },
        )
        # 同 (year, month, category) 第二次 PUT → upsert update
        r = c.put(
            "/api/monthly-fixed-costs",
            json={
                "year": 2026,
                "month": 3,
                "category": "rent",
                "amount": "550000.00",
                "notes": "adjusted",
            },
        )
        assert r.status_code == 200
        with sf() as s:
            rows = s.query(MonthlyFixedCost).all()
            assert len(rows) == 1  # upsert 沒新增第二筆
            assert float(rows[0].amount) == 550000.0
            assert rows[0].notes == "adjusted"

    def test_rejects_invalid_category(self, client):
        c, sf = client
        _login(c, sf)
        r = c.put(
            "/api/monthly-fixed-costs",
            json={
                "year": 2026,
                "month": 3,
                "category": "bogus_category",
                "amount": "100.00",
            },
        )
        assert r.status_code == 422  # Pydantic Literal validator

    def test_rejects_negative_amount(self, client):
        c, sf = client
        _login(c, sf)
        r = c.put(
            "/api/monthly-fixed-costs",
            json={
                "year": 2026,
                "month": 3,
                "category": "rent",
                "amount": "-100.00",
            },
        )
        assert r.status_code == 422

    def test_requires_write_permission(self, client):
        """登入但無 VENDOR_PAYMENT_WRITE permission 不可寫。"""
        c, sf = client
        _login(c, sf, permission_names=[])  # 無權限
        r = c.put(
            "/api/monthly-fixed-costs",
            json={
                "year": 2026,
                "month": 3,
                "category": "rent",
                "amount": "100.00",
            },
        )
        assert r.status_code == 403


class TestBatchEndpoint:
    def test_batch_create_multiple(self, client):
        c, sf = client
        _login(c, sf)
        with patch(
            "api.monthly_fixed_costs.invalidate_finance_summary_cache"
        ) as mock_inv:
            r = c.put(
                "/api/monthly-fixed-costs/batch",
                json={
                    "year": 2026,
                    "entries": [
                        {"month": 1, "category": "rent", "amount": "500000.00"},
                        {"month": 1, "category": "water", "amount": "5989.00"},
                        {"month": 2, "category": "rent", "amount": "500000.00"},
                    ],
                },
            )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 3
        assert len(body["ids"]) == 3
        # cache 只 invalidate 一次（整批共享）
        mock_inv.assert_called_once()
        with sf() as s:
            assert s.query(MonthlyFixedCost).count() == 3

    def test_batch_rejects_duplicate_keys(self, client):
        """同一批送兩筆相同 (month, category) → 400。"""
        c, sf = client
        _login(c, sf)
        r = c.put(
            "/api/monthly-fixed-costs/batch",
            json={
                "year": 2026,
                "entries": [
                    {"month": 1, "category": "rent", "amount": "500000.00"},
                    {"month": 1, "category": "rent", "amount": "600000.00"},
                ],
            },
        )
        assert r.status_code == 400
        assert "重複" in r.json()["detail"]
        with sf() as s:
            assert s.query(MonthlyFixedCost).count() == 0  # 整批未寫入

    def test_batch_mixed_create_and_update(self, client):
        c, sf = client
        _login(c, sf)
        # 先建 1 筆
        c.put(
            "/api/monthly-fixed-costs",
            json={
                "year": 2026,
                "month": 1,
                "category": "rent",
                "amount": "500000.00",
            },
        )
        # batch：1 筆 update + 1 筆 create
        r = c.put(
            "/api/monthly-fixed-costs/batch",
            json={
                "year": 2026,
                "entries": [
                    {"month": 1, "category": "rent", "amount": "550000.00"},
                    {"month": 1, "category": "water", "amount": "5989.00"},
                ],
            },
        )
        assert r.status_code == 200
        with sf() as s:
            rows = s.query(MonthlyFixedCost).all()
            assert len(rows) == 2  # 不是 3


class TestDeleteEndpoint:
    def test_delete_existing(self, client):
        c, sf = client
        _login(c, sf)
        r1 = c.put(
            "/api/monthly-fixed-costs",
            json={
                "year": 2026,
                "month": 3,
                "category": "rent",
                "amount": "500000.00",
            },
        )
        cost_id = r1.json()["id"]
        with patch(
            "api.monthly_fixed_costs.invalidate_finance_summary_cache"
        ) as mock_inv:
            r = c.delete(f"/api/monthly-fixed-costs/{cost_id}")
        assert r.status_code == 200
        assert r.json()["message"] == "刪除成功"
        mock_inv.assert_called_once()
        with sf() as s:
            assert s.query(MonthlyFixedCost).count() == 0

    def test_delete_404_when_not_found(self, client):
        c, sf = client
        _login(c, sf)
        r = c.delete("/api/monthly-fixed-costs/99999")
        assert r.status_code == 404
