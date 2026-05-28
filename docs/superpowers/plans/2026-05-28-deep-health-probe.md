# Deep Health Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `/health/ready` 加 `?deep=1` query param，回傳 LINE breaker / Supabase breaker / DB pool 三個 component 健康狀態。Shallow 預設行為（SELECT 1）完全不變，K8s/zeabur readiness probe 不需改 config。Deep mode **不打外網**（讀既有 P1 breaker state + LineTokenHealth daily ping row + DB pool 統計），零外部成本。

**Architecture:** Read-only 擴充 `api/health.py:readiness`。三個 `_check_*` 純函式從 `utils.circuit_breaker` / `models.integration_health` / SQLAlchemy engine pool 拿狀態，組合成 `components` dict。整體 ok 才回 200，任一 component 紅回 503。

**Tech Stack:** FastAPI、SQLAlchemy（pool 統計）、既有 P1 落地的 `LINE_BREAKER` / `SUPABASE_BREAKER` / `LineTokenHealth` / `PendingUpload`

**Spec:** `docs/superpowers/specs/2026-05-28-observability-forensic-and-design-tokens-design.md` Ch3

**前置依賴**：P1 外部整合韌性已 ship（含 `utils/circuit_breaker.py` 暴露 `LINE_BREAKER` / `SUPABASE_BREAKER` 兩 singleton 與 `.state` property、`models.integration_health.LineTokenHealth` 表、`models.pending_uploads.PendingUpload` 表）。本 plan **不**新增 migration。

---

## File Structure

| 檔案 | 動作 | 責任 |
|---|---|---|
| `api/health.py` | Modify | `readiness` 加 `deep: bool = Query(False)`；新增 3 個 module-level `_check_line` / `_check_supabase` / `_check_db_pool` |
| `tests/test_health_deep.py` | Create | 3 個 pytest（shallow 路徑不變 / deep 全綠 200 / deep LINE breaker open 503） |

---

## Task 1: shallow 路徑零變動驗證

**Files:**
- Create: `tests/test_health_deep.py`

- [ ] **Step 1: 寫 baseline pytest（shallow 行為不變）**

Create `tests/test_health_deep.py`：

```python
"""tests/test_health_deep.py — Ch3 deep /health/ready。"""

from fastapi.testclient import TestClient


def test_ready_shallow_returns_db_only(client: TestClient):
    """不帶 ?deep 預設 shallow，response.components 只含 db。"""
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # shallow 模式 components 應只有 db
    assert set(body.get("components", {}).keys()) <= {"db"}


def test_ready_shallow_does_not_query_breaker_modules():
    """shallow 路徑不應 import / 觸發 circuit_breaker 狀態查詢。

    用 monkeypatch raise on _check_line 證明不會被呼叫。
    """
    from api import health as health_module
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    with patch.object(
        health_module, "_check_line",
        side_effect=AssertionError("shallow path called _check_line!"),
    ):
        # Reimport client or use existing — shallow 不應觸發
        # 用既有 client fixture 但 patch 已生效
        # 簡化：直接 call route function
        from starlette.testclient import TestClient as SC
        from main import app
        c = SC(app)
        resp = c.get("/health/ready")
        assert resp.status_code == 200
```

**Fixture 提示**：`client` 是既有 conftest fixture（FastAPI TestClient on app）。若不同命名 grep 既有 health test 對齊。

- [ ] **Step 2: Run test — 預期 PASS（shallow 行為已存在）**

Run:
```bash
pytest tests/test_health_deep.py::test_ready_shallow_returns_db_only -xvs
```
Expected: PASS — 既有 shallow 行為 unchanged。

第二個 test 預期 FAIL（`_check_line` 尚不存在）— 將在 Task 2 通過。

- [ ] **Step 3: Commit baseline**

```bash
git add tests/test_health_deep.py
git commit -m "test(health): baseline shallow /ready behavior unchanged

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch3.1"
```

---

## Task 2: Deep mode — 加 ?deep=1 query + 3 個 component check

**Files:**
- Modify: `api/health.py`
- Test: `tests/test_health_deep.py`

- [ ] **Step 1: 寫 failing test — deep all-green 200**

加入 `tests/test_health_deep.py`：

```python
from unittest.mock import patch


def test_ready_deep_all_green_returns_200(client):
    """deep=1 + breaker closed + LineTokenHealth healthy + pool 低 → 200。"""
    with patch("api.health._check_line", return_value={"ok": True, "breaker": "closed"}), \
         patch("api.health._check_supabase", return_value={"ok": True, "breaker": "closed", "pending_uploads": 0}), \
         patch("api.health._check_db_pool", return_value={"ok": True, "used": 1, "size": 5, "utilization": 0.2}):
        resp = client.get("/health/ready?deep=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["components"].keys()) == {"db", "line", "supabase", "db_pool"}
    assert all(c.get("ok") for c in body["components"].values())


def test_ready_deep_line_breaker_open_returns_503(client):
    """deep=1 + LINE breaker open → 503 含 line.breaker='open'."""
    with patch("api.health._check_line", return_value={"ok": False, "breaker": "open", "consecutive_failures": 6}), \
         patch("api.health._check_supabase", return_value={"ok": True, "breaker": "closed", "pending_uploads": 0}), \
         patch("api.health._check_db_pool", return_value={"ok": True, "used": 1, "size": 5, "utilization": 0.2}):
        resp = client.get("/health/ready?deep=1")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["components"]["line"]["breaker"] == "open"
    assert body["components"]["line"]["ok"] is False


def test_ready_deep_supabase_too_many_pending_returns_503(client):
    """deep=1 + pending_uploads > 50 (積壓警戒) → 503。"""
    with patch("api.health._check_line", return_value={"ok": True, "breaker": "closed"}), \
         patch("api.health._check_supabase", return_value={"ok": False, "breaker": "closed", "pending_uploads": 75}), \
         patch("api.health._check_db_pool", return_value={"ok": True, "used": 1, "size": 5, "utilization": 0.2}):
        resp = client.get("/health/ready?deep=1")
    assert resp.status_code == 503
    assert resp.json()["components"]["supabase"]["pending_uploads"] == 75
```

- [ ] **Step 2: Run failing tests**

```bash
pytest tests/test_health_deep.py -xvs
```
Expected: deep tests FAIL（`api.health._check_line` 不存在）

- [ ] **Step 3: 實作 deep endpoint**

Modify `api/health.py`，**完整覆蓋既有檔**：

```python
"""
api/health.py — 健康檢查端點

提供 liveness 與 readiness 探針，供負載均衡器 / K8s 使用。

Readiness shallow（無 query）：SELECT 1，K8s/zeabur 預設 probe 用。
Readiness deep（?deep=1）：另探 LINE / Supabase breaker state + DB pool 飽和度。
SRE 手測 / 排錯用，**不打外網**（讀既有 P1 breaker state + LineTokenHealth 表）。
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from models.base import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


# DB pool 飽和度警戒（>85% utilization 視為紅）
_DB_POOL_UTILIZATION_WARN = 0.85

# Supabase pending uploads 積壓警戒（> 50 視為紅）
_SUPABASE_PENDING_WARN = 50

# LineTokenHealth row 過期警戒：> 26 小時無 daily tick 視為不可信
_LINE_HEALTH_STALE_HOURS = 26


def _check_db() -> dict:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:
        logger.error("DB check failed: %s", e, exc_info=True)
        return {"ok": False, "error": "db_unavailable"}


def _check_line() -> dict:
    """LINE 健康度：breaker state + LineTokenHealth 最新 row。"""
    try:
        from utils.circuit_breaker import LINE_BREAKER
    except Exception:
        return {"ok": False, "error": "breaker_import_failed"}

    breaker_stats = LINE_BREAKER.stats
    state = breaker_stats.get("state", "unknown")
    ok = state == "closed"
    out: dict = {
        "ok": ok,
        "breaker": state,
        "consecutive_failures": breaker_stats.get("consecutive_failures", 0),
    }

    # 補 LineTokenHealth row 狀態（不打外網）
    try:
        from models.integration_health import LineTokenHealth
        session = get_engine().connect()
        try:
            row = session.execute(
                text("SELECT healthy, last_check_at, consecutive_failures FROM line_token_health WHERE id=1")
            ).first()
        finally:
            session.close()

        if row is None:
            out["token_health"] = "no_record"
        else:
            out["token_healthy"] = bool(row.healthy)
            out["token_last_check_at"] = row.last_check_at.isoformat() if row.last_check_at else None
            # 若 last_check_at 超過 stale 警戒，視為紅
            if row.last_check_at:
                age = datetime.now(timezone.utc) - row.last_check_at
                if age > timedelta(hours=_LINE_HEALTH_STALE_HOURS):
                    out["ok"] = False
                    out["stale"] = True
            # token healthy=False 直接拉紅
            if not row.healthy:
                out["ok"] = False
    except Exception as e:
        logger.warning("LineTokenHealth query failed: %s", e)
        out["token_health"] = "query_failed"

    return out


def _check_supabase() -> dict:
    """Supabase 健康度：breaker state + pending_uploads 積壓。"""
    try:
        from utils.circuit_breaker import SUPABASE_BREAKER
    except Exception:
        return {"ok": False, "error": "breaker_import_failed"}

    breaker_stats = SUPABASE_BREAKER.stats
    state = breaker_stats.get("state", "unknown")
    out: dict = {
        "ok": state == "closed",
        "breaker": state,
    }

    try:
        with get_engine().connect() as conn:
            pending = conn.execute(
                text(
                    "SELECT COUNT(*) FROM pending_uploads "
                    "WHERE status='pending' AND attempts < 5"
                )
            ).scalar() or 0
        out["pending_uploads"] = pending
        if pending > _SUPABASE_PENDING_WARN:
            out["ok"] = False
    except Exception as e:
        logger.warning("pending_uploads query failed: %s", e)
        out["pending_uploads"] = None

    return out


def _check_db_pool() -> dict:
    """DB connection pool 飽和度。"""
    try:
        pool = get_engine().pool
        used = pool.checkedout()
        # SQLAlchemy QueuePool 才有 size()；其他 pool 退路用 -1
        size = pool.size() if hasattr(pool, "size") else -1
    except Exception as e:
        logger.warning("db pool stats failed: %s", e)
        return {"ok": True, "error": "stats_unavailable"}

    utilization = (used / size) if size > 0 else 0.0
    return {
        "ok": utilization <= _DB_POOL_UTILIZATION_WARN,
        "used": used,
        "size": size,
        "utilization": round(utilization, 2),
    }


@router.get("/live")
async def liveness():
    """Liveness probe — 程序存活即回 200。"""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(deep: bool = Query(False)):
    """Readiness probe — shallow: SELECT 1; deep: + LINE/Supabase/db_pool 健康度。"""
    start = time.monotonic()
    components: dict[str, dict] = {}

    components["db"] = _check_db()
    overall_ok = components["db"].get("ok", False)

    if deep:
        components["line"] = _check_line()
        components["supabase"] = _check_supabase()
        components["db_pool"] = _check_db_pool()
        overall_ok = overall_ok and all(
            c.get("ok") for c in components.values()
        )

    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    body = {
        "status": "ok" if overall_ok else "degraded",
        "latency_ms": elapsed_ms,
        "components": components,
    }
    return JSONResponse(
        status_code=200 if overall_ok else 503,
        content=body,
    )
```

- [ ] **Step 4: Run all tests pass**

Run:
```bash
pytest tests/test_health_deep.py -xvs
```
Expected: 5 PASS（baseline 2 + deep 3）

- [ ] **Step 5: Commit**

```bash
git add api/health.py tests/test_health_deep.py
git commit -m "feat(health): /health/ready?deep=1 加 LINE/Supabase/db_pool 健康度

Shallow 模式不變（K8s/zeabur readiness probe 不需改）。
Deep mode 讀既有 P1 breaker state + LineTokenHealth + pending_uploads，
零外部呼叫成本。任一 component 紅回 503。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch3"
```

---

## Task 3: 整合驗證 + 零 regression

- [ ] **Step 1: 全 health 相關 test**

```bash
pytest tests/ -k "health" -xvs
```
Expected: 既有 + 新 5 個全 PASS。

- [ ] **Step 2: 全套 pytest 零 regression**

```bash
pytest tests/ --tb=short 2>&1 | tail -30
```
Expected: 既有 pass 數不減。

- [ ] **Step 3: dev server 實打 shallow + deep**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh   # 另開 terminal 起 backend
curl -s http://localhost:8088/health/ready | jq '.'
curl -s http://localhost:8088/health/ready?deep=1 | jq '.'
```
Expected:
- shallow：`{"status":"ok","components":{"db":{"ok":true}}}`
- deep：`{"status":"ok","components":{"db":..., "line":..., "supabase":..., "db_pool":...}}`

可進一步測 503：手動將 `LINE_BREAKER._state` 設 `"open"` 或讓 LineTokenHealth.healthy=False 後再打。

---

## Self-Review Checklist

- [x] **Spec coverage**：Ch3 全 6 section 覆蓋
  - 3.1 API 變動 → Task 2
  - 3.2 3 component 來源 → Task 2 各 `_check_*`
  - 3.3 不打外網設計 → Task 2 註解 + `_check_line` 讀 LineTokenHealth row 而非 introspect
  - 3.4 Response 範例 → Task 2 結構符
  - 3.5 zeabur/LB 不動 → shallow 行為 by Task 1 test 鎖
  - 3.6 測試 3 個 → Task 1+2 共 5 個（含 supabase pending warn 額外覆蓋）
- [x] **Placeholder scan**：無 TBD/TODO
- [x] **Type consistency**：所有 `_check_*` 回 dict[str, Any] 且必含 `ok: bool`，readiness aggregator 一致 reduce
- [x] **無 schema 變動**：本 plan 零 migration，純 read-only 擴充

## 風險與緩解

| 風險 | 緩解 |
|---|---|
| P1 落地 `LINE_BREAKER` 命名與本 plan 假設不同 | Task 2 step 3 先 import 失敗時 graceful return `{"ok": False, "error": "breaker_import_failed"}`，不阻 endpoint |
| `pending_uploads` 表結構與本 plan SQL 不符 | Task 2 step 3 query 失敗 try/except 包，`pending_uploads: None` 不算紅（避免誤報） |
| SQLAlchemy pool 不是 QueuePool（如 NullPool 在 test）| `hasattr(pool, "size")` 守住；無 size 時 `utilization=0`，視為綠 |
| K8s/zeabur probe 改成 deep 模式造成額外 DB 流量 | 文件 explicit 標註 deep 為 SRE 手測；可選擇加 `X-Health-Deep-Token` header 防誤觸（本 plan 不做，YAGNI） |
