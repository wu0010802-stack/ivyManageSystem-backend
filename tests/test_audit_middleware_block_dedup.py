"""驗證 AuditMiddleware 對 401/403 重複請求做 dedup（同 ip+method+path 60s 內只記一筆）。

威脅：401/403 補登（2026-05-07 P1）讓攻擊偵測成形，但同時開了「灌爆 audit_logs」
的攻擊面：未授權者向受保護端點猛轟，每次都寫 BLOCKED_* 進 DB。

修法：_should_audit_block(ip, method, path) 用 in-memory cache + 60 秒視窗
做 dedup；超出 1000 條時 opportunistic cleanup。

Refs: 資安掃描 2026-05-07 P1。
"""

import os
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.audit import AuditMiddleware


def _build_app(captured_writes):
    from utils import audit as audit_module

    original_schedule = audit_module._schedule_audit_write
    audit_module._schedule_audit_write = lambda payload: captured_writes.append(payload)
    # 清空 dedup cache 確保 test 之間隔離
    audit_module._audit_block_cache.clear()

    app = FastAPI()
    app.add_middleware(AuditMiddleware)

    @app.put("/api/students/{sid}")
    def students_update_403(sid: int):
        raise HTTPException(status_code=403, detail="禁止")

    @app.delete("/api/students/{sid}")
    def students_delete_401(sid: int):
        raise HTTPException(status_code=401, detail="未登入")

    return app, lambda: (
        setattr(audit_module, "_schedule_audit_write", original_schedule),
        audit_module._audit_block_cache.clear(),
    )


@pytest.fixture
def app_setup():
    captured: list = []
    app, restore = _build_app(captured)
    yield app, captured
    restore()


@pytest.fixture
def client(app_setup):
    app, _ = app_setup
    with TestClient(app) as c:
        yield c


class TestAuditBlockDedup:
    def test_repeated_403_same_path_logs_once(self, app_setup, client):
        _, captured = app_setup
        captured.clear()

        for _ in range(5):
            res = client.put("/api/students/123")
            assert res.status_code == 403

        # 5 個請求只應落 1 筆 audit
        block_writes = [w for w in captured if w["action"].startswith("BLOCKED_")]
        assert len(block_writes) == 1
        assert block_writes[0]["action"] == "BLOCKED_UPDATE"

    def test_different_paths_log_separately(self, app_setup, client):
        """不同 path 不互相影響"""
        _, captured = app_setup
        captured.clear()

        client.put("/api/students/100")  # 403
        client.put("/api/students/200")  # 403（不同 path）
        client.delete("/api/students/100")  # 401（不同 method）

        block_writes = [w for w in captured if w["action"].startswith("BLOCKED_")]
        assert len(block_writes) == 3, f"expected 3, got: {block_writes}"

    def test_dedup_window_first_request_logs(self, app_setup, client):
        _, captured = app_setup
        captured.clear()

        res1 = client.put("/api/students/777")
        assert res1.status_code == 403
        block_writes_1 = [w for w in captured if w["action"].startswith("BLOCKED_")]
        assert len(block_writes_1) == 1

        # 立即第二次 → dedup 跳過
        captured.clear()
        res2 = client.put("/api/students/777")
        assert res2.status_code == 403  # endpoint 仍回 403（dedup 不影響 response）
        block_writes_2 = [w for w in captured if w["action"].startswith("BLOCKED_")]
        assert len(block_writes_2) == 0

    def test_dedup_helper_independent_keys(self):
        """直接驗證 helper：(ip, method, path) 三個維度任一不同就視為新 key"""
        from utils.audit import _should_audit_block, _audit_block_cache

        _audit_block_cache.clear()

        assert _should_audit_block("1.1.1.1", "PUT", "/x") is True
        # 第二次同 key → False
        assert _should_audit_block("1.1.1.1", "PUT", "/x") is False
        # 不同 ip
        assert _should_audit_block("2.2.2.2", "PUT", "/x") is True
        # 不同 method
        assert _should_audit_block("1.1.1.1", "DELETE", "/x") is True
        # 不同 path
        assert _should_audit_block("1.1.1.1", "PUT", "/y") is True

    def test_anon_ip_dedups(self):
        """request.client.host 為 None 時用 'anon' 為 key 做 dedup"""
        from utils.audit import _should_audit_block, _audit_block_cache

        _audit_block_cache.clear()
        assert _should_audit_block(None, "PUT", "/x") is True
        assert _should_audit_block(None, "PUT", "/x") is False
