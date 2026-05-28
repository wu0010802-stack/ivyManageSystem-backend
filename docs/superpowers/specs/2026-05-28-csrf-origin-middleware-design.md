# Spec B: CSRF Origin/Referer middleware (#12)

**日期**：2026-05-28
**狀態**：Draft，等 user 確認
**對應 audit findings**：🟠 P1 #12 — CSRF 唯一防線是 SameSite，prod cross-domain 用 none 即裸奔
**對應 spec 系列**：A (限流) ✅ / **B (CSRF)** / C (Logger PII) / D (audit append-only) / E (LINE 跨境) / F (staff refresh)

---

## 1. Why

### 1.1 攻擊面

`utils/cookie.py:11-19, 33-49` 註解明示 Zeabur split-domain 部署（`ivymanageportal.zeabur.app` 前端 ↔ `ivymanagesystem-api.zeabur.app` 後端）必把 `COOKIE_SAMESITE` 設 `none`，因為 SameSite=strict/lax 跨網域 cookie 不會送。設 none 後：

- 瀏覽器允許從**任意**第三方網域發起的請求帶 cookie（只要前端 form/fetch 帶 credentials）
- 唯一 CSRF 防線（SameSite）失效
- 無 CSRF token、無 Origin/Referer middleware → **裸奔**

攻擊情境：使用者登入 `portal.zeabur.app` 並打開另一分頁 `evil.example.com`，攻擊者誘導點擊「免費領取」按鈕觸發隱形 form：
```html
<form action="https://ivymanagesystem-api.zeabur.app/api/users/123/reset-password"
      method="POST">
  <input name="new_password" value="hacked123!">
</form>
<script>document.forms[0].submit()</script>
```
→ 瀏覽器自動帶 `access_token` cookie（SameSite=none + credentials）→ 攻擊者重設 victim 密碼成功（如果 victim 是 admin）。

### 1.2 為何選 Origin/Referer middleware vs CSRF token

| 方案 | 工程量 | 前端配合 | 與既有機制重複 | 攻擊面收斂 |
|------|--------|---------|---------------|-----------|
| **Origin/Referer middleware (本 spec)** | 半天 | 零 | 與 SameSite 互補 | CSRF Top 10 |
| Double-submit CSRF token | 1-2 週 | 需改每 form/api 呼叫 | 與 SameSite=strict 重複防護 | CSRF Top 10 |
| Synchronizer token pattern | 2-3 週 | 同上 + session state | 同上 | 同上 |

Origin/Referer 是 OWASP CSRF Prevention Cheat Sheet 列為 **defense-in-depth** 的最低成本最有效手段。所有主流瀏覽器在 POST/PATCH/PUT/DELETE 都會送 `Origin` header（fetch + form 皆然），server 比對白名單擋下跨網域請求即可。

---

## 2. Goals / Non-goals

### Goals
- (G1) 新 `CSRFOriginCheckMiddleware` 對 POST/PATCH/PUT/DELETE 強制檢查 Origin header（fallback Referer），不在白名單即 403。
- (G2) 白名單**重用** `config/network.py:cors_origins`（無新 env，prod 部署 CORS_ORIGINS 同時保護 CSRF）。
- (G3) Bypass path 寫死於 middleware 模組：
  - `/api/line/webhook`（LINE signature 驗證不靠 cookie）
  - `/api/activity/public/`（家長公開報名 by design 接受跨站 POST）
- (G4) Origin/Referer 都缺視為可疑 → 403 + log warning。
- (G5) GET/HEAD/OPTIONS 不檢查（CSRF standard — safe methods per RFC 7231）。
- (G6) 零回歸：既有 5492 pytest 全綠 + login / change-password / 所有現有 endpoint 在前端正常運作。

### Non-goals
- 不引入 double-submit CSRF token / synchronizer token（與 SameSite + Origin/Referer 重複防護）。
- 不改前端（前端 fetch/axios 自動帶 Origin header）。
- 不對 GET/HEAD/OPTIONS 檢查（safe methods + 觸發 CSRF 沒意義）。
- 不入 audit_logs（log volume 控制；只 logger.warning + Sentry 自動帶 request_id 追蹤）。
- 不對 webhook payload 二次驗證（LINE webhook 已用 signature；其他 webhook 若日後加，加進 CSRF_EXEMPT_PATHS 同時加自己的 auth 機制）。
- 不在本 spec 內處理 P0/P1 其餘 audit findings（A 已完成；C/D/E/F 為獨立 spec）。

---

## 3. Architecture

### 3.1 PR 結構（單 PR 2 commit）

| Commit | 範圍 | 檔案數 | 風險 |
|--------|------|--------|------|
| **C1**：`feat(security): CSRF Origin/Referer middleware` | middleware + register + tests | 1 new module + main.py + 1 new test file | 低（middleware 新增、可逐步觀察 log） |
| **C2**：`chore(ci): integration smoke for CSRF middleware` (optional) | 可選 — 加一條 CI lint check 確認 CORS_ORIGINS env 設了才不算 unsafe | `.github/workflows/ci.yml` | 零 |

主推 **C1 一個 commit 即可 ship**，C2 視 CI 改動意願決定（不強制）。

### 3.2 白名單機制

- 主白名單：`config/network.py:cors_origins` (CsvList, 既存 env `CORS_ORIGINS`)
- 比對方式：
  - Origin header 直接字串比對（`origin in cors_origins`），不做 wildcard / suffix match（標準作法）
  - Referer header：parse URL 取 `scheme://host[:port]`（用 `urllib.parse.urlsplit`）後字串比對
- 空白名單行為：若 `cors_origins == []`（dev 環境 fallback `localhost:5173/3000` 等），CSRF middleware 同樣套用該 fallback 清單（與 `main.py:702 CORS_ORIGINS` 變數共用），無 unsafe 模式

### 3.3 Bypass paths（寫死於 module 常數）

```python
# middleware/csrf_origin.py 模組層級
CSRF_EXEMPT_PREFIXES = (
    "/api/line/webhook",       # LINE webhook signature 驗證不靠 cookie
    "/api/activity/public/",   # 家長公開報名 by design 接受跨站 POST
                               # （限流 + reCAPTCHA + audit 為主要防線）
)
```

判斷邏輯：`request.url.path.startswith(prefix)` 任一命中即 bypass。

**為何 prefix 而非 exact match**：
- `/api/line/webhook` 雖目前是 single endpoint，未來若加 `/api/line/webhook/v2` 也應 bypass
- `/api/activity/public/` 內有 7 個 POST endpoint，未來新增 public route 自動 bypass 無需更新白名單

**新增 bypass 流程**：改 code + PR review + reviewer 必確認新 path 有自己的 auth 機制（webhook signature / reCAPTCHA / rate limit）。**不**走 env 配置避免 prod 配錯造成意外 bypass。

### 3.4 Middleware 順序

`main.py:845-869` 既有 middleware reverse-add 順序（先 add 後執行）：

```
TrustedHostMiddleware (最先執行，host 攔截)
  → RequestLoggingMiddleware
    → SecurityHeadersMiddleware
      → AuditMiddleware
        → CORSMiddleware (最後執行 / 最先回應 preflight)
          → routers
```

新 `CSRFOriginCheckMiddleware` 插在 **TrustedHostMiddleware 之後、RequestLoggingMiddleware 之前**：

```python
# main.py 順序（reverse-add）
app.add_middleware(CORSMiddleware, ...)             # 既有
app.add_middleware(AuditMiddleware)                  # 既有
app.add_middleware(SecurityHeadersMiddleware)        # 既有
app.add_middleware(RequestLoggingMiddleware)         # 既有
app.add_middleware(CSRFOriginCheckMiddleware)        # 【新】TrustedHost 之後執行
app.add_middleware(TrustedHostMiddleware, ...)       # 既有（最後 add → 最先執行）
```

**為何放這個位置**：
- TrustedHost 之後：host 合法才檢查 Origin（先擋掉 invalid host 減少 CSRF middleware 工作量）
- RequestLogging 之前：CSRF 403 reject 不需要進入 request logging pipeline（避免 log noise + 節省 audit middleware 處理）
- 在 CORS 之前進入 chain（但因 reverse-add，CORS preflight 仍最先回應 — preflight OPTIONS 不會被 CSRF 攔，安全）

### 3.5 檢查策略

```python
class CSRFOriginCheckMiddleware(BaseHTTPMiddleware):
    """檢查 POST/PATCH/PUT/DELETE 的 Origin/Referer 是否在白名單。
    
    GET/HEAD/OPTIONS 不檢查（RFC 7231 safe methods）。
    Bypass path（webhook / public）跳過檢查。
    """
    UNSAFE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

    async def dispatch(self, request: Request, call_next) -> Response:
        # Safe methods 直接放行
        if request.method not in self.UNSAFE_METHODS:
            return await call_next(request)

        # Bypass path 直接放行
        path = request.url.path
        if any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
            return await call_next(request)

        # 取白名單（rebuild per request 避免 hot-reload 配置）
        allowed_origins = _get_allowed_origins()
        if not allowed_origins:
            # 配置錯誤狀態（dev 預設 fallback 仍有值；prod 必設）
            logger.error("CSRF middleware: cors_origins 空集合，拒絕所有 unsafe request")
            return JSONResponse(
                {"detail": "CSRF check failed: no allowed origins configured"},
                status_code=403,
            )

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        
        # 優先 Origin（瀏覽器 POST 一定有；非標準 client 才可能缺）
        if origin:
            if origin in allowed_origins:
                return await call_next(request)
            logger.warning(
                "CSRF reject: origin=%s not in allowlist path=%s method=%s",
                origin, path, request.method,
            )
            return JSONResponse({"detail": "CSRF check failed: origin not allowed"}, status_code=403)

        # Fallback Referer（部分舊瀏覽器 / 特殊 client）
        if referer:
            referer_origin = _extract_origin_from_referer(referer)
            if referer_origin and referer_origin in allowed_origins:
                return await call_next(request)
            logger.warning(
                "CSRF reject: referer=%s (origin=%s) not in allowlist path=%s method=%s",
                referer, referer_origin, path, request.method,
            )
            return JSONResponse({"detail": "CSRF check failed: referer not allowed"}, status_code=403)

        # 都缺：嚴格 reject
        logger.warning(
            "CSRF reject: missing both origin and referer path=%s method=%s",
            path, request.method,
        )
        return JSONResponse({"detail": "CSRF check failed: missing origin/referer"}, status_code=403)


def _extract_origin_from_referer(referer: str) -> str | None:
    """從 Referer URL 取 scheme://host[:port]。"""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(referer)
        if not parts.scheme or not parts.netloc:
            return None
        return f"{parts.scheme}://{parts.netloc}"
    except Exception:
        return None


def _get_allowed_origins() -> list[str]:
    """重用 main.py 的 CORS_ORIGINS 計算邏輯（含 dev fallback）。"""
    # 直接 import main.CORS_ORIGINS 會循環，改 access settings + dev fallback
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
```

### 3.6 不入 audit_logs（log volume 控制）

CSRF reject 預期在 prod 主要場景：
- 配置錯誤（前端 deploy 換 domain 沒同步 CORS_ORIGINS）→ 一般 helper 該被告警，不該每 request 都進 audit_logs
- 真實攻擊嘗試 → 量少（單 IP 通常觸發前面限流就被擋）

決定：**只 `logger.warning` 不 write_login_audit**。理由：
- audit_logs 是稽核軌跡（事後可追溯）；CSRF reject 預期高頻 / 高雜訊
- log warning 配合 `RequestIdLogFilter` 已可在 prod 透過 request_id 追溯
- Sentry warning 鏈會自動收（若 CSRF reject 突然爆量）

未來若 prod 觀察 CSRF reject 量穩定低、且需 forensic trail，再 follow-up 加 audit middleware 接 `CSRF_REJECTED` action（不在本 spec）。

---

## 4. 測試計畫

新增 5 個 pytest 在 `tests/test_csrf_origin_middleware.py`：

1. **test_safe_methods_pass_without_origin** — GET/HEAD/OPTIONS 不檢查 Origin，無 header 也 200
2. **test_post_without_origin_returns_403** — POST 無 Origin/Referer → 403 + 含「missing origin/referer」 detail
3. **test_post_with_allowed_origin_passes** — POST + Origin in cors_origins → 通過進 router（test 用 mock router 確認 dispatch 抵達）
4. **test_post_with_disallowed_origin_returns_403** — POST + Origin 不在 cors_origins → 403 + log warning
5. **test_post_with_referer_fallback_passes** — POST 缺 Origin、Referer 在白名單 → 通過
6. **test_bypass_paths_skip_csrf** — POST `/api/line/webhook` + `/api/activity/public/register` 無 Origin → 通過（path bypass）

策略：用 `TestClient` 包 minimal FastAPI app + middleware，避免依賴整個 main.py app 初始化（test 速度快、scope 隔離）。`monkeypatch settings.network.cors_origins` 設 fixed 白名單。

回歸測試：跑既有全套 pytest 確認 5492 baseline 無 fail。**特別注意**：
- 既有 TestClient 預設 Origin 是 `http://testserver`，dev fallback 含 `http://localhost:5173` 等但**不含** `http://testserver`。
- 需要在 conftest 或本 middleware test fixture 加 `cors_origins` mock 含 `http://testserver`，或 middleware 對 `localhost` / `testserver` 加 dev exemption。
- **採方案 A**（mock cors_origins）：避免污染 prod 防護面。conftest 加一個 autouse fixture set `cors_origins=["http://testserver", ...] ` 給所有 TestClient test。

實際 conftest 改動範圍待 plan stage 確認。

---

## 5. Roll-out

### 5.1 部署步驟

1. PR 合併（1 commit + 5-6 個新 test + conftest 微調）。
2. Zeabur 後端服務 env 必要檢查（**critical**）：
   - `CORS_ORIGINS` 必須含 prod 前端網域（例 `https://ivymanageportal.zeabur.app`）+ 可能 staging 網域
   - 若漏設 → CSRF middleware 會把所有 POST/PATCH/PUT/DELETE 擋下 503/403
3. 部署後 smoke：
   - 從 prod 前端登入 → 正常進入 portal
   - 從 prod 前端發任意 POST（例如新增員工 / 簽核 / 上傳附件）→ 正常完成
   - 從 `curl https://ivymanagesystem-api.zeabur.app/api/employees -X POST -H "Origin: http://evil.com" ...` → **預期 403**

### 5.2 回退方案

純 hotfix revert PR：行為立刻回到「CSRF 防線只有 SameSite」。無 DB migration、無 schema 變動，回退零成本。

但若 prod 上線時發現 `CORS_ORIGINS` env 漏設、所有 POST 都 403，**先補 env 不要急著 revert**（revert 後攻擊面再次裸奔）。

### 5.3 監控指標

7 天觀察 Sentry / log：
- `CSRF reject: origin=*` warning：應接近 0（前端正常運作下）。爆量 → 排查 (a) CORS_ORIGINS env 漏設 (b) 前端 deploy 換 domain 沒同步 (c) 真實攻擊嘗試
- `CSRF reject: missing both origin and referer`：應接近 0。爆量 → 可能某 server-to-server caller（cron / 排程） 沒帶 Origin → 加進 bypass list 或讓該 caller 自帶 Origin

---

## 6. 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| TestClient 預設 Origin `http://testserver` 不在 cors_origins → 既有 5000+ POST test 全 403 | 大規模 test 回歸 | conftest autouse fixture mock cors_origins 含 `http://testserver`；plan 預留 task |
| Zeabur prod env `CORS_ORIGINS` 漏設 → 所有 POST 403 | prod 短暫不可用 | spec §5.1 強調 critical pre-deploy check；roll-out checklist 第一條 |
| 某 server-to-server caller（cron / scheduler）發內部 POST 無 Origin | 該 caller 全部 403 | grep 內部 POST caller（`scheduler/` `services/` 內 httpx 用法）；若有，加 caller 自帶 `Origin` header（無攻擊面，內部呼叫安全） |
| 前端 deploy 換 domain 但 CORS_ORIGINS 沒同步 | prod 用戶看 403 | Sentry warning 立刻可見；同時 ops checklist 更新 domain 切換 SOP |
| 攻擊者偽造 Origin header（curl / server-side）| 仍可 bypass middleware | Origin/Referer 防御 of 純粹是 **CSRF（瀏覽器強制要求）** 防護，不是 anti-API-abuse；瀏覽器無法偽造 Origin（XHR 被瀏覽器 forced 設真實 origin）；非瀏覽器 client（curl/server）拿到 cookie 是 cookie 已洩漏 = 別層攻擊（XSS / 中間人）已成功，CSRF 防線此時失效屬於設計限制（同 OWASP 文檔） |

---

## 7. Out of scope

- 不處理 P0/P1 其餘 audit findings（C/D/E/F 為獨立 spec）
- 不引入 double-submit CSRF token
- 不對 GET 做 CSRF 檢查（不適用）
- 不重構既有 middleware 順序（只插一個新 middleware）
- 不調整 CORS_ORIGINS 的內容（保留 dev fallback 行為）

---

## 8. 驗收 checklist（user 手測 + roll-out）

PR 合併 + deploy 後 USER 手動驗證：

- [ ] Zeabur env 確認 `CORS_ORIGINS` 含 prod 前端網域（例：`https://ivymanageportal.zeabur.app`）
- [ ] 從 prod 前端登入正常
- [ ] 從 prod 前端新增員工（POST）成功
- [ ] 從 prod 前端簽核假單（PATCH/PUT）成功
- [ ] LINE webhook 仍正常運作（推一條測試訊息給 LINE 觸發 callback → 看 audit_logs / 該 webhook 行為）
- [ ] 家長公開報名 (`/api/activity/public/register`) 正常運作（即使從第三方網域 embed 也應放行）
- [ ] curl 偽造 Origin 應被擋：`curl -X POST https://ivymanagesystem-api.zeabur.app/api/employees -H "Origin: http://evil.com" --cookie ...` → 預期 403
- [ ] curl 不帶 Origin 應被擋（同上但無 `-H Origin:`）→ 預期 403
- [ ] Sentry 7 天觀察 `CSRF reject` warning 量是否接近 0

---

## 9. 後續 follow-up（不在本 spec）

- 若 prod 觀察 CSRF reject 量穩定低、需 forensic trail → 加 `CSRF_REJECTED` audit action（接 audit middleware）
- 若日後新增 webhook（gov-moe / payment gateway / etc）→ 加進 `CSRF_EXEMPT_PREFIXES` 並 PR review 確認該 caller 有獨立 auth 機制
- 若 prod 觀察 false-positive（誤擋合法請求）→ 加 cors_origins entries 或調 bypass list（不該降為 allow-on-missing）
