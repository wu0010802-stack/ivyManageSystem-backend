"""main.py KillSwitchMiddleware 整合測試。

確認 middleware 已在 main.app 的 stack 中：
- maintenance/read-only 旗開時，非 bypass 端點 503
- bypass 端點 /health/live 仍可達
- middleware 順序正確：KillSwitchMiddleware 在 AuditMiddleware 之後 add
  → 成為 Audit 的外層 wrapper（執行順序在 Audit 之前）→ 503 不寫 audit log
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient


def _client(monkeypatch, **env):
    """設定 env 後 import main 並回 TestClient（不啟 lifespan，避免 DB 依賴）。

    main.app 是 module-level singleton，多 test 共用同一 instance；middleware
    在 dispatch 期間透過 get_settings() 動態取 env，無需 reload。
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from config import reset_for_tests

    reset_for_tests()
    from main import app

    return TestClient(app)


def test_kill_switch_middleware_registered_in_main_stack():
    """掃 main.app.user_middleware 確認 KillSwitchMiddleware 已註冊。"""
    from main import app
    from utils.kill_switch import KillSwitchMiddleware

    middleware_classes = [m.cls for m in app.user_middleware]
    assert KillSwitchMiddleware in middleware_classes, (
        f"KillSwitchMiddleware 未在 main.app middleware stack 中。"
        f"現有 stack: {[c.__name__ for c in middleware_classes]}"
    )


def test_kill_switch_after_audit_middleware():
    """KillSwitchMiddleware 必須在 AuditMiddleware 之後 add（list 中索引較大）。

    starlette/FastAPI 中後 add 的 middleware 在外層，先執行；因此
    KillSwitch 先於 Audit 執行 → maintenance 503 不會寫 audit log。
    """
    from main import app
    from utils.audit import AuditMiddleware
    from utils.kill_switch import KillSwitchMiddleware

    classes = [m.cls for m in app.user_middleware]
    assert KillSwitchMiddleware in classes
    assert AuditMiddleware in classes
    # 後 add 的 index 較小（FastAPI add_middleware 是 insert(0)）
    # 我們要：KillSwitch.index < Audit.index → KillSwitch 在 stack 外層 → 先執行
    idx_kill = classes.index(KillSwitchMiddleware)
    idx_audit = classes.index(AuditMiddleware)
    assert idx_kill < idx_audit, (
        f"KillSwitchMiddleware (idx={idx_kill}) 應在 AuditMiddleware (idx={idx_audit}) "
        f"之外層（idx 更小）—— main.py 註冊順序錯誤。"
    )


def test_maintenance_blocks_non_bypass_endpoint(monkeypatch):
    """啟用 MAINTENANCE_MODE 後 /api/employees 回 503 MAINTENANCE_MODE。

    用 GET 而非 POST，避開 AuditMiddleware DB 寫入（middleware 順序 KillSwitch
    在 Audit 外層，503 不會 propagate 到 Audit；但安全起見用 GET 還是少踩雷）。
    """
    client = _client(monkeypatch, MAINTENANCE_MODE="1", MAINTENANCE_MESSAGE="升級中")
    r = client.get("/api/employees")
    assert r.status_code == 503, r.text
    payload = r.json()
    assert payload["detail"]["code"] == "MAINTENANCE_MODE"
    assert payload["detail"]["message"] == "升級中"
    assert payload["detail"]["retry_after"] == 300
    assert r.headers["retry-after"] == "300"


def test_maintenance_503_includes_cors_header(monkeypatch):
    """維護 503 短路回應必須帶 CORS header。

    回歸：CORSMiddleware 原本最先 add（最內層），KillSwitch 503 在其外層短路，
    回應不經過 CORS → 缺 Access-Control-Allow-Origin → 跨來源前端收到 CORS error
    而非可讀的 503，打斷 MaintenanceView / 503-redirect 友善降級。
    CORS 移到 KillSwitch/CSRF 外層後，503 回流經過 CORS → 帶 header。
    """
    from starlette.middleware.cors import CORSMiddleware

    client = _client(monkeypatch, MAINTENANCE_MODE="1")
    # 從 live middleware stack 取實際允許的 origin（不假設 config 值）
    allowed: list[str] = []
    for m in client.app.user_middleware:
        if m.cls is CORSMiddleware:
            allowed = list((getattr(m, "kwargs", {}) or {}).get("allow_origins") or [])
            break
    assert allowed, "main.app 找不到 CORSMiddleware 的 allow_origins"
    origin = allowed[0]

    r = client.get("/api/employees", headers={"Origin": origin})
    assert r.status_code == 503, r.text
    assert (
        r.headers.get("access-control-allow-origin") == origin
    ), f"維護 503 缺 CORS header（CORS middleware 順序錯誤）；headers={dict(r.headers)}"


def test_maintenance_bypasses_health_live(monkeypatch):
    """MAINTENANCE_MODE 期間 /health/live 仍可達（UptimeRobot 監控不中斷）。"""
    client = _client(monkeypatch, MAINTENANCE_MODE="1")
    r = client.get("/health/live")
    assert r.status_code == 200, r.text


def test_maintenance_bypasses_health_ready(monkeypatch):
    client = _client(monkeypatch, MAINTENANCE_MODE="1")
    r = client.get("/health/ready")
    # /health/ready 可能因 DB 未連回 503/500，但不該是「我們的」MAINTENANCE_MODE 503
    if r.status_code == 503:
        # 確認不是 KillSwitch 擋的
        payload = r.json()
        detail = payload.get("detail")
        if isinstance(detail, dict):
            assert (
                detail.get("code") != "MAINTENANCE_MODE"
            ), "MAINTENANCE_MODE 不該擋 /health/ready bypass 路徑"


def test_read_only_blocks_post_to_non_bypass(monkeypatch):
    """READ_ONLY_MODE 期間對非 bypass endpoint 的 POST 回 503 READ_ONLY_MODE。

    用 /api/employees POST（即便 auth 會擋，KillSwitch 在 Audit 外層更早執行
    回 503，根本到不了 auth）。
    """
    client = _client(monkeypatch, READ_ONLY_MODE="1")
    r = client.post("/api/employees", json={})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "READ_ONLY_MODE"


def test_read_only_allows_get_to_non_bypass(monkeypatch):
    """READ_ONLY_MODE 期間 GET 不該被 KillSwitch 擋（雖可能因 auth 401，但不是 503 READ_ONLY_MODE）。"""
    client = _client(monkeypatch, READ_ONLY_MODE="1")
    r = client.get("/api/employees")
    # 不該是 KillSwitch 的 READ_ONLY_MODE 503；可能 200/401/403 都行
    if r.status_code == 503:
        detail = r.json().get("detail")
        if isinstance(detail, dict):
            assert detail.get("code") != "READ_ONLY_MODE"


def test_normal_mode_does_not_intercept(monkeypatch):
    """無 maintenance / read_only 旗時 /health/live 走原路徑。"""
    monkeypatch.delenv("MAINTENANCE_MODE", raising=False)
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    from main import app

    client = TestClient(app)
    r = client.get("/health/live")
    assert r.status_code == 200
