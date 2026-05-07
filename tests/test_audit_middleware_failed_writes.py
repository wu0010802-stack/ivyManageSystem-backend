"""驗證 AuditMiddleware 對 401/403 失敗寫入請求補登 audit。

audit 2026-05-07 P1：原本 middleware 只 audit 2xx → 失敗的攻擊嘗試
（未登入越權、權限不足）一律無紀錄，無法做 brute-force / 異常偵測。

修補：
- 401 / 403 → audit action=BLOCKED_<CREATE|UPDATE|DELETE>，summary 含
  ⚠ 標記
- 其他 4xx/5xx（400/404/409/422/5xx）不 audit（避免量爆）
- 2xx 行為不變
"""

import os
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import AuditLog, Base
from utils.audit import AuditMiddleware


def _build_app(captured_writes):
    """建立極簡 FastAPI 含 AuditMiddleware；對 _schedule_audit_write 攔截
    收集寫入的 payload 給 assertion 用（避免依賴 DB engine 寫入）。"""
    from utils import audit as audit_module

    original_schedule = audit_module._schedule_audit_write

    def fake_schedule(payload):
        captured_writes.append(payload)

    audit_module._schedule_audit_write = fake_schedule

    app = FastAPI()
    app.add_middleware(AuditMiddleware)

    @app.post("/api/students/abc")
    def students_create_ok():
        return {"ok": True}

    @app.put("/api/students/{sid}")
    def students_update_403(sid: int):
        raise HTTPException(status_code=403, detail="禁止")

    @app.delete("/api/students/{sid}")
    def students_delete_401(sid: int):
        raise HTTPException(status_code=401, detail="未登入")

    @app.post("/api/students/notfound")
    def students_404():
        raise HTTPException(status_code=404, detail="不存在")

    @app.put("/api/students/9001/badreq")
    def students_400():
        raise HTTPException(status_code=400, detail="輸入錯")

    @app.delete("/api/students/9002/conflict")
    def students_409():
        raise HTTPException(status_code=409, detail="衝突")

    @app.post("/api/students/internal")
    def students_500():
        raise HTTPException(status_code=500, detail="boom")

    return app, lambda: setattr(
        audit_module, "_schedule_audit_write", original_schedule
    )


@pytest.fixture
def captured():
    return []


@pytest.fixture
def app(captured):
    app, restore = _build_app(captured)
    yield app
    restore()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


class TestAuditFailedWrites:
    def test_201_creates_audit_with_create_action(self, client, captured):
        """2xx 仍走原本邏輯。"""
        captured.clear()
        res = client.post("/api/students/abc")
        assert res.status_code == 200
        assert len(captured) == 1
        assert captured[0]["action"] == "CREATE"
        assert captured[0]["entity_type"] == "student"

    def test_403_records_blocked_update(self, client, captured):
        """403 寫入嘗試 → BLOCKED_UPDATE。"""
        captured.clear()
        res = client.put("/api/students/42")
        assert res.status_code == 403
        assert len(captured) == 1
        assert captured[0]["action"] == "BLOCKED_UPDATE"
        assert "⚠" in captured[0]["summary"]
        assert "403" in captured[0]["summary"]

    def test_401_records_blocked_delete(self, client, captured):
        """401 同樣記下 BLOCKED_DELETE（攻擊者沒帶 cookie 嘗試）。"""
        captured.clear()
        res = client.delete("/api/students/42")
        assert res.status_code == 401
        assert len(captured) == 1
        assert captured[0]["action"] == "BLOCKED_DELETE"

    def test_404_does_not_audit(self, client, captured):
        """404 不算攻擊嘗試（用戶輸入錯）→ 不 audit 避免量爆。"""
        captured.clear()
        res = client.post("/api/students/notfound")
        assert res.status_code == 404
        assert len(captured) == 0

    def test_400_does_not_audit(self, client, captured):
        captured.clear()
        res = client.put("/api/students/9001/badreq")
        assert res.status_code == 400
        assert len(captured) == 0

    def test_409_does_not_audit(self, client, captured):
        captured.clear()
        res = client.delete("/api/students/9002/conflict")
        assert res.status_code == 409
        assert len(captured) == 0

    def test_500_does_not_audit(self, client, captured):
        """5xx internal error 自有 log，不 audit。"""
        captured.clear()
        try:
            res = client.post("/api/students/internal")
        except HTTPException:
            pass
        # FastAPI HTTPException(500) 會被 middleware 處理成 500 response
        assert len(captured) == 0
