# 家長端 LINE 登入長效 Refresh Token 設計

**日期**：2026-05-03
**範圍**：`ivy-backend` + `ivy-frontend`（家長 App）
**目標**：家長端 30 天免重登；access token 維持 15 分鐘短期；以 rotation + reuse detection 防 refresh token 失竊。

---

## 1. 背景與問題

家長端目前只發單一 `access_token`（15 分鐘），refresh 寬限期 2 小時，超過就必須重走 LIFF（彈 LINE OAuth → 跳出 → 回來）。家長使用頻率低（一天 1–2 次推播提醒），實際上 95% 的進站都會碰到「token 已過寬限期」場景，UX 差。

員工管理端（`/api/auth/refresh`）維持原本 2 小時寬限不動，因為員工每天高頻使用、資料機敏度高，短 session 是合理權衡。本案僅針對家長端。

## 2. 目標

- 家長 30 天內任何時間打開 App 都不需重登
- access token 短期化（仍 15 分鐘）保留撤銷的時效性
- refresh token 失竊可被偵測並全家族撤銷
- 多裝置並存（媽媽手機 + 爸爸手機 + 平板可同時登入）
- 員工端認證機制完全不動，互不干擾

非目標：

- 裝置指紋 / 異常地理偵測（誤判風險高，本案不做）
- 強制單裝置（家長場景不適用）

## 3. 設計總覽

### 3.1 雙 token 機制

```
LIFF 登入成功
    ├─ access_token  : 15 分鐘，httpOnly，path=/api
    └─ refresh_token : 30 天，   httpOnly，path=/api/parent/auth

任何 401 → 前端 interceptor 自動 POST /api/parent/auth/refresh
    ├─ 驗證 refresh token hash → rotation：發新 access + 新 refresh（同 family）
    └─ 若舊 token 已 used 又被送來 → reuse detection → 撤銷整個 family，401 重登
```

### 3.2 邊界區分

| 範圍 | refresh 端點 | TTL / Grace |
|---|---|---|
| 員工管理端 | `/api/auth/refresh`（不動） | 15min access + 2h grace（不動） |
| 家長端 | `/api/parent/auth/refresh`（新增） | 15min access + 30 天 refresh token |

兩端 cookie path 不同（`/api/auth` vs `/api/parent/auth`），不會互踩。

## 4. 資料模型

新增表 `parent_refresh_tokens`：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | BigInt PK | |
| `user_id` | FK → `users.id` ON DELETE CASCADE | 家長 User |
| `family_id` | UUID, NOT NULL | 同一裝置 rotation 鏈共用此 id |
| `token_hash` | String(64), UNIQUE NOT NULL | sha256(refresh_token raw)；DB 不存明文 |
| `parent_token_id` | FK → self.id ON DELETE SET NULL | 上一個 token（追溯 family） |
| `used_at` | DateTime, nullable | rotation 後填入；reuse 偵測看這欄 |
| `revoked_at` | DateTime, nullable | family 被撤銷時填入 |
| `expires_at` | DateTime, NOT NULL | 預設 `now() + interval '30 days'` |
| `created_at` | DateTime, NOT NULL, default now() | |
| `user_agent` | String(255), nullable | 觀測用，不參與決策 |
| `ip` | String(45), nullable | 觀測用，IPv6 預留 |

**Index**：

- `UNIQUE (token_hash)`
- `(user_id, family_id)`：logout / 全家族 revoke 時使用
- `(expires_at)`：定期 GC

**Migration**：alembic `parent_refresh_tokens` 新檔，沿用 `inspect().get_table_names()` 防重跑慣例。

## 5. 端點變更

### 5.1 修改：`POST /api/parent/auth/liff-login`

成功路徑（user 已存在 / role=parent）多做：

1. 產生 `refresh_token = secrets.token_urlsafe(48)`
2. 寫一筆 `parent_refresh_tokens(user_id, family_id=uuid4(), token_hash=sha256(refresh_token), expires_at=now+30d, user_agent, ip)`
3. `Set-Cookie: parent_refresh_token=<raw>; Path=/api/parent/auth; HttpOnly; SameSite=Lax; Secure(prod); Max-Age=2592000`

### 5.2 修改：`POST /api/parent/auth/bind`

與 5.1 同樣處理：bind 成功 commit 後追加發 refresh token。

### 5.3 新增：`POST /api/parent/auth/refresh`

```
1. 讀 cookie parent_refresh_token；缺 → 401
2. token_hash = sha256(raw)
3. SELECT ... FROM parent_refresh_tokens WHERE token_hash = ? FOR UPDATE
   ├─ 不存在 → 401
   ├─ revoked_at IS NOT NULL → 401
   ├─ expires_at < now → 401
   ├─ used_at IS NOT NULL ⚠ REUSE：
   │    ├─ UPDATE parent_refresh_tokens SET revoked_at=now WHERE family_id=? AND revoked_at IS NULL
   │    ├─ user.token_version += 1（讓還在飛的 access token 也廢）
   │    ├─ logger.warning("[parent-refresh] REUSE detected, family revoked")
   │    └─ raise 401
   └─ 正常：進 rotation
4. Rotation（同一 transaction，FOR UPDATE 鎖在 step 3 取得）：
   a. 舊 row: used_at = now()
   b. 新 token raw = secrets.token_urlsafe(48)
   c. INSERT 新 row(user_id, family_id=舊.family_id, token_hash, parent_token_id=舊.id, expires_at=now+30d)
   d. user.last_login = now
   e. 取最新 user.permissions / token_version 重發 access_token cookie
   f. Set-Cookie 新 refresh_token
5. response: { "ok": true, "user": { user_id, name, role } }
```

#### 5.3.1 並發 race（同裝置雙 tab）

兩個請求同時拿同一個 refresh token 來 refresh：

- 第一個請求拿到 `FOR UPDATE` 鎖、把 used_at 填上、commit、回應
- 第二個請求等鎖、進來時看到 used_at 已填 → 觸發 reuse 路徑會誤殺

對策：在 reuse 分支判斷 `used_at` 與 `now` 的距離，**5 秒內**視為合法 race，不 revoke、不發新 token，直接回 409 `RACE_PLEASE_RETRY` 給前端。前端 interceptor 看到 409 直接重打原請求一次（此時新 access token 已透過第一個 refresh response 寫入 cookie）。

> 為什麼不直接回剛換出的新 token？因為 race 第二個 request 並未拿到新 token 的 raw 值（DB 只存 hash），無法在 cookie 寫入新 raw。重打原請求方案最簡單且安全。

### 5.4 修改：`POST /api/parent/auth/logout`

既有邏輯（清 access cookie + bump token_version）保留，新增：

1. 讀 `parent_refresh_token` cookie
2. 若存在：`UPDATE parent_refresh_tokens SET revoked_at=now WHERE family_id=(SELECT family_id FROM ... WHERE token_hash=?) AND revoked_at IS NULL`
3. 清 `parent_refresh_token` cookie

只 revoke 當前裝置的 family；其他裝置不受影響（多裝置並存策略）。

## 6. 前端變更（`ivy-frontend`）

### 6.1 `src/parent/api/index.js`

```js
function _doRefresh() {
  return axios.post('/api/parent/auth/refresh', null, {
    withCredentials: true, timeout: 10000,
  }).then(() => true)
}

const isAuthEndpoint =
  url.includes('/parent/auth/liff-login') ||
  url.includes('/parent/auth/bind') ||
  url.includes('/parent/auth/refresh') // 加這行避免遞迴
```

並對 `409 RACE_PLEASE_RETRY` 加一次重試（最多 1 次）。

### 6.2 LoginView.vue

不動。LIFF 流程結束後，後端已自動寫入兩個 cookie。

### 6.3 logout 流程

`useParentAuthStore` 的 logout action 不動（仍呼叫 `/api/parent/auth/logout`），cookie 由後端清。

## 7. 安全考量

| 威脅 | 對策 |
|---|---|
| Refresh token cookie 被偷（XSS / 中間人） | HttpOnly + Secure + Path 限制；rotation + reuse detection 偵測異常使用 |
| Reuse detection 誤判（race） | 5 秒寬容窗 + 409 重試（5.3.1） |
| 帳號停用要立即生效 | 既有 `token_version` bump 機制；refresh 時校驗 `user.is_active` 與 `token_version` |
| Refresh token 落 log | raw 永不寫 log，只寫 hash 前 8 字元 + family_id |
| 暴力試 token | refresh token 是 256-bit 隨機（`secrets.token_urlsafe(48)`），無法暴力 |
| GC 與隱私 | 過期 7 天後 DELETE（保留 7 天供事後稽核）；`user_agent` / `ip` 僅觀測用，不參與決策 |

## 8. 既有家長兼容

不做 migration。部署後：

- 既有家長下次 access_token 過期 → refresh 失敗（沒有 refresh token）→ 跳回 LIFF 重登一次 → 重登後就拿到 refresh token，之後 30 天免重登

單次成本可接受，避免一次性 INSERT 所有家長 refresh token 的隱私風險。

## 9. 測試策略

### 9.1 後端 `tests/test_parent_auth_refresh.py`（新增）

| 場景 | 斷言 |
|---|---|
| 正常 rotation | 舊 token used_at 寫入；新 token 可用；舊 token 不能再用 |
| Reuse detection | 拿 used token 二次 refresh → family 全 revoke、user.token_version +1 |
| 多裝置 family 隔離 | family A 被 reuse 踢，family B 仍可用 |
| Refresh token 過期 | 401 |
| 已 revoked family 內任一 token | 401 |
| Logout 只踢當下 family | 其他 family 仍可 refresh |
| 並發 race（5 秒內） | 第二個請求收 409，不 revoke |
| Reuse > 5 秒 | 觸發完整 family revoke |
| 帳號 disabled | refresh 401 |

### 9.2 後端 `tests/test_parent_auth.py`（補強）

- liff-login 成功時要寫入 `parent_refresh_token` cookie
- bind 成功時同上
- logout 時要清 `parent_refresh_token` cookie 並 revoke family

### 9.3 前端 `src/parent/__tests__/parentAuth.refresh.test.js`（新增）

- 401 觸發 interceptor 打 `/api/parent/auth/refresh`，refresh 成功後重打原請求
- 409 RACE_PLEASE_RETRY → 重試 1 次原請求
- 並發兩個 401 共用同一個 `_refreshing` Promise（去重）
- refresh 自身 401 → 不遞迴，跳 `#/login`

## 10. 部署與監控

- Migration：`alembic upgrade head`（新檔）
- Log 觀測指標：
  - `[parent-refresh] rotation user_id=X family_id=...`（INFO）
  - `[parent-refresh] REUSE detected user_id=X family_id=...`（WARNING）
  - `[parent-refresh] race-tolerated user_id=X`（DEBUG）
- 排程或 cron：每日 04:00 `DELETE FROM parent_refresh_tokens WHERE expires_at < now() - interval '7 days'`（可走既有 cron 機制）

## 11. 不做的事（YAGNI）

- 裝置指紋（user_agent / ip 只記錄不決策）
- 「登出所有裝置」按鈕（家長端用 A 方案，未需要）
- Refresh token sliding TTL（rotation 已等價滑動 30 天）
- Audit log（refresh 過於高頻，落 log 不經濟；異常情境用 logger.warning 即可）

## 12. 變更檔案清單

**後端**

- `alembic/versions/<新>_create_parent_refresh_tokens.py`（新）
- `models/parent_refresh_token.py`（新）
- `models/database.py`：import 新模型
- `models/user.py`：加 `refresh_tokens` relationship（或選擇不加，省略 ORM cascade）
- `api/parent_portal/auth.py`：liff-login / bind 加發 refresh、logout 加 revoke、新增 `/refresh` 端點
- `tests/test_parent_auth_refresh.py`（新）
- `tests/test_parent_auth.py`（補強）

**前端**

- `src/parent/api/index.js`：refresh 端點換成 `/api/parent/auth/refresh`、加 409 重試
- `src/parent/__tests__/parentAuth.refresh.test.js`（新）

## 13. Commit 策略

依專案慣例兩 repo 各一支分支，commit 分開：

- `ivy-backend`：`feat/parent-refresh-token-v1` → 三個 commit（migration / model+endpoint / tests）
- `ivy-frontend`：`feat/parent-refresh-token-v1` → 兩個 commit（interceptor / tests）

CI 兩端各跑 pytest / vitest 後合併。
