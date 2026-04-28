# 資安體檢報告 — ivyManageSystem

**稽核日期**：2026-04-17
**稽核範圍**：後端（FastAPI）+ 前端（Vue 3）全系統手動資安體檢
**稽核者**：Claude Code + Opus 4.7

---

## 總評

系統整體資安成熟度**偏高**，JWT 認證、PBKDF2 密碼雜湊、權限系統、IDOR 防護、路徑穿越防護、輸入驗證、WebSocket auth、LINE webhook 簽名、稽核日誌等皆已就位，且防禦深度（defense-in-depth）意識明顯。

本次發現的問題多屬**中、低風險**，主要集中在：

1. 錯誤訊息洩漏內部例外訊息（6 個公開端點）
2. CSP 使用 `'unsafe-inline'` / `'unsafe-eval'` 削弱 XSS 防護
3. 依賴套件有已知 CVE 需升級
4. Rate Limiter 為 in-process 版（已知限制）

---

## Findings 清單（按嚴重度排序）

---

### 🟠 MEDIUM-1：公開 API 的例外訊息外洩

**位置**：
- `backend/api/activity/public.py`（`/public/register`、`/public/update`、`/public/inquiries`）
- `backend/api/activity/registrations.py`
- `backend/api/activity/settings.py`
- `backend/api/activity/supplies.py`
- `backend/api/activity/courses.py`
- `backend/api/activity/inquiries.py`

**描述**：
`except Exception as e: raise HTTPException(status_code=500, detail=str(e))` 會把內部例外訊息（可能含 stack trace、SQL 錯誤、檔案路徑、DB 欄位名）原樣回給家長端，洩漏系統內部結構。

**攻擊情境**：
攻擊者送畸形 payload（如超長字串、特殊字元）觸發 DB 錯誤，從 500 回應中收集表名、欄位名、ORM 類別等資訊，協助後續攻擊。

**建議修法**：
改用專案已有的 `utils/errors.raise_safe_500(e)`（在 `auth.py`、`leaves.py` 已採用）。它會 log 原始例外但回傳通用訊息。

---

### 🟠 MEDIUM-2：CSP 允許 `unsafe-inline` / `unsafe-eval`

**位置**：`backend/utils/security_headers.py:27`

**描述**：
```
script-src 'self' 'unsafe-inline' 'unsafe-eval' https://maps.googleapis.com ...
```
`'unsafe-inline'` 允許 inline `<script>`，`'unsafe-eval'` 允許 `eval()` / `Function()`。兩者皆會讓 CSP 幾乎無法阻擋 XSS。

**攻擊情境**：
即使已仔細過濾 `v-html`，一旦有任何疏漏注入 `<script>alert(1)</script>`，瀏覽器會直接執行。若未開 `unsafe-inline`，瀏覽器會阻擋並回報 CSP violation。

**建議修法**：
- 短期：為必要的 inline script 改用 nonce（Vue 3 默認是 separate SFC，通常不需 inline）
- 長期：檢查是否有實際需要 `unsafe-eval`（Vue 生產版不需要），只有開發環境才放寬

---

### 🔴 HIGH-1：前端依賴有高風險 CVE（實測）

**位置**：`frontend-vue/package.json` + 傳遞依賴

**描述**（實跑 `npm audit --production` 於 2026-04-17 取得）：

| 套件 | Severity | CVSS | Advisory |
|------|----------|------|----------|
| `lodash` (transitive, <=4.17.23) | HIGH | 8.1 | GHSA-r5fr-rjxr-66jc（`_.template` Code Injection）|
| `lodash-es` (transitive, <=4.17.23) | HIGH | 8.1 | 同上 |
| `axios` 1.6.7 | MODERATE | 4.8 | GHSA-3p68-rc4w-qgx5（NO_PROXY Hostname Bypass → SSRF）|
| `axios` 1.6.7 | MODERATE | 4.8 | GHSA-fvcv-3m26-pcqx（Header Injection → Cloud Metadata Exfil）|
| `follow-redirects` (axios transitive, <=1.15.11) | MODERATE | N/A | GHSA-r4q5-vmmm-2653（Auth Header 跨域洩漏）|

lodash 的 `_.template` Code Injection 在本系統若未直接使用 `_.template()` 於 user input 則風險較低，但 `lodash-es` 若透過 Element Plus / vue-router 傳入 untrusted input 仍可能觸發。

**建議修法**：
```bash
cd frontend-vue && npm audit fix
# 或手動升級：
# axios        → >=1.15.0
# lodash       → >=4.17.24（或改用 lodash-es 最新版）
# follow-redirects → >=1.15.12
```

Backend 跑 `pip-audit -r requirements.txt` 結果：**No known vulnerabilities found**（撰寫時 python-jose 3.3.0 等無當前掛號 CVE）。

**建議**：在 backend 與 frontend 各自的 `.github/workflows/ci.yml` 加入：
- Backend：`pip install pip-audit && pip-audit -r requirements.txt`
- Frontend：`npm audit --production --audit-level=moderate`

---

### 🟡 LOW-1：Rate Limiter 為 in-process 版本

**位置**：`backend/utils/rate_limit.py`，`backend/api/auth.py:82-83`（登入限流 dict）

**描述**：
`_ip_attempts`、`_account_failures` 是 Python dict，多 worker / 多實例部署會各自計數，實際限流能力下降。CLAUDE.md 已註明此限制。

**攻擊情境**：
若未來擴展到 2+ uvicorn worker，登入暴力破解 / 公開 API 灌水的實際可容忍次數為設定值的 N 倍（N = worker 數）。

**建議修法**：
- 短期：維持單 worker 或在 Nginx 層加 `limit_req`
- 長期：改用 Redis-backed 限流（如 `fastapi-limiter` 或 `slowapi` + Redis）

---

### 🟡 LOW-2：JWT refresh 寬限期 2 小時

**位置**：`backend/utils/auth.py:30` — `JWT_REFRESH_GRACE_HOURS = 2`

**描述**：
Access token 15 分鐘過期後，仍有 2 小時寬限期可以 refresh。若 token 被盜（例如透過 httpOnly Cookie 繞道的 CSRF 或開發人員除錯洩漏），攻擊者取得過期 token 後仍有 2 小時可換發新 token。

雖然 `token_version` 機制可在帳號狀態變更時使舊 token 立即失效，但使用者本人不知情時無法主動廢止。

**建議修法**：
- 加上「已廢止 token 黑名單」（Redis TTL），`logout` 時寫入 jti
- 或把寬限期降到 15 分鐘以內
- 或實作 refresh token rotation（每次 refresh 簽發新 refresh token 並廢止舊的）

---

### 🟡 LOW-3：家長公開查詢可能被枚舉

**位置**：`backend/api/activity/public.py:295` — `/public/query`

**描述**：
`姓名 + 生日 + 家長手機`三欄比對，rate limit 為 10/60s/IP。雖然通用錯誤訊息不洩漏哪欄錯，但：
- 家長手機格式有限（台灣 09xx-xxx-xxx，~1000 萬組合）
- 若學生姓名外洩，攻擊者可用常見生日 × phone 暴力枚舉

對小型幼兒園整體風險可控（資料量小）；對大量資料仍有枚舉空間。

**建議修法**：
- 加上錯誤累積鎖定（同 IP 失敗 N 次後延長 window）
- 回應延遲加入 jitter（constant-time 較難，但加 200~500ms 隨機延遲足以擋低成本枚舉）
- 視需求引入 CAPTCHA（家長端 UX 影響較大，需 trade-off）

---

### 🟡 LOW-4：公開報名 / 提問缺乏機器人防護

**位置**：`backend/api/activity/public.py` — `/public/register`、`/public/inquiries`

**描述**：
報名 rate limit 5/60s/IP、提問 3/60s/IP，分散式攻擊（多 IP）仍可灌水。目前沒有 CAPTCHA / honeypot 欄位 / 時序檢查。

**攻擊情境**：
攻擊者用多個 IP 或 VPN 輪詢灌入大量假報名 / 垃圾提問，污染資料與佔用審核人力。

**建議修法**：
- 加入隱形 honeypot 欄位（bot 通常會填，真人不會看到）
- 加入提交時間差檢查（< 3 秒可能是 bot）
- 視量決定是否加 reCAPTCHA v3（分數式，家長無感）

---

### 🟡 LOW-5：SameSite=Lax 對 CSRF 仍有部分暴露

**位置**：`backend/utils/cookie.py:18` — `_COOKIE_SAMESITE = "lax"`

**描述**：
`Lax` 允許 top-level GET 跨站（例如 `<a href>`、`<form method=GET>`）。對於有狀態變更的 GET 端點（例如登出連結）可能被 CSRF 觸發。但此系統狀態變更都用 POST/PUT/DELETE，風險較低。

**建議修法**：
若無 cross-site 流程需求（純 SPA），改為 `SameSite=Strict`。

---

### 🟡 LOW-6：Dev Fallback Secret 硬編碼在原始碼

**位置**：`backend/utils/auth.py:22` — `"dev-only-insecure-key-do-not-use-in-production"`

**描述**：
開發環境 fallback JWT secret 寫死在原始碼中。雖然 CLAUDE.md 說明並有 logger.warning，但若有人把 dev secret 誤用到 staging / 測試環境，攻擊者可偽造 JWT。

**建議修法**：
`.env.example` 附 dummy secret，啟動時若 dev 環境也沒設 `JWT_SECRET_KEY` 就隨機產生一個（logger.warning 提醒每次重啟 session 都會失效）；或保留現況但在 `logger.warning` 層級提升至 CRITICAL。

---

### 🟢 INFO-1：permission 位元超過 JavaScript 32-bit 範圍

**位置**：`backend/utils/permissions.py:46-52`

**描述**：
`FEES_WRITE = 1 << 32` 以後的權限在 JS 端必須用 `BigInt` 處理；若前端誤用 `&` 運算子（32-bit 強制），會發生權限判斷錯誤。原始碼已有註解警告。

**建議**：
在 `frontend-vue/src/utils/` 新增統一的 `hasPermission(mask, bit)` helper，強制 BigInt，避免分散使用。

---

### 🟢 INFO-2：正式環境 `api/dev.py` 預設關閉

**位置**：`backend/main.py:271` — `if not _is_production(): app.include_router(dev_router)`

**描述**：
Dev 路由僅在 `ENV != production` 時掛載。若有人誤把 production 設為 `development`，會暴露薪資計算內部邏輯（雖仍需 `SETTINGS_READ` 權限）。

**建議**：
部署清單（deployment checklist）明列必須設 `ENV=production`。可在 `/health/ready` 端點順帶回報 `env` 值以便 SRE 檢查。

---

## 未發現重大風險的項目

以下經檢查後判定為**設計紮實、無可行攻擊面**：

| 項目 | 評估結果 |
|------|---------|
| JWT 認證（演算法、過期、簽名、token_version） | ✅ 實作完整，algorithm confusion 有 defense-in-depth |
| 密碼雜湊（PBKDF2 600k + timing-safe + dummy hash） | ✅ 符合 OWASP 2023 建議 |
| 登入限流（IP 滑動視窗 + 帳號鎖定） | ✅ 雙層保護 |
| 權限覆蓋率（40+ routers / 377 endpoints） | ✅ 無漏掉守衛，18 個公開端點皆為合理公開 |
| IDOR 防護（portal 強制 `emp.id` 過濾） | ✅ 教師端 portal 皆綁定 `current_user.employee_id`；**管理端橫向隔離（同 role 使用者間）未逐一驗證**，假設採「admin/hr/supervisor 可見全系統」RBAC 設計 |
| 檔案上傳（副檔名白名單 + magic bytes + 大小限制 + UUID 命名） | ✅ `utils/file_upload.py` 完整 |
| 路徑穿越防護（`_safe_attach_path` + resolve + relative_to） | ✅ 多處一致使用 |
| LINE Webhook 簽名驗證（HMAC-SHA256 + constant-time） | ✅ 未設 secret 時回 503 |
| WebSocket auth（httpOnly Cookie + token_version + role 白名單） | ✅ 完整 |
| SQL Injection（ORM 使用、無 user input 拼接 text()） | ✅ 未發現 |
| XSS（`v-html` 僅 2 處，皆已 escape 或靜態常數） | ✅ `highlight()` 做過 HTML escape |
| 敏感資料處理（銀行帳號遮罩、匯出需 SALARY_WRITE） | ✅ 有稽核日誌 |
| 冒充機制（禁止冒充 admin / 停用帳號 / 離職員工） | ✅ 防護完整 |
| Security Headers（nosniff、DENY、HSTS、Referrer-Policy） | ✅（CSP 可進一步收緊） |
| 秘密管理（.env 已加 gitignore、production 強制設 env） | ✅ |

---

## 建議修復優先順序

| 優先 | 項目 | 預估工時 |
|------|------|---------|
| 1 | HIGH-1：`npm audit fix` 升級 lodash / axios / follow-redirects；CI 加入 `npm audit` / `pip-audit` | 1 小時 |
| 2 | MEDIUM-1：把公開端點的 `HTTPException(500, str(e))` 改為 `raise_safe_500(e)` | 30 分鐘 |
| 3 | MEDIUM-2：CSP 收緊（移除 `unsafe-eval`，評估 `unsafe-inline` 是否能用 nonce） | 2~4 小時 |
| 4 | LOW-1：若要多 worker 部署，改 Redis-backed 限流 | 4 小時 |
| 5 | LOW-2：JWT token 黑名單 / refresh rotation | 1 天 |
| 6 | LOW-4：公開報名加 honeypot 欄位 | 1 小時 |
| 7 | LOW-5：評估 SameSite=Strict 可行性 | 30 分鐘 |

---

## 附註

- 本報告僅基於靜態程式碼審查，未進行動態滲透測試（DAST）、fuzz、或依賴 CVE 實測。
- 未涵蓋：資料庫權限設定、網路邊界（Nginx/Cloudflare）、備份加密、磁碟加密、日誌留存政策。
- 若要做完整合規（例如個資法、CMMC），建議另行委託專業滲透測試團隊。

---

## 修復追蹤（2026-04-28 安全收口子項目）

對應 `docs/superpowers/specs/2026-04-27-backend-security-hardening-design.md`。

| 編號 | 狀態 | 修復內容 | 對應檔案 |
|------|------|---------|---------|
| HIGH-1 | ✅ 已修 | 前端 `npm audit fix`（production deps 0 CVE）；後端 CI 加 `pip-audit --strict`、前端 CI 加 `npm audit --production --audit-level=moderate` | `.github/workflows/ci.yml`（兩端） |
| MEDIUM-1 | ✅ 已修 | audit 列出的 6 檔已先前修復；補修 `pos.py:716`、`api/health.py readiness`；新增 `tests/test_safe_500.py` 攔截回退 | `api/activity/pos.py`、`api/health.py` |
| MEDIUM-2 | ✅ 已修 | 移除 `script-src 'unsafe-inline'`（路徑 A，build 後 dist 無 inline script）；`unsafe-eval` 已先前移除；`CSP_SCRIPT_HASHES` env var 預留 hash fallback；`style-src 'unsafe-inline'` 工程取捨保留 | `utils/security_headers.py` |
| LOW-1 | ✅ 已修 | `PostgresLimiter` 抽出，`RATE_LIMIT_BACKEND=postgres` 切換；`security_gc_scheduler` 每 5 min 清舊視窗；DB 失敗 fail-open | `utils/rate_limit.py`、`services/security_gc_scheduler.py`、`models/security.py` |
| LOW-2 | ✅ 已修 | `create_access_token` 自動帶 jti；`get_current_user` / `verify_ws_token` 查 `jwt_blocklist`；logout 寫入 jti；每天 GC | `utils/auth.py`、`api/auth.py`、`models/security.py` |
| LOW-3 | ✅ 已修 | `/public/query` 加入 200~500ms 隨機延遲 | `api/activity/public.py` |
| LOW-4 | ✅ 已修 | `_hp` honeypot + `_ts` 時序檢查（< 3s 視為 bot）；silent reject 回偽裝成功訊息 | `api/activity/_shared.py`、`api/activity/public.py` |
| LOW-5 | ✅ 已修 | `SameSite=Strict` 預設；`COOKIE_SAMESITE=lax` env var 留作 LIFF fallback | `utils/cookie.py` |
| LOW-6 | ✅ 已修 | dev 環境無 `JWT_SECRET_KEY` 改用 `secrets.token_urlsafe(64)` 隨機產生（非硬編碼） | `utils/auth.py` |
| INFO-1 | ✅ 已修 | `permissionMaskHas/Add/Remove/Combine` 四個 BigInt-safe helper；`SettingsUsersTab.vue`、`ActivityAttendanceView.vue`、`Recruitment*View.vue` 改用 helper | `frontend-vue/src/utils/auth.js` |
| INFO-2 | ✅ 已修 | `/health/ready` 加 `env` 欄位；503 不再洩漏 `str(e)` | `api/health.py` |

新增測試：`test_safe_500.py`、`test_csp_headers.py`、`test_rate_limit_pg.py`、`test_jwt_blocklist.py`、`test_cookie_samesite.py`、`test_public_honeypot.py`、`tests/unit/utils/permission-mask-helpers.test.js`（前端）。
