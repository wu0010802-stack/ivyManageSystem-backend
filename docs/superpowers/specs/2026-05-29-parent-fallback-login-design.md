# 家長端 fallback 登入：無 LINE 裝置登入碼設計

**日期**：2026-05-29
**範圍**：後端為主（新資料表 + 端點 + 服務）＋前端（後台發碼/撤銷按鈕 + 家長無 LINE 登入頁）
**對應 audit finding**：#6「家長僅能透過 LINE LIFF 登入，無 fallback」

---

## 1. 問題與現況

家長端目前**只有一條登入路徑**：LINE LIFF。

- `POST /api/parent/auth/liff-login`：家長前端用 LIFF SDK 取 id_token → 後端 `LineLoginService.verify_id_token` 驗證 → 找/建 parent User → 以 `GuardianBindingCode` 完成 `/bind` 把 LINE 帳號綁到 guardian。
- parent User 的 `password_hash = "!LINE_ONLY"`（sentinel，`verify_password` 永不通過）→ **沒有任何非 LINE 登入路徑**。

**缺口**：根本不用 LINE 的家長（無智慧型手機、長輩代養、不裝 LINE）**完全進不去 portal**。這是真實支援痛點。

---

## 2. 已定決策（brainstorm 確認）

| 決策點 | 結論 |
|--------|------|
| 主要情境 | **永久無 LINE 的家長**（需可重複登入，非一次性緊急） |
| 長效憑證 | **一次性設定碼 → 裝置記憶（passwordless device-trust）**：staff 發碼，家長在裝置輸入一次，之後該裝置長效 token 自動登入 |
| 裝置記憶 TTL | **30 天 rolling**（沿用 LIFF 家長現有 `_REFRESH_TTL_DAYS=30`） |
| 後台發碼 UI | **攀附現有「產生 LINE 綁定碼」UI**（同一處加「無 LINE 裝置登入碼」選項） |
| 實作形狀 | **獨立資料表 + 共用機制 helper + 重用 refresh 基建**（不污染既有 `/bind` claim 邏輯，降低對 LINE 主路徑的風險） |

---

## 3. 架構與資料流

```
[首次設定]
  staff 後台（某 guardian）→ 產生「裝置登入碼」（8+ 碼明文僅回傳一次）
    → 口頭/紙本交給家長
  家長開「無 LINE 登入頁」（一般瀏覽器 URL，非 LIFF）→ 輸入設定碼
    → POST /api/parent/auth/device-setup（無需任何既有 auth）
    → 後端：IP 限流 + atomic 單次 claim → 找/建 parent User（link guardian.user_id）
            → 發 access token（body）+ ParentRefreshToken（HttpOnly cookie，30d）
    → 回與 liff-login 相同的 ParentLoginOut → 進入 portal（畫面/權限與 LIFF 家長完全一致）

[回訪同裝置]
  既有 POST /api/parent/auth/refresh（cookie rotation）→ 免再輸碼

[換裝置 / 清 cookie / 逾 30 天未訪]
  refresh 失效 → staff 重發設定碼

[遺失/被盜裝置]
  staff 後台「撤銷此家長所有裝置」→ revoke ParentRefreshToken family
```

**關鍵重用點**（皆已存在於 `api/parent_portal/auth.py`）：
- `_create_parent_user`：建 role=parent User（`password_hash="!LINE_ONLY"`）。fallback 路徑 display_name 取 `Guardian.name`（無 LINE 暱稱）。
- `_issue_refresh_token` / `ParentRefreshToken`：family rotation + reuse 偵測 + 30d TTL + HttpOnly/Secure cookie（path `/api/parent/auth`）。
- `POST /api/parent/auth/refresh`：rotation，**零改動**。
- `_get_parent_student_ids(session, user_id)`：以 `Guardian.user_id == user_id AND deleted_at IS NULL` 解學生 → **fallback User 只要 link 了 guardian.user_id 就完全可用，不需任何 LINE 綁定**（已驗證）。
- `_assert_student_owned(for_write=True)`：終態子女擋寫 → fallback 家長自動套用。

---

## 4. 資料模型

新表 `parent_device_setup_codes`（與 `guardian_binding_codes` 結構平行但語意分離，避免重載既有 `/bind` claim 邏輯）：

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | Integer PK | |
| `guardian_id` | FK → guardians.id | scoped 單一 guardian |
| `code_hash` | String(64) | sha256(明碼)；明碼僅產生時回傳一次 |
| `expires_at` | DateTime | 預設 now + 24h |
| `used_at` | DateTime nullable | 單次：claim 後填入 |
| `used_by_user_id` | FK → users.id nullable | claim 後落地的 parent User |
| `created_by` | FK → users.id | 發碼 staff |
| `created_at` | DateTime | |

共用機制抽到 helper（與 binding-code 共用，DRY 但不共用資料表）：
- `_generate_plain_code()` / `_hash_code()`（既有於 binding_admin.py，提取共用）。
- atomic 單次 claim（`UPDATE ... WHERE code_hash=? AND used_at IS NULL AND expires_at>now`，rowcount 判定成功；race 由 DB 保證）。
- per-guardian active cap（避免單一 guardian 堆積未用碼 → 409，沿用 binding-code 的 S4 限制）。

alembic migration：建單表，single head。

---

## 5. 端點

### 5.1 後台發碼（staff）
`POST /api/parent/.../{guardian_id}/device-setup-code`
- 權限：`require_staff_permission(Permission.GUARDIANS_WRITE)`（與 binding-code 一致）。
- 行為：鏡像 `create_binding_code`——產 8+ 碼、hash、寫 `parent_device_setup_codes`、per-guardian active cap → 409、寫 audit（`entity_type="parent_device_setup"`）。
- 回傳：`{ "code": "<明碼僅此次>", "expires_at": ... }`。

### 5.2 兌換設定碼（家長，無 auth）
`POST /api/parent/auth/device-setup`  body `{ "code": "<明碼>" }`
- **無需任何既有 auth**（這是 bootstrap 入口）。
- 行為：IP 限流 + 嘗試鎖 → atomic 單次 claim → **解析 parent User**（若 `guardian.user_id` 已存在 → 重用該 User，保留其既有多子女綁定/歷史，例如曾用 LINE 後失去存取者；否則建新 User 並回填 `guardian.user_id`，display_name=Guardian.name）→ `_issue_refresh_token`（新 family，30d）+ 發 access → 寫 audit。
- 回傳：與 `liff-login` 相同的 `ParentLoginOut`（access token + parent payload；refresh 走 cookie）。
- **錯誤回應採通用文案**（`碼無效或已過期`），**不分流具體原因**，避免碼枚舉（與既有 `/bind` 流程不同：那裡已被 LIFF auth gate，可給具體診斷；此處 ungated 必須防枚舉）。

### 5.3 撤銷裝置（staff）
`POST /api/parent/.../{guardian_id}/revoke-devices`
- 權限：`Permission.GUARDIANS_WRITE`。
- 行為：對 `guardian.user_id` 的所有未撤銷 `ParentRefreshToken` 設 `revoked_at`（family 全撤）→ 寫 audit。下次該裝置 `/refresh` 即 401。

---

## 6. 安全模型（核心）

| 層 | 機制 |
|----|------|
| **Bootstrap 秘密＝設定碼** | staff 發（人工信任錨：staff 認識家長本人）；guardian-scoped；sha256 hash at rest；單次（used_at）；短效（24h）；per-guardian active cap |
| **暴力防護（新攻擊面）** | device-setup 端點 **ungated**（不像 `/bind` 有 LIFF gate）→ 為唯一守門。必措施：(a) **per-IP 限流 + 嘗試鎖**（沿用既有 rate_limit/lockout 基建）；(b) **設定碼熵足夠**——LINE-bind 用 8 碼，device-setup 建議**提高到 10–12 碼**或限縮字符集為無歧義集再配強限流。24h 短效 + 同時未用碼數少 + 高熵 + 限流 → 線上暴力不可行。**最終長度/限流參數於 plan 階段定**。 |
| **持久秘密＝裝置 token** | 重用 `ParentRefreshToken`：HttpOnly/Secure/SameSite、384-bit、hash at rest、family rotation + reuse 偵測、30d rolling、可撤銷 |
| **撤銷** | staff `revoke-devices`（遺失/被盜）；PII GC 終態+365d 後 portal 自然回空 |
| **無密碼** | 無可釣魚/重設/外洩之密碼；遺失裝置＝staff 撤銷 + 重發碼 |
| **稽核** | 發碼 / 兌換（成功 + 失敗）/ 撤銷 全寫 audit log |

---

## 7. PII retention 互動

**完全不變**：與 LIFF 同。`Student.lifecycle_status` 終態 365 天後，家長端 PII（Guardian.phone/email/name/user_id）被 GC（CLAUDE.md #9）；`_get_parent_student_ids` 回空 → portal 空白（by design）。device 登入仍可登入但回空，與 LIFF 一致。

---

## 8. 前端範圍

- **後台**（攀現有綁定碼 UI）：在 guardian 綁定碼操作處加 (1)「產生無 LINE 裝置登入碼」按鈕（顯示明碼一次，可複製）(2)「撤銷此家長裝置」按鈕。
- **家長無 LINE 登入頁**：一般瀏覽器 URL（**不可是 LIFF 頁**——無 LINE 的家長開不了 LIFF），輸入設定碼 → 成功導入 portal。需與既有 LIFF 入口區隔。
- 型別走 OpenAPI codegen（後端落地後 `gen:api` → schema.d.ts）。

---

## 9. 權限

不新增 `Permission`：發碼/撤銷沿用 `GUARDIANS_WRITE`（與既有 binding-code 一致）。device-setup 兌換端點無 permission（公開 bootstrap）。

---

## 10. 測試策略（TDD）

- **純函式**：碼產生/hash、lockout 計數、atomic 單次 claim（並發 race 只一個成功）。
- **端點 integration**：
  - device-setup 成功 → 建 User + link guardian.user_id + 發 token + 可進 portal；
  - 過期碼 / 已用碼 / 不存在碼 → **通用錯誤（不洩露差異）**；
  - IP 限流 / 嘗試鎖觸發；
  - 兌換後 `_get_parent_student_ids` 正確解出該學生；
  - revoke-devices → 該 family `/refresh` 後 401；
  - 發碼端點：權限守衛、per-guardian cap → 409、audit。
- 修任何 bug 先補可重現回歸測試。

---

## 11. v1 範圍 vs follow-up

**v1（本 spec）**：單一 guardian 設定碼 → 該家長該學生；多子女沿用既有 `bind-additional`（device-setup 後已登入即可再綁更多碼）；staff 發碼 + 兌換 + 撤銷；30d 裝置 TTL。

**follow-up（不做）**：
- 家長**自助**請碼（需 SMS/email 基建——本系統目前無，暫不做）。
- 家長自設 PIN / 裝置清單自助管理。
- fallback 家長收 LINE 推播（本質做不到——他們不用 LINE；staff 須知此類家長走其他通知管道）。

---

## 12. plan 階段待定 / 待確認

1. **device-setup 設定碼長度/字符集 + 限流參數**（安全核心，§6）。
2. 後台發碼/撤銷 UI 的確切落點元件（攀附 binding-code 現有元件）。
3. device-setup 端點掛載路徑（`/api/parent/auth/device-setup`）與既有 router 註冊位置。
4. `_create_parent_user` 是否需小調整以接受「無 LINE name → 用 Guardian.name」的 display_name 來源。
5. 前端無 LINE 登入頁的 route 與既有 LIFF 入口的區隔方式。
