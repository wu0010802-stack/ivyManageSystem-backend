"""驗證 /health/ready 不外洩 env 欄位給未認證請求。

威脅：原本 readiness probe 回傳 `env: "production"`，無認證即可查；
攻擊者得知部署環境後可針對性設計 payload。

修法：response 不再帶 env 欄位；env 改在啟動 log 一次性紀錄供 SRE 檢查。

Refs: 資安掃描 2026-05-07 P1。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.health import router as health_router


@pytest.fixture
def health_client():
    app = FastAPI()
    app.include_router(health_router)
    with TestClient(app) as client:
        yield client


class TestReadinessNoEnvLeak:
    def test_ready_response_does_not_include_env(self, health_client):
        """成功路徑不應包含 env 欄位"""
        res = health_client.get("/health/ready")
        # 200 (DB 連得到) 或 503 (DB 連不到) 都接受；重點是不能有 env
        assert res.status_code in (200, 503)
        body = res.json()
        assert "env" not in body, f"/health/ready response 不可外洩 env：{body}"

    def test_live_endpoint_does_not_include_env(self, health_client):
        """liveness 同樣不應外洩 env"""
        res = health_client.get("/health/live")
        assert res.status_code == 200
        assert "env" not in res.json()
