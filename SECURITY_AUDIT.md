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

### 🔴 HIGH-2：後端 site-packages 落後且 requirements.txt 下限過寬（實測 2026-05-11）

**位置**：`ivy-backend/requirements.txt` + 本機 site-packages

**描述**（實跑 `pip-audit` 於 2026-05-11 取得）：

兩個視角發現結果差異甚大：

| 視角 | 命令 | 發現 CVE 數 |
|------|------|-------------|
| 本機 site-packages 實裝版本 | `pip-audit` | **32** 個 CVE，跨 16 個套件 |
| requirements.txt 解析（fresh install） | `pip-audit -r requirements.txt` | **0** |

代表 CI / 部署做 fresh install 不會踩到（`>=` 解析吃 latest），但本機 / 長壽 venv 持續落後即實際暴露。

**直接依賴受影響清單**：

| 套件 | 舊下限 | 實裝（升前） | 修補版本（實裝升後） | 已修 CVE |
|------|--------|--------------|-----------------------|---------|
| `Pillow` | `>=11.0.0` | 12.1.0 | 12.2.0 | CVE-2026-25990, -40192, -42308/9/10/11 |
| `python-multipart` | `>=0.0.6` | 0.0.22 | 0.0.28 | CVE-2026-40347, -42561 |
| `python-dotenv` | `>=1.0.0` | 1.2.1 | 1.2.2 | CVE-2026-28684 |
| `requests` | `>=2.31.0` | 2.32.5 | 2.33.1 | CVE-2026-25645 |
| `pytest` | `>=7.4.0` | 9.0.2 | 9.0.3 | CVE-2025-71176 |

**間接依賴**（cryptography、tornado、urllib3、werkzeug、mako、gitpython、pyasn1、pygments、flask）由 transitive resolution 自然帶入 latest，無需釘版。

**無修補 CVE 風險評估**：

| 套件 | CVE | 評估 |
|------|-----|------|
| `ecdsa` | CVE-2024-23342 | **不影響本系統**。系統 JWT 使用 HS256 對稱演算法（`utils/auth.py:34`），`ecdsa` 僅為 `python-jose` 預設依賴被動帶入，運行時未走 ECDSA 簽章路徑，timing 側信道攻擊條件不成立。長期可考慮把 `python-jose` 換成 `pyjwt` 移除 `ecdsa` 依賴。 |
| `pip` | CVE-2026-3219 | 工具層 CVE，不在 application runtime。其餘兩個 pip CVE（1703 / 6357）已修在 26.0 / 26.1。CI 已自動升 pip；本機可 `python3 -m pip install --upgrade pip`。 |

**修補執行（2026-05-11）**：

- ✅ `requirements.txt` 直接依賴下限提升到修補版本，每行加 `# CVE-xxxx` 註解。
- ✅ `pip-audit -r requirements.txt` 再驗：0 CVE。
- ✅ 本機 site-packages 以 `python3 -m pip install --upgrade -r requirements.txt` 升級。連動升級：fastapi 0.128 → 0.136（8 minor）、pandas 2.3 → 3.0（**major**）、python-json-logger 2 → 4（**major**）、pydantic 2.12 → 2.13、cachetools 6 → 7。
- ✅ 後端 baseline + 升級後 pytest 各跑一次：**3055 passed, 4 skipped, 8 warnings**（兩次完全一致；升級後 9:30 比 baseline 10:24 反而快）。
- ✅ 該 3055 tests 覆蓋 FastAPI form 上傳路徑、JWT 簽章、圖片處理、Pandas 報表，等同完成高 blast-radius 套件 smoke。

**本機環境注意**：`/usr/local/bin/pip` shebang 指向 macOS CommandLineTools 的 Python 3.9，與 `pytest` / `python3` 使用的 Python 3.14 不一致；用 `pip install ...` 會走錯 Python 導致 install 失敗。本機升級必須用 `python3 -m pip install -r requirements.txt --upgrade`。CI 使用 Python 3.12（見 `.github/workflows/ci.yml`），無此問題。

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

---

## IDOR 全面盤查（2026-04-28，Phase 1 + Phase 2 完成）

對 ivy-backend 全部 API 路由的 IDOR 靜態盤查；威脅模型涵蓋：員工互查（a）、跨班教師（b）、家長跨家庭（c）、未認證公開（d）、高權限欄位級（e）。

### 文件
- 設計：`docs/superpowers/specs/2026-04-28-idor-audit-design.md`
- 盤查報告：`docs/superpowers/audits/2026-04-28-idor-findings.md`
- Phase 1 plan：`docs/superpowers/plans/2026-04-28-idor-audit-phase1.md`

### 結果摘要（共 46 筆 finding，F-001~F-046）

✅ **全部修補完成（46/46，2026-04-28）**

| 級別 | 筆數 | 狀態 |
|---|---:|---|
| Critical | 3 | ✅ 3/3 Fixed |
| High | 15 | ✅ 15/15 Fixed |
| Medium | 14 | ✅ 14/14 Fixed |
| Low | 14 | ✅ 14/14 Fixed |

### 修補執行歷程

依等級分批修補（Critical → High → Medium → Low），Critical/High 全配 pytest 回歸測試。共抽出下列共用 helper 實作：
- `utils/idor_guards.*`（員工自查 / 班級 scope / 家長綁定）
- `utils/attendance_guards.require_not_self_attendance`
- `utils/finance_response.mask_salary_fields`（解 F-017/F-031/F-036 同型）
- `utils/portfolio_access`（教師班級 scope）
- 多支 `assert_*_access` 與遮罩 helper

### 後續維運建議

- 新增 API 端點時，於 PR 自我檢查清單加入「IDOR 五項威脅模型」對照（a~e）
- 涉及他人資料／薪資／PII 的 GET 端點，預設疊加自我守衛或 `accessible_*_ids` scope
- 自訂角色 UI 警示業主：開出 STUDENTS_WRITE / USER_MANAGEMENT_WRITE / SALARY_READ 等敏感 bit 前需評估 blast radius

---

## 部署 SOP — 2026-05-12 Bug Sweep 後續

### 1. `students.id_number` partial unique index 後續 backfill 注意

Migration `20260511_v8a9b0c1d2e3_moe_phase1_schema.py:44-50` 建立了
`uq_students_id_number_notnull` 這條 partial unique index（`WHERE id_number IS NOT NULL`）。
建立當下因欄位剛新增、所有值皆為 NULL，index 不會撞到既有資料；但 Phase 2 之後若
透過匯入或人工 SQL 回填 `id_number`，**必須**先跑去重檢查再執行：

```sql
SELECT id_number, COUNT(*)
FROM students
WHERE id_number IS NOT NULL
GROUP BY id_number
HAVING COUNT(*) > 1;
```

若有結果，先逐筆人工釐清正本後再嘗試 backfill，否則 backfill 第一個重複的身分證
字號時 partial unique index 會觸發 violation 中斷整個 transaction。

### 2. 員工硬刪流程（被 RESTRICT 擋下時）

`b1c2d3e4f5a6` migration 把 `art_teacher_payroll_entries.employee_id`、
`disciplinary_actions.employee_id`、`special_education_subsidies.employee_id`
三條 FK 改為 `ondelete="RESTRICT"`，以保留金流流水。實際業務上員工以 soft-delete
為主；若 HR 確認真的要硬刪某 employees row，標準流程：

1. 先匯出該員工所有 `art_teacher_payroll_entries` / `disciplinary_actions` /
   `special_education_subsidies` / `salary_records` 到稽核保管位置（PDF + CSV 雙存）
2. 在這三張表上把該員工的紀錄歸檔到 archive 表或加 soft-delete 標記
3. 再執行 `DELETE FROM employees WHERE id = ?`

直接硬刪會被 FK 擋下，這是預期行為而非 bug。

### 3. `appraisal_events.created_by` RESTRICT 是設計而非疏漏

Migration `20260511_a1p2p3r4i5s6_appraisal_init.py:292-296` 將
`appraisal_events.created_by` 設為 `ondelete="RESTRICT"` 且 `nullable=False`，
與同表其他 audit FK（`reverted_by` / `supervisor_signed_by` / `finalized_by`
皆為 `SET NULL`）不一致。這是刻意設計：考核**事件**必有作者（法規責任），
與「動作的執行者」（reverted/supervisor_signed/finalized）不同；後者離職後
事件本身仍合法存在，只是這些動作的執行者欄位轉為匿名。

因此曾發起過考核事件的員工 user row 在離職後**無法硬刪**，必須先把
`appraisal_events.created_by` 歸檔（一般做法：在 events 表加 `created_by_archive`
欄位、把 username 字串複本寫過去，再把 FK 設成「離職管理員」代理 id），
這流程屬 HR + 法務 SOP，不由 schema 自動處理。

---

### F-STORAGE-001：Supabase Service Role Key 機敏處理（2026-05-13）

**Status:** Open（隨上線同步處理）

**Threat:** Service Role Key 等同 Supabase project root 權限。一旦外洩，攻擊者可讀寫所有 bucket、bypass RLS、刪除 DB 資料。

**Context:** 物件儲存遷移 PR（feat/object-storage-migration-backend）引入 `utils/supabase_storage.py`，prod 模式下 backend container 需透過 `SUPABASE_SERVICE_ROLE_KEY` 連 Supabase Storage 寫入/取 signed URL。

**Mitigation:**
- Key 只放 backend container env var，不 commit 任何 repo
- `.env.example` 僅放占位符與註解
- 後端日誌不輸出 key 值（已用 `os.getenv` 不串接到 log）
- 每 90 天輪替（見 `docs/sop/storage-deployment.md`）
- 前端絕無存取 service role key 的需要（只用 anon/publishable key）

**Verification:** `git grep -i "service_role\|SUPABASE_SERVICE"` 應只出現於 `.env.example`、`docs/sop/`、`SECURITY_AUDIT.md` 文件，不應有實際 key 值。

---

# 2026-06-02 全面 re-audit

**稽核日期**：2026-06-02
**被審基準**：後端 main（起跑 HEAD `8394f33`，過程中 user 並行推進至 `4deffcb`；新增 commit 僅動薪資補充保費檔，未觸及任何被審 authz 檔，findings 對 `4deffcb` 仍成立）；前端 main `d273c5d0`
**範圍**：4/17 後新增攻擊面 deep-trace 深審（冒充雙模式 / 家長 device-trust 登入 / 權限 row-level scoping / DB-driven 自訂角色 / 個資法 DSR·consent·醫療加密 / 批次加班·配號·lifecycle·才藝跨班）+ 全系統標準 OWASP 類別 regression 輕掃 + 4/17 後新安全基建 regression
**方法論**：11 個唯讀 deep-trace audit lane 平行（追實際 call chain、讀完整檔案，不靠 grep 推論）+ 專用 agent（migration / finance / cross-repo parity）+ postgres MCP 動態驗證 + High 候選 failing-test 重現。spec/plan：`docs/superpowers/specs|plans/2026-06-02-security-reaudit-*`
**稽核者**：Claude Code + Opus 4.8

## 總評

新攻擊面的**機密性 access control 體質仍紮實**：標準 OWASP 輕掃（L7）0 個 High/Medium 真陽性；IDOR 五威脅模型在家長端逐端點追 call chain 皆有 student scope 綁定；薪資金流計算正確性與授權無 P0（核心加解密、補充保費、考核年終、進位、proration、N+1、portal 自助薪資 IDOR 全過）；前後端 PII denylist 與權限字串集合無單側漂移。

本輪 finding 集中在三條主軸：(1) **權限 scope 設計 footgun**（非 scope-aware 權限授 `:own_class` 被當全域放行，已動態重現）；(2) **個資法合規控制的強制力落差**（consent 完全 fail-open、DSR 無決議端點故 opt-out/刪除/更正永不執行、醫療 reason-gate 對批量讀取路徑形同虛設、多項醫療級欄位仍明文、稽核表 FK 用 CASCADE 會在硬刪時連坐抹除）；(3) **限流 fail-open / 可繞過**（DB 失敗時 auth 限流歸零且 in-process backstop 是死碼；`TRUSTED_PROXY_IPS="*"` 使 per-IP 限流可能被偽造 XFF 繞過）。多數為「特權內部人誤用」或「合規完整性」性質，**非未認證外部可直接利用**；其中 RA-HIGH-3（園長可竄改全園家長 PII）為**預設角色即可觸發、非條件性**，是本輪最該優先處理者。

> **prod 驗證限制**：supabase prod MCP 本輪不可用（`Resource has been removed`），數項需 prod 資料/環境佐證的判定（scope grant 實際分布、醫療欄位 at-rest 是否真加密、Zeabur edge 的 XFF 行為、prod `COOKIE_SAMESITE`/`TRUSTED_PROXY_IPS` 值）標記為 **unverified-prod**，列於文末待 user 補驗。

## Findings（按嚴重度）

### 🔴 RA-HIGH-1：非 scope-aware 權限授 `:own_class` 被當全域放行（scope fail-open footgun）

- **位置**：`utils/permissions.py:600-622`（`has_permission`）+ `api/auth.py`（`POST/PUT /api/auth/users` 原樣存 `permission_names` 不驗 code/scope）
- **描述**：`has_permission` 對任何 code 一律 `any(p.startswith(f"{name}:"))`，**從不檢查該 code 是否真的 scope-aware**（`_SCOPE_AWARE_PREFIXES` 只用於 startup warning，runtime 不參考；真正會做 row 過濾的只有 `STUDENTS_*` / `PORTFOLIO_*` / `HEALTH` / `MEDICATION` / `DISMISSAL_*` 共 13 個 code）。把一個非 scope-aware 的敏感 code 授成 `:own_class`（例 `SALARY_READ:own_class`、`USER_MANAGEMENT_WRITE:own_class`）→ 端點完全不做 scope 過濾 → 等同授出**全域**權限。`:own_class` 後綴被靜默忽略。
- **攻擊情境**：管理者在權限 UI 想「只讓某老師看自班薪資」存成 `SALARY_READ:own_class`，實際上該老師可讀**全園**薪資；同理 `USER_MANAGEMENT_WRITE:own_class` → 全域使用者管理。屬靜默提權（UI 看似限本班、實為全域）。
- **嚴重度**：High（影響面＝被誤授 code 的全域權限；觸發前提為一筆 misgrant，需 `USER_MANAGEMENT_WRITE` 寫入）
- **驗證狀態**：**confirmed-機制**（動態重現 `.scratch/security-reaudit/repro/repro_L3_1_scope_failopen.py`：`has_permission(["SALARY_READ:own_class"],"SALARY_READ")==True`）。dev DB 實測**無**任何非 13-code 帶 scope 後綴的 grant → 目前為 latent footgun 非進行中洩漏；**prod grant 分布 unverified-prod**。
- **建議修法**：`has_permission` 對 scope 後綴 fail-closed——僅當 code ∈ 已知 scope-aware 集合才認 `:scope`，否則畸形/非預期後綴一律不放行；並在 `POST/PUT /api/auth/users` 寫入時驗證每個 entry 的 code 合法性與 scope 後綴只能掛在 scope-aware code 上。前端 `getPermissionScope` 已 fail-closed，後端對齊即可。

### 🟠 RA-HIGH-2（條件性）：`TRUSTED_PROXY_IPS="*"` + XFF 解析使 per-IP 限流可被繞過

- **位置**：`config/network.py:15`（預設 `TRUSTED_PROXY_IPS="*"`）+ `utils/`（`get_client_ip` 由右往左取第一個非信任 IP）
- **描述**：`ipaddress.ip_network("*")` 解析失敗 → fallback 只信任 RFC1918。可利用性取決於 prod ingress：**passthrough 透傳 client XFF（或無 edge）** 時，輪換偽造 `X-Forwarded-For` 即繞過所有 per-IP 限流（登入 / refresh / 改密 / 公開報名）；標準 append edge 把真實 IP append 在最右則反而擋下偽造（已 Python 雙情境實測）。
- **攻擊情境**：暴力破解者每次請求換偽造 XFF，使 per-IP 滑動視窗永不累積。
- **嚴重度**：條件 High / 預設 Low-Med（取決於 Zeabur edge 行為）
- **驗證狀態**：confirmed-程式路徑（Python 實測解析行為）；**prod 曝險 unverified-prod**（需 ops 佐證 edge 行為 + `TRUSTED_PROXY_IPS` 實際值）
- **建議修法**：prod 明設 `TRUSTED_PROXY_IPS` 為實際 edge IP/網段（勿留 `"*"`）；`get_client_ip` 改信任「最右側第 N 跳」而非由右掃第一個非信任值。配合 RA-MED-2 一併處理 auth 限流韌性。

### 🔴 RA-HIGH-3：園長（principal）可竄改全園任一學生的家長 PII（GUARDIANS_WRITE 寫入端點漏 scope 守衛）

> **驗證後自 Medium 升級**（2026-06-02，advisor catch）：原判「需 scoped grant 才越權」前提錯誤——`principal` 並非 unrestricted 角色，卻在預設模板就持有 `GUARDIANS_WRITE` → 開箱即用提權，無需任何 misconfig。

- **位置**：`api/students.py`（`create_guardian`:1438 / `update_guardian`:1481 / `delete_guardian`:1534）；對照 READ 端點 `assert_student_access`（:1404）有做、WRITE 三端點皆無
- **描述**：三個改家長 PII 的端點只掛 `require_staff_permission(GUARDIANS_WRITE)`，**完全不做 per-student 存取守衛**。`utils/portfolio_access.is_unrestricted` 的 `_UNRESTRICTED_ROLES` 僅 `{admin, hr, supervisor}`，**`principal` 不在內**（實測），但 `ROLE_TEMPLATES["principal"]` 含 `GUARDIANS_WRITE`（實測）。故園長 READ 家長受 `accessible_classroom_ids` 班級 scope 限制（只能看自己帶的班），WRITE/DELETE 卻無限制。同 codebase 的 `require_unrestricted_role` docstring 明訂「改家長電話只限 admin/hr/supervisor」，此三端點正好繞過該政策。
- **攻擊情境**：園長帳號（預設角色）只能讀自己班級的家長，卻可新增/竄改/刪除**全園任一學生**的家長電話 / 緊急聯絡人 / email。同理，若 RA-HIGH-1 被觸發（teacher 被授 `GUARDIANS_WRITE:own_class` 被當全域），此處 WRITE 無 scope 過濾使越權加倍。
- **嚴重度**：**High**（低門檻越權 + PII 寫入 + authz 守衛缺失，預設角色即可觸發）
- **驗證狀態**：**confirmed**（三重：實測 `ROLE_TEMPLATES["principal"]` 含 `GUARDIANS_WRITE`、實測 `is_unrestricted("principal")==False`、讀端點 body 確認 WRITE 無 `assert_student_access`）。完整 HTTP repro（principal token PATCH 他班 guardian 應 403 實得 200）列為 fix 階段回歸測試。
- **建議修法**：三個寫入端點補 `assert_student_access`（與 READ 對齊）或 `require_unrestricted_role`（若政策是只 admin/hr/supervisor 可改家長）；並評估把 `GUARDIANS_*` 納入 scope-aware 集合。

### 🟠 RA-MED-2：DB 失敗時 auth 限流完全 fail-open，且 in-process backstop 是死碼

> **✅ 已修（2026-06-04，governance-gaps）**：`utils/rate_limit_db.count_recent_attempts` 加 `fail_closed` 參數（:192）；DB 失敗時 auth scope caller 傳 `fail_closed=True`，改用 in-process backstop per-worker 滑動視窗計數（:214），降級而非歸零。`api/auth.py` 5 處（login IP / 帳號鎖 / 改密 IP+帳號 / 重設，:253/268/305/322/361）與 `api/parent_portal/auth.py` bind / device-setup（:240/459）皆已 fail-closed；非 auth scope 維持 fail-open（寧放行）。回歸測試 `tests/test_rate_limit_failclosed.py`（fail-closed 降級 / 預設 fail-open / backstop 無紀錄不誤鎖 三情境）。原 finding 文字保留供稽核追溯。

- **位置**：`utils/rate_limit_db.py`（`count_recent_attempts` 失敗回 0 → fail-open）；`api/auth.py:221-225`（`_ip_attempts`/`_account_failures` dict 註解宣稱是 fail-open 配套，實際 production 從不讀取，只有測試 `.clear()`）
- **描述**：login / 改密 / 重設 / 家長 bind 限流全走 DB-backed limiter；DB 一旦失敗，暴力破解防線歸零，且宣稱的 in-process backstop 並未生效。fail-open 範圍涵蓋所有 auth 端點且未縮限。
- **攻擊情境**：攻擊者先拖慢/打掛 limiter DB 寫入，再對 auth 端點無限制暴力破解。
- **嚴重度**：Medium（升至 High 若 RA-HIGH-2 同時成立）
- **驗證狀態**：confirmed → **✅ 已修（2026-06-04，見本節開頭 note）**（原判：grep 證 backstop dict 無 production reader + 讀 except 區塊確認 fail-open）
- **建議修法**：auth 端點限流 DB 失敗時 **fail-closed 或降級到真正生效的 in-process backstop**（把 backstop 接回 production 路徑）；非 auth 端點維持 fail-open。

### 🟠 RA-MED-3：醫療 reason-gate 對批量讀取路徑形同虛設

- **位置**：`api/students.py`（`GET /students/{id}` 等透明解密欄）vs 專用 `GET /students/{id}/medical`（reason gate + `medical_access_log`）
- **描述**：`allergy` / `medication` / `special_needs` 是 `EncryptedText` ORM 透明解密欄。持 `STUDENTS_HEALTH_READ` 者改打 `GET /students/{id}`、列表、`classrooms`、教師 portal（四條路徑已逐一確認**有 `mask_student_health_fields` 遮罩**，故非機密性破口）即拿到**相同明文**，**不需 reason、不寫 medical_access_log**。§6 特種個資取用稽核對最易濫用的批量路徑零覆蓋，專用 reason-gate 端點淪為 security theater。
- **嚴重度**：Medium（稽核完整性 / 個資法 §6，非機密性破口）
- **驗證狀態**：confirmed（逐檔確認 4 條讀取路徑遮罩存在 + 解密非經 gate）
- **建議修法**：解密路徑統一收斂——批量/detail 端點對醫療欄位預設遮罩或要求 reason，把 §6 access log 下移到解密 helper 層而非單一端點。

### 🟠 RA-MED-4：consent gating 完全 fail-open（零強制力）

- **位置**：全 codebase（`main.py` middleware stack 無 consent gate）；`api/parent_portal/consent.py`（僅記帳）
- **描述**：consent / policy 版本只是一本記錄帳本，**無任何 PII 讀寫被 consent 狀態擋下**。docstring 已自承 Phase 1 deferred，但對外宣稱「落地 consent」與 runtime 零強制力有落差。
- **嚴重度**：Medium（個資法合規）
- **驗證狀態**：confirmed（無 consent enforcement caller）
- **建議修法**：明確標示 consent 為「記錄但未強制」現況；若需強制，於 parent_portal 寫入/讀取路徑加 consent 版本 gate（fail-closed 設計需評估 LIFF UX）。

### 🟠 RA-MED-5：DSR 無 admin 決議端點 → opt-out/刪除/更正永不執行 + 提交端點漏家庭綁定

- **位置**：`api/parent_portal/dsr.py`（delete/correct/opt-out 提交端點）
- **描述**：整個 DSR 系統**沒有 admin 決議/執行端點**，申請永遠卡 pending：delete 不刪、correct 不改、opt-out 無效力（docstring 標 Phase 1 deferred）。此外 delete/correct 提交端點**不檢查 `subject_entity_id` 是否屬於該家長**——目前因無 resolver 故為 latent，一旦補上執行端點卻未加家庭綁定檢查即成跨家庭 IDOR。
- **嚴重度**：Medium（合規；補執行端點時若漏綁定升 High）
- **驗證狀態**：confirmed（無決議端點 + 提交端點無 ownership 檢查）
- **建議修法**：實作 admin DSR 決議 queue + 執行邏輯；執行/提交端點一律加 `subject_entity_id ∈ 該家長學生` 綁定檢查。

### 🟠 RA-MED-6：舊版 `POST /students/{id}/graduate` 繞過 lifecycle 狀態機

- **位置**：`api/students.py:1003-1092`（直接寫 `status/is_active/graduation_date`，未呼叫 `set_lifecycle_status`/`transition`）；前端 `StudentListPanel.vue` 為「設定離園」主路徑、active 使用中
- **描述**：違反 CLAUDE.md §9 不變量。單一根因兩後果：(1) `terminal_entered_at` 永遠 NULL → 家長 PII 365 天 retention GC 計時器**永不啟動**（個資法曝險：應抹除的家長 PII 永久留存）；(2) `update_student` 終態守衛只比對 `lifecycle_status`，經此路徑離園的學生仍可被任意改家長電話 / 緊急聯絡人（守衛被繞過、稽核斷鏈）。全域枚舉確認此為**唯一**轉入終態卻繞過狀態機的旁路。
- **嚴重度**：Medium
- **驗證狀態**：confirmed（讀端點 + 枚舉全部 lifecycle_status writer）
- **建議修法**：graduate 端點改走 `set_lifecycle_status`/`transition()`，確保 `terminal_entered_at` 設定 + 終態守衛一致。

### 🟠 RA-MED-7：家長 PII GC 未連動 token/User 生命週期 → 殭屍 device-trust token

- **位置**：PII retention GC（只設 `guardians.user_id=NULL`）；`api/parent_portal/auth.py`（refresh）；`revoke_guardian_devices`
- **描述**：GC 抹除家長 PII 時未撤銷 `ParentRefreshToken`、未停用 User、未 bump token_version。後果：device-trust token 變殭屍（`/api/parent/auth/refresh` 仍 200 並無限 rolling 續期）；且 `revoke_guardian_devices` 因 `guardian.user_id` 已 NULL 直接 `return {"revoked":0}` → admin 撤銷變靜默 no-op。另無 list-devices 端點，撤銷只能全撤、無法察覺殭屍。
- **嚴重度**：Medium
- **驗證狀態**：confirmed（追 GC + refresh + revoke call chain）
- **建議修法**：GC 連動撤銷 ParentRefreshToken + 停用 User + bump token_version；補 list-devices 端點。

### 🟠 RA-MED-8：device-trust refresh 無裝置/IP 綁定、30 天 rolling 永續

- **位置**：`api/parent_portal/auth.py`（refresh；UA/IP 欄位明寫「不參與決策」）
- **描述**：passwordless、30 天 rolling 永續續期，refresh 不比對 UA/IP。cookie 被竊後可換裝置、換 IP 無限期維持登入；reuse 偵測僅在合法使用者持續使用時才自癒。cookie 屬性（httpOnly/Secure/SameSite）本身正確。
- **嚴重度**：Medium
- **驗證狀態**：confirmed
- **建議修法**：refresh 加裝置指紋/IP 異常偵測或縮短絕對有效期 + 提供撤銷/裝置列表（與 RA-MED-7 合併處理）。

### 🟠 RA-MED-9：稽核/合規表 FK 誤用 `ON DELETE CASCADE`（硬刪連坐抹除）

- **位置**：`medacc01_medical_access_log.py:43` / `models/medical_access_log.py:46`（`medical_access_log.student_id` CASCADE）；`consent01:74`、`dsrreq01:40`（`user_id` CASCADE）
- **描述**：§6 醫療取用稽核、GDPR Art.7 同意證明、個資法 DSR 申請史皆為稽核性質，卻用 CASCADE。真實觸發點：`services/recruitment_funnel.py:253` 硬刪 student（連坐 medical_access_log）、`api/auth.py:1633 DELETE /users/{id}` 硬刪 user（連坐 consent/dsr）。同表 `user_id`(medacc)/`decided_by`(dsr) 刻意用 SET NULL「保留稽核」，且 `portalimp01` 對 audit_logs 故意不設 FK 保完整性——立場自相矛盾。
- **嚴重度**：Medium（個資法稽核完整性；P0 from migration-reviewer）
- **驗證狀態**：confirmed（逐 migration + 硬刪 caller 追蹤）
- **建議修法**：三條 FK 改 `RESTRICT` 或 `SET NULL` + 反正規化關鍵識別字串；硬刪前先 cold-storage 歸檔稽核。

### 🟠 RA-MED-10：多項醫療級欄位仍明文且不寫 §6 稽核

- **位置**：`StudentMeasurement`（身高/體重/頭圍/視力 明文 Numeric）、contact_book 體溫、結構化過敏表 `StudentAllergy`（嚴重度/症狀/急救說明全程明文）
- **描述**：與已加密的 `allergy`/`medication`/`special_needs` 同為特種個資，但未納入 `EncryptedText` 加密，且 `StudentAllergy` 不寫 §6 access log。
- **嚴重度**：Medium（記憶已知部分為 follow-up，但 `StudentAllergy` 整表明文為新發現）
- **驗證狀態**：confirmed（讀 model 定義）
- **建議修法**：評估納入 `EncryptedText` + §6 稽核（須配 backfill script + conftest test fernet key）。

### 🟡 RA-LOW（彙整）

| ID | 位置 | 摘要 | 來源 |
|----|------|------|------|
| RA-L1 | `api/guardians_admin.py:248-258` | `revoke_guardian_devices` 直寫 AuditLog 漏 `impersonated_by` 冒充歸屬（同檔另兩端點有）；種子假設「create_device_setup_code 漏 stamp」在 main **已修**，校正記憶 | L1/L2 |
| RA-L2 | `api/permissions_admin.py` | role/permission CRUD 無 caller-perms 子集檢查 + runtime 對 NULL-perms user 走 in-code ROLE_TEMPLATES 不讀 DB（撤權/授權失真、token_version bump 無效）；**兩者須綁一起修**，否則修後者會接成 High 提權 | L4 F-L4-01/05 |
| RA-L3 | `utils/permissions.py` | `require_scoped_permission` 為死碼（零 call site），scope 強制靠 handler 自覺呼叫 helper，非結構性護欄 | L4 F-L4-02 |
| RA-L4 | `medical_access_log` | reason gate `min_length=10` 不 trim，純空白可過；`student_id` CASCADE（見 RA-MED-9） | L5-08 |
| RA-L5 | device-trust | GC 後重簽設定碼會「復活」綁定並 re-link 仍存在的 Student PII（admin 只見 `[已離校家長]`）；per-guardian 碼兌換得 per-User token，多孩家庭授予全部小孩 | L2-2/L2-4 |
| RA-L6 | `api/students.py` | legacy null-seq 學生 `student_id` 可被竄改；display-cache 學號非 unique 可造重複 | L6-2 |
| RA-L7 | `BatchOvertimeCreate.employees` | 無 `max_length`（對照 portal 點名有 500 上限）；admin-only + rate limit 緩解 | L6-3 |
| RA-L8 | `utils/cookie.py` | `SameSite=None` 跨網域模式下 active-session CSRF（系統無獨立 CSRF token）；prod 值 unverified | L8-4 |
| RA-L9 | `api/activity/public.py` | honeypot/時序門檻信任 client `_ts`，不送即繞過；silent-reject 耗時差為理論側信道 | L8-5 |
| RA-L10 | `permscope01:129-181` | downgrade 非逆操作（無條件剝 teacher 所有權限 scope，破壞 permscope04 手動授權 + double-bump token_version） | migration P1 |
| RA-L11 | `rcrgeoconsent01:29-33` | backfill `geocoding_consent_at=created_at` 回溯捏造同意時間戳，需法務確認 | migration P1 |
| RA-L12 | frontend `schema.d.ts` | 落後後端契約 214 行，CI `openapi-drift` 會 fail；無 live PII 洩漏（runtime 已正確 POST），重跑 `npm run gen:api` 即解 | parity P1 |
| RA-L13 | sentry denylist 兩端 | `special_needs`（EncryptedText 特種個資）未列入 PII denylist（**雙端對稱漏、非單側漂移**）；兩端各加 `"special_needs"` + 補測試 | parity P2 |
| RA-L14 | `api/year_end/appraisal_payout.py` | 考核年終 payout 重生成未 wire `mark_salary_stale_from_month`，2 月補充保費基底可能 stale 後 finalize（目前靠 CLAUDE.md 人工約定） | finance P1 |
| RA-L15 | `POST /api/internal/uptime-webhook` | prod 被 CSRF middleware 擋（fail-closed 可用性 bug，非洩漏）：UptimeRobot 告警收不到，加 `CSRF_EXEMPT_PREFIXES` 修 | L7 INFO-1 |
| RA-L16 | `GET /api/auth/permissions` | 未認證可取權限定義 catalogue（code/label/role template，**非使用者指派**，無 PII） | L7 INFO-2 |

## 未發現重大風險（正向確認）

| 項目 | 評估 |
|------|------|
| 標準 OWASP 8 類別（authz 覆蓋 / IDOR / injection / XSS / secret / 上傳 / CSRF / mass-assignment） | ✅ L7 逐項追 call chain，0 High/Medium 真陽性 |
| 18 個檔案上傳端點 | ⚠→✅ magic-bytes + 白名單 + 大小 + 檔名 sanitize 一致；EXIF strip 原僅 jpg/jpeg/png/webp，HEIC/HEIF 原檔曾 raw 落盤保留 iPhone GPS（P2-4，2026-06-23 全系統資安掃描發現本列舊述過度）。**已修**：納入 image_sanitize（heic/heif 重 encode HEIF 去 EXIF）+ put_attachment 原檔落盤前單點清洗（覆蓋 10 caller）。`.gif` 容器無標準 EXIF GPS sub-IFD（威脅趨零）不處理 |
| 薪資金流計算正確性 + 授權 | ✅ 無 P0；補充保費/考核年終/進位/proration/N+1/portal 自助薪資 IDOR 全過 |
| `breakdown.supplementary_health_employee` / `appraisal_year_end_bonus` | ✅ 已落地 column（CLAUDE.md #11 的「未 persist」前提已過時，待更新文件） |
| PII denylist + 權限字串集合前後端同步 | ✅ 逐 token 比對零差異（除 RA-L13 雙端對稱漏） |
| JWT blocklist（logout 寫 jti / 三處查詢拒絕 / GC 只刪已過期） | ✅ 無撤銷 token 復活窗口 |
| 設定碼熵 + 一次性 claim + TTL + 失敗鎖 | ✅ secrets.choice 2^60 + sha256 + atomic + 24h + per-guardian cap 3，暴力不可行 |
| 冒充防護（readonly/write token HMAC 簽名不可竄改降級 / write 僅 admin / 禁冒充 admin·停用·離職 / refresh 封死冒充洗白） | ✅ 主路徑完整（缺口僅 RA-L1 單一 inline audit） |
| 配號競態（pg_advisory_xact_lock + unique constraint + IntegrityError 兜底） | ✅ 無重號/搶號 |
| 才藝跨班點名 | ✅ by-design，名冊只回姓名/班級/出席，未開放醫療·家長電話·身分證 |
| Alembic 鏈 | ✅ 單一 head、mergeheads 正確收斂、studnum01 unique-before-backfill 安全 |

## 建議修復優先順序

| 優先 | Finding | 主軸 | 預估 |
|------|---------|------|------|
| 1 | **RA-HIGH-3**（園長越權：GUARDIANS_WRITE 三端點補 `assert_student_access`/`require_unrestricted_role`）— 預設角色即可觸發，最優先 | 權限 | 1-2 小時 |
| 2 | **RA-HIGH-1**（`has_permission` 對非 scope-aware code fail-closed + `POST/PUT /api/auth/users` 驗證 code/scope + **前端 `auth.ts:189` 同步**，見下方跨端註） | 權限 | 半天 |
| 3 | ✅ **程式碼已修（2026-06-04）** RA-MED-2（auth 限流 DB 失敗 fail-closed）+ RA-HIGH-2（`utils/request_ip` 只信「明設」可信代理，預設 fallback RFC1918）；**殘留為部署動作：prod 須設 `TRUSTED_PROXY_IPS` env 為 Zeabur edge 出口 CIDR** | 限流 | 部署設定 |
| 4 | RA-MED-6（graduate 走 lifecycle）+ RA-MED-7（PII GC 連動 token 撤銷） | 個資法 PII 生命週期 | 半天 |
| 5 | RA-MED-9（稽核表 FK CASCADE → RESTRICT/SET NULL，需 prod migration） | 稽核完整性 | 半天（含 migration） |
| 6 | RA-MED-3（醫療解密收斂 §6 稽核）+ RA-L13（special_needs denylist 兩端） | 個資法醫療 | 半天 |
| 7 | RA-L2（role CRUD 子集檢查 + runtime 讀 DB，**兩者必須綁一起修**，否則修後者接成 High 提權） | 權限 | 半天 |
| 8 | RA-MED-4 / RA-MED-5（consent 強制 / DSR 決議端點）— 視合規時程，工程較大 | 個資法合規 | 1-2 天 |
| 9 | RA-MED-8 / RA-MED-10 / RA-L 其餘 | 強化 | 視項目 |

> **跨端註（RA-HIGH-1 fix 必讀）**：前端 `src/utils/auth.ts:189` 的 `hasPermission` 目前與後端 `has_permission` 同樣用 `startsWith(name+":")`，故「今日無漂移」。一旦後端改為「非 scope-aware code 的 `:own_class` fail-closed」，前後端會**反向漂移**：使用者看得到 UI（前端說有權）卻被後端 403。修 RA-HIGH-1 時必須把「哪些 code 是 scope-aware」這份集合放在兩端共用來源或明確同步，否則拿安全 footgun 換 UX 不一致。

## 待 user 補驗（unverified-prod / 業務確認）

1. **prod scope grant 分布**：查 `users.permission_names` / `roles.permissions` 是否有非 13-code 帶 `:` 後綴或畸形後綴（判定 RA-HIGH-1 是否已 live）。
2. **prod 醫療欄位 at-rest 是否真加密**：backfill script 為手動非 migration，`decrypt_medical` 對明文 legacy passthrough；確認 prod 已跑過 backfill（否則加密層形同未啟用且不報錯）。
3. **Zeabur edge XFF 行為 + prod `TRUSTED_PROXY_IPS` / `COOKIE_SAMESITE` 實際值**（判定 RA-HIGH-2 / RA-L8）。
4. **業務確認**：`supervisor_dividend` 納入二代健保補充保費累計（業主 2026-05-26 決策，與健保法定義有爭議）；`rcrgeoconsent01` 回溯視為同意是否站得住（法務）。
