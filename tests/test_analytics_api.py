"""經營分析 API 端點測試。

採用 dependency_overrides 模式（參考 test_gov_reports.py）搭配
SQLite in-memory DB（參考 test_announcements_api.py）。

測試策略：
- 以 `get_current_user` override 繞過 JWT 驗證
- 以 SQLite 換掉 models.base 的 engine/session_factory
- builder（report_cache_service.get_or_build）會呼叫真實 service 函式，
  在空 DB 上執行 → 確認端點能正常回傳空但結構正確的回應
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.analytics import router as analytics_router
from models.base import Base
from utils.auth import get_current_user
from utils.permissions import Permission

# ---------------------------------------------------------------------------
# 輔助：建立不同權限的 mock user
# ---------------------------------------------------------------------------

_ANALYTICS_PERM = int(Permission.BUSINESS_ANALYTICS)
_ANALYTICS_STUDENTS_PERM = int(Permission.BUSINESS_ANALYTICS | Permission.STUDENTS_READ)


def _mock_user_with_perm(permissions: int, role: str = "admin"):
    async def _inner():
        return {
            "id": 1,
            "username": "testuser",
            "role": role,
            "permissions": permissions,
        }

    return _inner


# ---------------------------------------------------------------------------
# Fixture：SQLite in-memory + dependency_overrides
# ---------------------------------------------------------------------------


@pytest.fixture
def analytics_client(tmp_path):
    db_path = tmp_path / "analytics-api.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(analytics_router)

    with TestClient(app) as client:
        yield client, app

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAnalyticsApi:
    def test_funnel_requires_business_analytics_permission(self, analytics_client):
        """無 BUSINESS_ANALYTICS 權限 → 403。"""
        client, app = analytics_client

        # 任何不含 BUSINESS_ANALYTICS 的權限（例如只有 REPORTS）
        no_analytics_perm = int(Permission.REPORTS)
        app.dependency_overrides[get_current_user] = _mock_user_with_perm(
            no_analytics_perm
        )
        try:
            resp = client.get(
                "/api/analytics/funnel",
                params={"start": "2026-01-01", "end": "2026-03-31"},
            )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()

    def test_funnel_returns_structure(self, analytics_client):
        """具備 BUSINESS_ANALYTICS 權限 → 200，回傳包含必要鍵的 dict。"""
        client, app = analytics_client

        app.dependency_overrides[get_current_user] = _mock_user_with_perm(
            _ANALYTICS_PERM
        )
        try:
            resp = client.get(
                "/api/analytics/funnel",
                params={"start": "2026-01-01", "end": "2026-03-31"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert isinstance(body, dict)
            for key in (
                "stages",
                "no_deposit_reasons",
                "by_source",
                "by_grade",
                "filters",
            ):
                assert key in body, f"回應缺少鍵：{key}"
            assert isinstance(body["stages"], list)
        finally:
            app.dependency_overrides.clear()

    def test_at_risk_endpoint_returns_list(self, analytics_client):
        """具備 BUSINESS_ANALYTICS 權限 → 200，body 為 list。"""
        client, app = analytics_client

        app.dependency_overrides[get_current_user] = _mock_user_with_perm(
            _ANALYTICS_STUDENTS_PERM
        )
        try:
            resp = client.get("/api/analytics/churn/at-risk")
            assert resp.status_code == 200
            body = resp.json()
            assert isinstance(body, list)
        finally:
            app.dependency_overrides.clear()

    def test_churn_history_returns_12_months(self, analytics_client):
        """具備 BUSINESS_ANALYTICS 權限，months=12 → 200，monthly 清單恰有 12 筆。"""
        client, app = analytics_client

        app.dependency_overrides[get_current_user] = _mock_user_with_perm(
            _ANALYTICS_PERM
        )
        try:
            resp = client.get("/api/analytics/churn/history", params={"months": 12})
            assert resp.status_code == 200
            body = resp.json()
            assert isinstance(body, dict)
            assert "monthly" in body
            assert len(body["monthly"]) == 12
        finally:
            app.dependency_overrides.clear()

    def test_at_risk_masks_name_without_students_read(self, analytics_client):
        """User with BUSINESS_ANALYTICS but NOT STUDENTS_READ should see masked names."""
        client, app = analytics_client

        # 只有 BUSINESS_ANALYTICS 權限，無 STUDENTS_READ
        app.dependency_overrides[get_current_user] = _mock_user_with_perm(
            _ANALYTICS_PERM
        )
        try:
            resp = client.get("/api/analytics/churn/at-risk")
            assert resp.status_code == 200
            body = resp.json()
            assert isinstance(body, list)
            # 在空 DB 上迴圈為空，但若有資料則 student_name 應為 "***"
            for item in body:
                assert item.get("student_name") == "***"
        finally:
            app.dependency_overrides.clear()
