# 後端安全收口（Security Hardening）設計

**日期**：2026-04-27
**狀態**：✅ Approved（透過互動 brainstorming 逐節確認）
**前置文件**：`SECURITY_AUDIT.md`（2026-04-17）

---

## 0. 範圍與目標

完成 SECURITY_AUDIT.md 全部 11 項 finding 的修復或具體緩解，並建立持續監控基建（CI 依賴掃描）。

### 範圍邊界
- ✅ **包含**：後端程式碼、後端 alembic migration、後端 CI workflow、前端 npm audit fix、前端 Vite build CSP hash 整合（conditional）、前端 `hasPermission` helper
- ❌ **不包含**：可觀測性、效能優化、大檔拆分（後續子項目）

### 非目標
- 不引入 Redis 或外部依賴（PG-based）
- 不改部署架構（HTML 仍由 Vite/Nginx serve）
- 不做 refresh token rotation
- 不做 reCAPTCHA

### 成功標準
1. SECURITY_AUDIT.md 11 項 finding 全部標記「✅ 已修復」或「✅ 已緩解（附說明）」
2. 後端 CI 跑 `pip-audit` 通過、前端 CI 跑 `npm audit --audit-level=moderate` 通過
3. 既有測試 + 新增測試（rate limiter、jti 黑名單、CSP header、honeypot）全綠
4. Production 部署後驗證：CSP header 不含 `'unsafe-eval'`；公開 API 500 不洩漏 stack trace

---

## 1. 資料模型

### Table: `rate_limit_buckets`
```sql
CREATE TABLE rate_limit_buckets (
    bucket_key TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (bucket_key, window_start)
);
CREATE INDEX ix_rate_limit_buckets_window_start ON rate_limit_buckets (window_start);
```
- `INSERT ... ON CONFLICT (bucket_key, window_start) DO UPDATE SET count = count + 1 RETURNING count`
- GC：每 5 分鐘刪 `window_start < now() - INTERVAL '1 hour'`
- 寫負擔評估：當前流量規模可吃下；預留 backend interface 換 Redis

### Table: `jwt_blocklist`
```sql
CREATE TABLE jwt_blocklist (
    jti TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT
);
CREATE INDEX ix_jwt_blocklist_expires_at ON jwt_blocklist (expires_at);
```
- Logout 寫入；驗 token 時 SELECT 1 命中則 401
- 舊 token 無 jti → fallback `token_version` + `expires_at`，**不強制重登**
- GC：每天刪 `expires_at < now()`

### Migration
`alembic/versions/2026_04_27_add_security_tables.py`，head 接續現有 chain。

---

## 2. 後端模組變更

### MEDIUM-1：500 訊息收口
6 個檔案的 `HTTPException(500, str(e))` 全替換為 `raise_safe_500(e)`：
- `api/activity/{public,registrations,settings,supplies,courses,inquiries}.py`
- 新增 `tests/test_safe_500.py`

### MEDIUM-2：CSP 收緊（conditional）
- `utils/security_headers.py`：`'unsafe-eval'` 確認移除；`script-src` 預設拿掉 `'unsafe-inline'`（路徑 A），環境變數 `CSP_SCRIPT_HASHES` 存在則改用 hash（路徑 B）
- `style-src 'unsafe-inline'` 保留（Element Plus / Vue scoped style）
- 實作期第一步：`npm run build && grep '<script[^>]*>[^<]' frontend-vue/dist/index.html` 決定走哪條路徑
- 新增 `tests/test_csp_headers.py`

### LOW-1：PG-based rate limiter
- `utils/rate_limit.py`：抽 `RateLimiterBackend` 介面；`PostgresRateLimiter`（預設）+ `InMemoryRateLimiter`（測試）
- `api/auth.py:82-83` 改用 limiter API
- `startup/scheduler.py` 註冊 `cleanup_rate_limit_buckets`（5 分鐘）
- 新增 `tests/test_rate_limit_pg.py`

### LOW-2：JWT jti 黑名單
- `utils/auth.py`：access/refresh token 確保有 `jti`；新增 `is_token_revoked(jti)`；`get_current_user` 解碼後查
- `api/auth.py`：新增 `POST /auth/logout`
- `startup/scheduler.py` 註冊 `cleanup_jwt_blocklist`（每天）
- 新增 `tests/test_jwt_blocklist.py`

### LOW-3：公開查詢 jitter
- `api/activity/public.py:295` `/public/query`：`await asyncio.sleep(random.uniform(0.2, 0.5))`
- 新增 `tests/test_public_query_jitter.py`

### LOW-4：公開報名 honeypot + 時序檢查
- `/public/register`、`/public/inquiries` 接受 `_hp` 與 `_ts`
  - `_hp` 非空 → silent reject 200
  - `now() - _ts < 3s` → silent reject 200
- 新增 `tests/test_public_honeypot.py`

### LOW-5：SameSite=Strict（**conditional**）
- `utils/cookie.py:18` `_COOKIE_SAMESITE = "strict"`
- 實作期需驗證 LIFF（家長 LINE 內嵌瀏覽器）行為；若 LIFF 受影響 → fallback `Lax + CSRF token` 雙保險
- 新增 `tests/test_cookie_samesite.py`

### LOW-6：Dev fallback secret 隨機產生
- `utils/auth.py:22`：移除硬編碼；dev 無 env → `secrets.token_urlsafe(64)` + `logger.warning`；prod 仍要求必填
- 新增 `tests/test_dev_secret.py`

### INFO-1：前端 `hasPermission` helper（見前端段）

### INFO-2：`/health/ready` 補 env
- `api/health.py` response 加 `"env": ENV`

---

## 3. 前端模組變更

### LOW-2 對應
- `src/api/auth.js`：`logout()` 呼叫 `POST /auth/logout`
- `src/store/auth.js`：logout action 先 await API 再清 store；網路失敗仍清

### LOW-4 對應
- `src/views/public/RegisterForm.vue`、`InquiryForm.vue`：隱形 `_hp` input + mounted `_ts` 時間戳

### INFO-1 對應
- `src/utils/permissions.js`：`hasPermission(maskBigInt, bit)` + `combinePermissions(...)` + `assertBigInt`
- `PERMISSIONS` 確認皆為 BigInt 字面量
- 至少改完薪資/才藝/權限管理三個高敏模組的 `& bit` 用法

### HIGH-1 對應
- `package.json`：`npm audit fix`；確認 build + test
- `.github/workflows/ci.yml`：`npm audit --production --audit-level=moderate`

### MEDIUM-2 路徑 B 對應（**僅在實測有 inline script 時觸發**）
- `vite-plugins/collect-csp-hashes.js`
- `.github/workflows/deploy.yml`：build 後 push hashes 到後端 env var

---

## 4. CI/CD 變更

### 後端 `.github/workflows/ci.yml`
```yaml
- name: 依賴 CVE 掃描
  run: |
    pip install pip-audit
    pip-audit -r requirements.txt --strict
```

### 前端 `.github/workflows/ci.yml`
```yaml
- name: 依賴 CVE 掃描
  run: npm audit --production --audit-level=moderate
```

### 規範文件更新
- `ivy-backend/CLAUDE.md`：「升級 dep 後 `pip-audit` 必過」
- `ivy-frontend/CLAUDE.md`：「升級 dep 後 `npm audit` 必過」
- `ivyManageSystem/CLAUDE.md`：跨端陷阱加「依賴升級必須兩端 CI 通過才合併」

---

## 5. 測試策略

| 主題 | 測試檔 | 重點 |
|------|--------|------|
| 500 收口 | `test_safe_500.py` | 6 端點觸發 500 → response 不含 stack trace |
| CSP | `test_csp_headers.py` | header 存在；無 `unsafe-eval`；`script-src` 不含 `unsafe-inline`（A）或包含 `sha256-`（B） |
| Rate limiter PG | `test_rate_limit_pg.py` | 併發 INSERT；視窗切齊；GC |
| JWT 黑名單 | `test_jwt_blocklist.py` | logout 後同 token → 401；舊無-jti token 仍可用 |
| Jitter | `test_public_query_jitter.py` | mock `asyncio.sleep` 確認被呼叫 |
| Honeypot | `test_public_honeypot.py` | `_hp` 非空 → 200 但無寫入；秒提交 → 同理 |
| Cookie SameSite | `test_cookie_samesite.py` | Set-Cookie 含 `SameSite=Strict` |
| Dev secret | `test_dev_secret.py` | dev 無 env → 啟動 OK；prod 無 env → 啟動失敗 |

既有 155 支測試必須全綠。

---

## 6. 風險與 Rollback

| 風險 | 緩解 |
|------|------|
| LIFF 受 SameSite=Strict 影響登入流程 | 實作期實測；若觸發 fallback Lax + CSRF |
| jti 黑名單表查詢延遲累積（DB pressure） | `expires_at` 索引 + 每日 GC；長期換 Redis |
| 移除 `script-src 'unsafe-inline'` 後第三方 SDK 注入 inline 失敗 | 路徑 B（hash）作為 fallback；實作期 build smoke test |
| `npm audit fix` 引入 breaking change | 升級後跑前端測試 + 手動 smoke test |
| dev secret 隨機產生 → 重啟 invalidate 所有 session | logger.warning 提醒；prod 仍要求顯式設定 |

### Rollback 策略
- Migration 可降版（每張表都有 `downgrade`）
- CSP / cookie / rate limit 都可透過 env var 強制 fallback 舊行為
- jti 黑名單未命中時 fallback 沿用 `token_version`，可單獨 disable 而不影響登入

---

## 7. 工時估算

| 群組 | 工時 |
|------|------|
| MEDIUM-1（500 收口） | 1 hr |
| MEDIUM-2（CSP）路徑 A | 0.5 hr |
| MEDIUM-2 路徑 B（若需要） | +1 day |
| LOW-1（PG rate limiter） | 4 hr |
| LOW-2（jti 黑名單） | 4 hr |
| LOW-3（jitter） | 0.5 hr |
| LOW-4（honeypot） | 2 hr |
| LOW-5（SameSite） | 1 hr |
| LOW-6（dev secret） | 0.5 hr |
| INFO-1（BigInt helper） | 2 hr |
| INFO-2（health env） | 0.25 hr |
| HIGH-1（CVE + CI） | 2 hr |
| 測試補強 | 4 hr |
| **總計（路徑 A）** | **~3 天** |
| **總計（路徑 B）** | **~4 天** |

---

## 8. 實作順序

依「無依賴 → 有依賴 → 跨 repo」分批：

**批次 1**（無依賴、低風險）
- INFO-2、LOW-3、LOW-6、MEDIUM-1

**批次 2**（需 migration）
- Alembic migration（新增兩張表）
- LOW-1（PG rate limiter）
- LOW-2（jti 黑名單）

**批次 3**（需實測）
- LOW-5（SameSite，先測 LIFF）
- MEDIUM-2（CSP，先 build 看 inline）
- LOW-4（honeypot，前後端同步）

**批次 4**（跨 repo + CI）
- HIGH-1（CVE 升 + CI）
- INFO-1（前端 BigInt helper）
- 三份 CLAUDE.md 規範更新
