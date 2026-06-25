"""宣告式稽核 Depends(audit_entity(...)) 行為驗證（設計審查 2026-06-25 主題 A MID-1）。

path-pattern 推導是 opt-out 反模式（新增 router 漏配 ENTITY_PATTERNS 即靜默零稽核）。
宣告式 audit_entity dependency 讓端點以 request.state 覆寫 entity_type，AuditMiddleware
在 call_next 後 resolve（request.state 優先、path-pattern fallback）。
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from utils.audit import AuditMiddleware, audit_entity


def test_audit_entity_dependency_sets_request_state():
    """audit_entity(X) 回傳的 dependency 被呼叫時把 request.state.audit_entity_type 設為 X。"""
    dep = audit_entity("my_entity")

    class _State:
        pass

    class _Req:
        state = _State()

    req = _Req()
    dep(req)
    assert req.state.audit_entity_type == "my_entity"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)

    @app.post("/widget", dependencies=[Depends(audit_entity("widget"))])
    def _create_widget():
        return {"ok": True}

    @app.post("/unmarked")
    def _create_unmarked():
        return {"ok": True}

    return app


def test_declarative_route_audited_with_entity_type(monkeypatch):
    """掛 Depends(audit_entity("widget")) 的 POST 端點：middleware 以 entity_type=
    'widget' 落 audit payload（證明 request.state 覆寫經 call_next 後被 resolve）。"""
    captured: dict = {}
    monkeypatch.setattr(
        "utils.audit._schedule_audit_write", lambda payload: captured.update(payload)
    )
    client = TestClient(_build_app())
    r = client.post("/widget")
    assert r.status_code == 200
    assert captured.get("entity_type") == "widget"


def test_unmarked_route_not_audited(monkeypatch):
    """未掛 audit_entity 且不匹配 ENTITY_PATTERNS 的合成端點 → 不落 audit
    （middleware resolve 後 entity_type 為 None，_schedule_audit_write 不被呼叫）。"""
    called: list = []
    monkeypatch.setattr(
        "utils.audit._schedule_audit_write", lambda payload: called.append(payload)
    )
    client = TestClient(_build_app())
    r = client.post("/unmarked")
    assert r.status_code == 200
    assert called == []
