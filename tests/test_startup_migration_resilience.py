"""對標稽核 P1：cold-start migration 韌性（boot-loop 止血）。

風險：``main.py`` ``app_lifespan`` 在 yield 前同步呼叫 ``on_startup()``，其第一步
``run_alembic_upgrade()`` 原本無 try/except → 任一 migration 在 prod 失敗 → lifespan
例外 → uvicorn 退出 → 平台重啟 → 同一壞 migration **無限 boot-loop**，全站 down 且
無法自救（push origin/main 即自動部署 + 單實例，無法回滾啟動）。

止血語意（刻意**不是**「半套 schema 唯讀續服務」——那同樣危險）：壞 migration 時
app 仍正常啟動但進「維護模式」——
- ``on_startup`` 失敗不 raise、回 ``False``、跳過後續 bootstrap/seed（壞 schema 上必失敗）；
- ``app_lifespan`` 據此略過排程/RLS，yield 後 return；
- ``/health/ready`` 回 503（讓 LB / UptimeRobot 看得到不健康）；
- ``KillSwitchMiddleware`` 對業務路由回 503 ``MAINTENANCE_MODE``（不在壞 schema 上服務真流量），
  但 BYPASS_PATHS（/health/*, /api/auth/login...）仍可達，供探針與 admin 自救；
- log CRITICAL + Sentry capture（讓 ops 看到根因而非靜默重啟）。
"""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _boom():
    raise RuntimeError("alembic upgrade 失敗（模擬壞 migration）")


# ── on_startup 失敗韌性 ────────────────────────────────────────────────


def test_on_startup_returns_false_and_skips_bootstrap_when_migration_fails(
    monkeypatch, caplog
):
    import main

    monkeypatch.setattr(main, "run_alembic_upgrade", _boom)
    bootstrap_calls = []
    monkeypatch.setattr(
        main,
        "run_startup_bootstrap",
        lambda se, ls, *, insurance_service=None: bootstrap_calls.append("bootstrap"),
    )

    with caplog.at_level(logging.CRITICAL):
        result = main.on_startup()  # 關鍵：必須**不 raise**

    assert result is False
    assert bootstrap_calls == []  # 壞 schema 上不應再跑 seed/bootstrap
    assert any(rec.levelno >= logging.CRITICAL for rec in caplog.records), (
        "壞 migration 必須留下 CRITICAL log 供 ops 排查"
    )


def test_on_startup_returns_true_and_runs_bootstrap_on_success(monkeypatch):
    import main

    monkeypatch.setattr(main, "run_alembic_upgrade", lambda: None)
    bootstrap_calls = []
    monkeypatch.setattr(
        main,
        "run_startup_bootstrap",
        lambda se, ls, *, insurance_service=None: bootstrap_calls.append("bootstrap"),
    )

    result = main.on_startup()

    assert result is True
    assert bootstrap_calls == ["bootstrap"]


# ── KillSwitch 維護模式（migration 失敗自動觸發，非 env） ────────────────


def _make_killswitch_app(migration_ok):
    from utils.kill_switch import KillSwitchMiddleware

    app = FastAPI()
    app.add_middleware(KillSwitchMiddleware)
    app.state.migration_ok = migration_ok

    @app.get("/api/employees")
    def _biz():
        return {"ok": True}

    @app.get("/health/ready")
    def _ready():
        return {"status": "ok"}

    return app


def test_killswitch_blocks_business_route_when_migration_failed():
    client = TestClient(_make_killswitch_app(False))
    r = client.get("/api/employees")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "MAINTENANCE_MODE"


def test_killswitch_bypasses_health_when_migration_failed():
    client = TestClient(_make_killswitch_app(False))
    r = client.get("/health/ready")  # 在 BYPASS_PATHS → 維護模式下仍可達
    assert r.status_code == 200


def test_killswitch_passes_through_when_migration_ok():
    client = TestClient(_make_killswitch_app(True))
    r = client.get("/api/employees")
    assert r.status_code == 200


# ── /health/ready 反映 migration 失敗 ──────────────────────────────────


def _make_readiness_app(migration_ok):
    from api.health import router as health_router

    app = FastAPI()
    app.include_router(health_router)
    if migration_ok is not None:
        app.state.migration_ok = migration_ok
    return app


def test_readiness_returns_503_when_migration_failed():
    client = TestClient(_make_readiness_app(False))
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json().get("reason") == "migration_failed"
