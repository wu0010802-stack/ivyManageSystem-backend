"""驗證 AuditMiddleware 主路徑（dispatch）對 request.state.audit_changes 套 redact_pii。

P0b：audit 表比主表更危險（DB dump 外洩時要遮 PII at rest）。三個顯式 helper
（write_audit_in_session / write_explicit_audit / write_login_audit）都先 redact_pii
再序列化，但 middleware 主路徑原本直接 json.dumps(changes_raw) → 任一 endpoint 漏遮
PII 欄位就明文落 audit_logs.changes。實例：api/activity/registrations.py 設
audit_changes={"student_name": ...}（student_name 命中 denylist）。
"""

import json
import os
import sys

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.audit import AuditMiddleware  # noqa: E402


@pytest.fixture
def captured():
    return []


@pytest.fixture
def client(captured):
    """極簡 app + AuditMiddleware；攔 _schedule_audit_write 收 payload（不依賴 DB）。"""
    from utils import audit as audit_module

    original = audit_module._schedule_audit_write
    audit_module._schedule_audit_write = lambda payload: captured.append(payload)

    app = FastAPI()
    app.add_middleware(AuditMiddleware)

    @app.post("/api/students/with-pii")
    def students_with_pii(request: Request):
        request.state.audit_changes = {
            "student_name": "王小明",
            "amount": 100,
            "note": "ok",
        }
        return {"ok": True}

    with TestClient(app) as c:
        yield c

    audit_module._schedule_audit_write = original


def test_middleware_dispatch_redacts_pii_in_changes(client, captured):
    """middleware 主路徑寫入的 changes 必須已遮 PII（student_name），但保留金流/一般欄位。"""
    captured.clear()
    res = client.post("/api/students/with-pii")
    assert res.status_code == 200
    assert len(captured) == 1, captured
    changes = json.loads(captured[0]["changes"])
    assert changes["student_name"] == "[Filtered]"  # PII denylist 命中 → 遮罩
    assert changes["amount"] == 100  # 金流欄位保留
    assert changes["note"] == "ok"  # 一般欄位保留
