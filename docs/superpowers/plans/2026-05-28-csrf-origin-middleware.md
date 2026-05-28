# CSRF Origin/Referer middleware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新 `CSRFOriginCheckMiddleware` 對所有 unsafe HTTP method 強制檢查 Origin/Referer 必在 `cors_origins` 白名單，阻擋跨網域 CSRF。LINE webhook 與家長公開報名 path bypass。

**Architecture:** 單 PR 順序 2 commit：(C1) conftest autouse fixture 先注入 `http://testserver` 進 `cors_origins`（無 middleware code 仍 baseline 5492 綠）→ (C2) 落 `middleware/csrf_origin.py` + register main.py + 5 個新 pytest。順序顛倒會造成 1433 行 mutation test 短暫紅燈。

**Tech Stack:** FastAPI Starlette BaseHTTPMiddleware / pytest TestClient / `config.network.cors_origins`

**Spec:** `docs/superpowers/specs/2026-05-28-csrf-origin-middleware-design.md` (commit `b66b6f3`)

---

## File Structure

**New files:**
- `middleware/csrf_origin.py` — `CSRFOriginCheckMiddleware` class + `CSRF_EXEMPT_PREFIXES` + helpers `_get_allowed_origins` / `_extract_origin_from_referer`
- `tests/test_csrf_origin_middleware.py` — 6 個 pytest（5 個 middleware 行為 + 1 bypass path）

**Modified files:**
- `main.py` — `app.add_middleware(CSRFOriginCheckMiddleware)` 插在 TrustedHost 之後、RequestLogging 之前
- `tests/conftest.py` — 加 autouse session-scoped fixture 注入 `http://testserver` 進 cors_origins

**Unchanged but referenced:**
- `config/network.py:13 cors_origins` — 白名單來源（既存）
- `utils/cookie.py` — SameSite=none 跨域時失效 CSRF 防護的場景
- `api/line_webhook.py` — bypass target（signature 驗證）
- `api/activity/public.py` — bypass target（家長公開報名）

---

## Task 1: conftest autouse fixture（critical 先做）

**Goal:** 注入 `http://testserver` 進 `cors_origins`，讓既有 1433 行 mutation test 在 middleware 落地後仍綠。**先 commit + verify baseline 不變後才進 Task 2**。

**Files:**
- Modify: `tests/conftest.py`

### Steps

- [ ] **Step 1.1: 先 verify settings.network.cors_origins 是否 mutable**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
python3 -c "
from config import settings
print('Type:', type(settings.network))
print('Frozen?', getattr(settings.network, 'model_config', {}).get('frozen', False))
print('Current cors_origins:', settings.network.cors_origins)
# 試 mutation
try:
    settings.network.cors_origins = list(settings.network.cors_origins or []) + ['http://testserver']
    print('Mutation 成功:', settings.network.cors_origins)
except Exception as e:
    print('Mutation 失敗:', type(e).__name__, e)
"
```

根據結果決定 fixture 方案：
- 若可直接 assign → 用 fixture 方案 A
- 若 frozen → 用 `object.__setattr__` 或 `monkeypatch.setattr` 方案 B

- [ ] **Step 1.2: 確認 conftest.py 既有結構**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
ls tests/conftest.py && head -40 tests/conftest.py
```
找到既有 autouse fixture 區（若有）+ scope 慣例。

- [ ] **Step 1.3: 加 autouse fixture 進 conftest.py**

Edit `tests/conftest.py`，根據 Step 1.1 結果擇一加：

**方案 A**（settings 可 mutate）：

```python
import pytest

@pytest.fixture(autouse=True, scope="session")
def _csrf_testclient_origin_allowlist():
    """TestClient 預設 Origin http://testserver 注入 cors_origins，
    讓 CSRFOriginCheckMiddleware (待 Task 2 落地) 不擋既有 mutation test
    （1433 行跨 189 file）。

    Spec: docs/superpowers/specs/2026-05-28-csrf-origin-middleware-design.md §4
    """
    from config import settings
    original = list(settings.network.cors_origins or [])
    if "http://testserver" not in original:
        settings.network.cors_origins = original + ["http://testserver"]
    yield
    settings.network.cors_origins = original
```

**方案 B**（settings frozen 需 setattr trick）：

```python
import pytest

@pytest.fixture(autouse=True, scope="session")
def _csrf_testclient_origin_allowlist():
    """TestClient 預設 Origin http://testserver 注入 cors_origins"""
    from config import settings
    original = list(settings.network.cors_origins or [])
    if "http://testserver" not in original:
        object.__setattr__(
            settings.network, "cors_origins", original + ["http://testserver"]
        )
    yield
    object.__setattr__(settings.network, "cors_origins", original)
```

- [ ] **Step 1.4: 跑全套 pytest 確認 baseline 不變**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest --tb=line 2>&1 | tail -10
```
Expected: `5492 passed, 87 skipped, 1 xfailed` 或同 baseline +0/-0 fail。

**此時 middleware 還沒落地**，fixture 純預設定 cors_origins 應無任何影響（既有程式碼還不會讀 cors_origins 來 enforce CSRF）。如果突然出現 regression，回頭查 fixture 順序 / scope 是否影響別的 autouse fixture。

- [ ] **Step 1.5: Commit (C1)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add tests/conftest.py
git commit -m "$(cat <<'EOF'
test(conftest): autouse fixture preload http://testserver into cors_origins

為 Spec B CSRF middleware 鋪墊。TestClient 預設 Origin 是 http://testserver，
不在 prod cors_origins / dev fallback 內，待 Task 2 落 CSRFOriginCheckMiddleware
後將擋下 1433 mutation test lines 跨 189 file。預先注入避免 Task 2 落地時
出現大規模短暫紅燈。

此 commit 落地時 middleware 尚未存在，fixture 純預設定 cors_origins 不影響
既有行為（baseline 5492/0 不變）。

Refs: Spec docs/superpowers/specs/2026-05-28-csrf-origin-middleware-design.md §4
EOF
)"
```

---

## Task 2: middleware code + register main.py + 6 pytest

**Goal:** 落 `middleware/csrf_origin.py` 完整實作；`main.py` register middleware 插在 TrustedHost 之後、RequestLogging 之前；6 個 pytest cover safe methods / no origin / allowed origin / disallowed origin / referer fallback / bypass paths。

**Files:**
- Create: `middleware/csrf_origin.py`
- Modify: `main.py`
- Create: `tests/test_csrf_origin_middleware.py`

### Steps

- [ ] **Step 2.1: 確認 middleware/ 目錄存在 + 既有 module 結構**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
ls middleware/ 2>/dev/null || echo "middleware/ does not exist"
```

若不存在，建：`mkdir -p middleware && touch middleware/__init__.py`。
若存在，list 既有 middleware module 確認命名一致。

- [ ] **Step 2.2: 創建 middleware/csrf_origin.py**

Write 新檔 `middleware/csrf_origin.py`：

```python
"""CSRF Origin/Referer middleware.

對 POST/PATCH/PUT/DELETE 強制檢查 Origin（fallback Referer）必在
config.network.cors_origins 白名單。GET/HEAD/OPTIONS skip（RFC 7231 safe methods）。

LINE webhook 與家長公開報名 path bypass（webhook 走 signature / public 走限流+audit）。

Spec: docs/superpowers/specs/2026-05-28-csrf-origin-middleware-design.md
"""

import logging
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

# Bypass paths（寫死於 module 常數，新增需改 code + PR review）
CSRF_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/line/webhook",        # LINE webhook signature 驗證不靠 cookie
    "/api/activity/public/",    # 家長公開報名 by design 接受跨站 POST
)


def _extract_origin_from_referer(referer: str) -> str | None:
    """從 Referer URL 取 scheme://host[:port]，normalize default port。"""
    try:
        parts = urlsplit(referer)
        if not parts.scheme or not parts.netloc:
            return None
        netloc = parts.netloc
        if parts.scheme == "https" and netloc.endswith(":443"):
            netloc = netloc[: -len(":443")]
        elif parts.scheme == "http" and netloc.endswith(":80"):
            netloc = netloc[: -len(":80")]
        return f"{parts.scheme}://{netloc}"
    except Exception:
        return None


def _get_allowed_origins() -> list[str]:
    """重用 main.py 的 CORS_ORIGINS 計算邏輯（含 dev fallback）。

    與 main.py:702-708 的 CORS_ORIGINS 變數同源（settings.network.cors_origins +
    dev fallback）。日後 main.py 改 fallback 名單需同步本 helper。
    """
    from config import settings

    origins = list(settings.network.cors_origins or [])
    if not origins and settings.core.env.lower() in ("development", "dev", "local"):
        origins = [
            "http://localhost:5173",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:3000",
        ]
    return origins


class CSRFOriginCheckMiddleware(BaseHTTPMiddleware):
    """CSRF defense — Origin/Referer header check for unsafe methods."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in UNSAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
            return await call_next(request)

        allowed_origins = _get_allowed_origins()
        if not allowed_origins:
            logger.error(
                "CSRF middleware: cors_origins 空集合，拒絕所有 unsafe request"
            )
            return JSONResponse(
                {"detail": "CSRF check failed: no allowed origins configured"},
                status_code=403,
            )

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")

        if origin:
            if origin in allowed_origins:
                return await call_next(request)
            logger.warning(
                "CSRF reject: origin=%s not in allowlist path=%s method=%s",
                origin,
                path,
                request.method,
            )
            return JSONResponse(
                {"detail": "CSRF check failed: origin not allowed"},
                status_code=403,
            )

        if referer:
            referer_origin = _extract_origin_from_referer(referer)
            if referer_origin and referer_origin in allowed_origins:
                return await call_next(request)
            logger.warning(
                "CSRF reject: referer=%s (origin=%s) not in allowlist path=%s method=%s",
                referer,
                referer_origin,
                path,
                request.method,
            )
            return JSONResponse(
                {"detail": "CSRF check failed: referer not allowed"},
                status_code=403,
            )

        logger.warning(
            "CSRF reject: missing both origin and referer path=%s method=%s",
            path,
            request.method,
        )
        return JSONResponse(
            {"detail": "CSRF check failed: missing origin/referer"},
            status_code=403,
        )
```

- [ ] **Step 2.3: 註冊 middleware 在 main.py**

`main.py:845-869` 區域有既有 middleware register。在 `TrustedHostMiddleware` 之前（add 順序最後 add 最先執行）加：

```python
from middleware.csrf_origin import CSRFOriginCheckMiddleware

# ... 既有 app.add_middleware(TrustedHostMiddleware, ...) 之前加：
app.add_middleware(CSRFOriginCheckMiddleware)
```

確認順序：在 main.py 內找到 `app.add_middleware(TrustedHostMiddleware` 那行，**之前**插 `app.add_middleware(CSRFOriginCheckMiddleware)`（CSRF middleware 將在 TrustedHost 之後執行）。

或在 RequestLoggingMiddleware add 之後加 — 兩種 add 順序產生同 execution order（CSRF 在 TrustedHost 之後、RequestLogging 之前）。讀完 main.py:845-869 區段後選一個合理位置插入。

- [ ] **Step 2.4: 創建 tests/test_csrf_origin_middleware.py**

Write 6 個 pytest：

```python
"""Spec B: CSRFOriginCheckMiddleware 6 個 pytest。

用 minimal FastAPI app + middleware，避免 main.py 整體啟動 cost。
monkeypatch settings.network.cors_origins 設 fixed 白名單。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.csrf_origin import CSRFOriginCheckMiddleware


@pytest.fixture
def app_with_csrf(monkeypatch):
    """Minimal app: middleware + 兩條 dummy route + bypass route。"""
    from config import settings

    monkeypatch.setattr(
        settings.network,
        "cors_origins",
        ["http://allowed.example.com", "https://allowed.example.com"],
    )

    app = FastAPI()
    app.add_middleware(CSRFOriginCheckMiddleware)

    @app.get("/api/safe")
    def safe_get():
        return {"ok": True}

    @app.post("/api/unsafe")
    def unsafe_post():
        return {"ok": True}

    @app.post("/api/line/webhook")
    def line_webhook():
        return {"ok": True}

    @app.post("/api/activity/public/register")
    def public_register():
        return {"ok": True}

    return TestClient(app)


def test_safe_methods_pass_without_origin(app_with_csrf):
    """GET/HEAD/OPTIONS skip CSRF check 即使無 Origin/Referer 也 200。"""
    res = app_with_csrf.get("/api/safe")
    assert res.status_code == 200


def test_post_without_origin_returns_403(app_with_csrf):
    """POST 無 Origin/Referer → 403 + 含 missing origin/referer detail。"""
    res = app_with_csrf.post("/api/unsafe", headers={"Origin": "", "Referer": ""})
    # TestClient 預設不送 Origin/Referer header；如 conftest fixture 注入了
    # http://testserver，需要明確 override 為空字串
    # 實際 monkeypatch 已 setattr 換掉 cors_origins，testserver 不在內
    assert res.status_code == 403
    assert "missing origin/referer" in res.json()["detail"]


def test_post_with_allowed_origin_passes(app_with_csrf):
    """POST + Origin in cors_origins → 200。"""
    res = app_with_csrf.post(
        "/api/unsafe", headers={"Origin": "http://allowed.example.com"}
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_post_with_disallowed_origin_returns_403(app_with_csrf):
    """POST + Origin 不在 cors_origins → 403 + 含 origin not allowed detail。"""
    res = app_with_csrf.post(
        "/api/unsafe", headers={"Origin": "http://evil.example.com"}
    )
    assert res.status_code == 403
    assert "origin not allowed" in res.json()["detail"]


def test_post_with_referer_fallback_passes(app_with_csrf):
    """POST 缺 Origin、Referer 在白名單 → 200。"""
    res = app_with_csrf.post(
        "/api/unsafe",
        headers={"Referer": "http://allowed.example.com/some/page"},
    )
    assert res.status_code == 200


def test_bypass_paths_skip_csrf(app_with_csrf):
    """POST /api/line/webhook + /api/activity/public/* 無 Origin → 200（path bypass）。"""
    res1 = app_with_csrf.post("/api/line/webhook")
    res2 = app_with_csrf.post("/api/activity/public/register")
    assert res1.status_code == 200
    assert res2.status_code == 200
```

**注意**：test 2 (`test_post_without_origin_returns_403`) 依賴 TestClient 不自動帶 Origin header。某些 TestClient 版本可能自動帶。如 test 失敗，改用：
```python
res = app_with_csrf.request("POST", "/api/unsafe", headers={})  # 明確空 header
```

- [ ] **Step 2.5: 跑新 CSRF test 確認 pass**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_csrf_origin_middleware.py -v 2>&1 | tail -20
```
Expected: 6 個 test 全 pass。

如有 fail：
- test 2 `test_post_without_origin_returns_403`：可能 TestClient 自動帶 Origin → 改 monkeypatch TestClient 配置
- 其他 fail：檢查 monkeypatch scope / fixture cors_origins 是否實際被改

- [ ] **Step 2.6: 跑全套 pytest 確認 baseline 不破**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest --tb=line 2>&1 | tail -10
```
Expected: `5498 passed`（5492 baseline + 6 CSRF test）或同 baseline +6/-0 fail。

**特別注意**：
- 既有 1433 個 mutation test 在 conftest autouse fixture（Task 1）下應全綠（Origin `http://testserver` 已注入 cors_origins）
- 若大量 mutation test 突然 403 → Task 1 conftest fixture 沒生效或 monkeypatch scope 錯了
- 若 webhook / public 端點 test fail → bypass path prefix 比對 bug

- [ ] **Step 2.7: Commit (C2)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add middleware/csrf_origin.py main.py tests/test_csrf_origin_middleware.py
# 如 middleware/ 是新目錄記得 git add middleware/__init__.py
git status --short
git commit -m "$(cat <<'EOF'
feat(security): CSRF Origin/Referer middleware

Spec B (audit P1 #12)：CSRFOriginCheckMiddleware 對 POST/PATCH/PUT/DELETE
強制檢查 Origin（fallback Referer）必在 config.network.cors_origins 白名單。

設計：
- Bypass paths 寫死：/api/line/webhook（signature 驗證）+
  /api/activity/public/（家長公開報名）
- Origin/Referer 都缺 → 403 + log warning（不入 audit_logs 控制 log volume）
- Referer normalize default port (rare 但 defensive)
- 重用 cors_origins (無新 env)；dev fallback 對齊 main.py:702-708

插在 main.py middleware chain：TrustedHost → CSRFOriginCheck →
RequestLogging → SecurityHeaders → Audit → CORS → routers。

零 migration、零前端改動。Roll-out 需 USER 確認 Zeabur env CORS_ORIGINS
含 prod 前端網域（否則所有 POST 全 403）。

6 個新 pytest 配合 Task 1 conftest autouse fixture（http://testserver 已注入
cors_origins）讓既有 1433 mutation test 跨 189 file 仍全綠。

Refs: Spec docs/superpowers/specs/2026-05-28-csrf-origin-middleware-design.md
EOF
)"
```

---

## Task 3: 最終驗收

**Goal:** 全套 pytest 最終 sanity + git log 確認 commit 結構 + Roll-out checklist 給 user。

### Steps

- [ ] **Step 3.1: 全套 pytest 最終跑**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest --tb=short 2>&1 | tail -15
```
Expected: `5498 passed` 或 baseline + 6 new test, 0 fail。

- [ ] **Step 3.2: git log + diff stat 確認**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
echo "=== Spec B 2 commits ==="
git log --oneline -5 | head -5
echo ""
echo "=== Diff stat ==="
git diff b66b6f3..HEAD --stat
```

Expected:
- 2 commits (C1 conftest + C2 middleware/main/test)
- 4 files：`tests/conftest.py` (+10) / `middleware/csrf_origin.py` (new ~120) / `main.py` (+2) / `tests/test_csrf_origin_middleware.py` (new ~80)
- 約 +210 / -0 行

- [ ] **Step 3.3: 報告完成狀態**

向 user 回報：
- ✅ 2 commit 完成（commit SHA）
- ✅ 全套 pytest pass + 6 new CSRF test
- **Roll-out checklist（spec §8 → 9 條 user 手測）**
- **critical reminder**：Zeabur env `CORS_ORIGINS` 必含 prod 前端網域，否則所有 POST 全 403

---

## Spec Coverage Check

| Spec section | Task | Status |
|--------------|------|--------|
| §2 G1 middleware 對 unsafe method 檢查 Origin | Task 2 Step 2.2 | ✓ |
| §2 G2 重用 cors_origins | Task 2 Step 2.2 `_get_allowed_origins` | ✓ |
| §2 G3 bypass paths (LINE webhook + public) | Task 2 Step 2.2 `CSRF_EXEMPT_PREFIXES` | ✓ |
| §2 G4 Origin/Referer 都缺 → 403 | Task 2 Step 2.2 + Step 2.4 test 2 | ✓ |
| §2 G5 GET/HEAD/OPTIONS skip | Task 2 Step 2.2 `UNSAFE_METHODS` + Step 2.4 test 1 | ✓ |
| §2 G6 零回歸 | Task 1 + Task 2 Step 2.6 | ✓ |
| §3.2 白名單機制 | Task 2 Step 2.2 `_get_allowed_origins` + Task 1 fixture | ✓ |
| §3.3 CSRF_EXEMPT_PREFIXES 寫死 | Task 2 Step 2.2 | ✓ |
| §3.4 middleware 順序 | Task 2 Step 2.3 | ✓ |
| §3.5 完整檢查邏輯 | Task 2 Step 2.2 | ✓ |
| §3.6 不入 audit_logs | Task 2 Step 2.2 (只 logger.warning) | ✓ |
| §4 5-6 個 pytest | Task 2 Step 2.4 (6 個) | ✓ |
| §4 conftest autouse fixture | Task 1 Step 1.3 | ✓ |
| §6 風險 TestClient blast radius | Task 1 (先做 fixture) | ✓ |
| §6 風險 internal callers (zero) | spec verify 完，無 plan task | N/A |
