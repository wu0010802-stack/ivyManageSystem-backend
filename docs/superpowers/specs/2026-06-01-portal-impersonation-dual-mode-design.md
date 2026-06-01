# 教師端後台檢視重構：雙模式 + 稽核歸屬

- **日期**：2026-06-01
- **範圍**：跨前後端（ivy-backend 為主，ivy-frontend 接上）
- **狀態**：設計定案，待寫實作計畫
- **相關**：`api/auth.py`（impersonate / refresh）、`utils/audit.py`、`utils/permissions.py`、前端 `PortalLayout.vue` / `AdminHeader.vue`

---

## 1. 背景與現況

後台目前提供「以某位老師身份進入教師端」的功能，技術上是**完整模擬登入（impersonation）**：

- 進入點：`AdminHeader`「進入前台」→（最高管理員）選擇瀏覽身份 → `POST /api/auth/impersonate { employee_id }`。
- 後端（`api/auth.py:393-511`）簽發**該老師的乾淨 JWT**（`auth.py:452-461`），把 admin 原 token 備份到 `admin_token`（httpOnly cookie），設定 `access_token` cookie（`auth.py:505`）。
- 退出：`POST /api/auth/end-impersonate`（`auth.py:1095`）從 `admin_token` 還原，且容忍過期 token（`decode_token_allow_expired`）。
- 防呆：不能模擬 admin／停用帳號／離職員工。
- 權限：只檢查 `role == "admin"`（`auth.py:400`），無 Permission 細分。

### 現況的真實問題（已實證）

1. **頭號缺陷 — 模擬期間的逐筆寫入全記成「老師本人」，沒有 admin 痕跡。**
   - 模擬簽發的是乾淨老師 JWT，**不帶任何 `impersonated_by` 標記**（`auth.py:452-461`）。
   - audit middleware 判斷 actor 完全靠 `access_token` 的 `user_id`（`utils/audit.py:776 → 436 → 448`），此時 access_token 即老師 token，middleware **從不讀 `admin_token`**。
   - 結果：admin 模擬老師 A 送假單／補打卡／改觀察紀錄 → `audit_log` 記成「老師 A 做的」，唯一 admin 線索是進入當下那一筆 entry row。要追回 admin 必須人工比對「進入/結束模擬時間窗 × 老師寫入時間」，無直接歸屬。對個資法／勞動法稽核是破口。

2. **預設即給完整寫入權，無唯讀模式。** 「純預覽／確認畫面」「管理巡檢」其實只需看，卻一進去就能以老師身份送任何東西 → 誤觸風險、權力過大。

3. **權限只有 `role==admin` 一個閘，無 Permission 細分。** 無法讓園長「能預覽、不能接管」。

4. **次要 — refresh 會洗掉 claim。** refresh 端點用 `_user` 欄位**從頭重建** access token（`auth.py:714-723`），不保留 claim。若加 impersonation claim 卻不處理 refresh，唯讀 token 一旦 refresh 就變回乾淨老師 token（偷偷升級 + 丟稽核歸屬）。現況 impersonate 未簽發 `staff_refresh_token`，教師身份天然無法走 staff rotation，但重構時必須明文守住此性質。

5. **既有「模擬中途切換老師」其實今天就壞了（實證）。** 前端 `PortalLayout` 的 in-portal 切換器假設「冒充狀態下後端用 admin_token Cookie 驗證操作者」（`PortalLayout.vue:248` 註解），但**全後端只有 impersonate（備份）與 end-impersonate（還原）會讀 admin_token，沒有任何端點拿 admin_token 當 auth**（`get_current_user` 只讀 access_token，`utils/auth.py:464`）。因此模擬中：
   - `handleSwitchUser` → `POST /api/auth/impersonate`，此時 access_token 是老師的 → `current_user.role != "admin"` → 403。
   - `fetchEmployees` → `GET /api/employees`（`require_staff_permission(EMPLOYEES_READ)`）→ 老師身份被擋 → 清單靜默變空。
   - **決策**：重構移除 in-portal 直接切換器，採「退出再進入」——看別位老師就退回後台再進。連帶簡化：模擬中永不再呼叫 `/impersonate`，操作者恆為進入當下 access_token 的 admin，無需 admin_token 操作者解析；員工清單在 AdminHeader（admin 端、token 有效）抓。

---

## 2. 目標與非目標

### 目標
- 將「一把大鎚」拆為**預覽（唯讀）** 與 **代為操作（可寫）** 兩個受控模式。
- 預覽：`admin` + `principal`（園長）可用；唯讀；看到的就是老師實際畫面。
- 代操作：僅 `admin`；不限範圍；全程橫幅提示 + **每筆寫入蓋上 admin 標記**。
- 集中修掉稽核歸屬缺陷（problem 1）。
- 明文守住「模擬 session 不可經 refresh 升級／洗掉歸屬」（problem 4）。

### 非目標（YAGNI）
- 不做代操作的「動作白名單／範圍限制」（決策：不限範圍，課責靠 audit）。
- 不做後台鏡像儀表板（採方案 1 沿用教師端頁面，畫面 100% 真實）。
- 不改 `admin_token` 回程票機制（維持現狀 + 容忍過期 token；admin_token 遺失列為已知小限制）。
- 不引入重量級 RBAC-impersonation 子系統（幼稚園規模，1–2 admin）。

---

## 3. 決策摘要

| 項目 | 決策 |
|------|------|
| 模型 | 雙模式：預覽 readonly / 代操作 write |
| 預覽權限 | admin + 園長(principal) |
| 代操作權限 | 僅 admin |
| 代操作範圍 | 不限制，但常駐橫幅 + 逐筆記 admin |
| 架構 | 方案 1：Token claim 雙模式（沿用模擬機制 + 教師端頁面） |

---

## 4. 詳細設計

### 4.1 Token claim（`api/auth.py`）
模擬簽發的 token 多帶三個 claim：
- `impersonated_by`：發起模擬的 admin `user_id`（int）
- `impersonated_by_name`：admin 顯示名（str）
- `impersonation_mode`：`"readonly"` 或 `"write"`

一般登入 token 不帶這三個 claim（`create_access_token` 用 `setdefault`，不影響既有路徑）。

### 4.2 新 Permission（`utils/permissions.py`）
- 新增 `Permission.PORTAL_PREVIEW`（值 `"PORTAL_PREVIEW"`，label「預覽教師端」）
- 新增 `Permission.PORTAL_IMPERSONATE`（值 `"PORTAL_IMPERSONATE"`，label「代為操作教師端」）
- `PERMISSION_LABELS` 同步補兩條。
- **角色模板**：`admin` 模板是 `["*"]`（WILDCARD，`utils/permissions.py:207`），`has_permission` 遇 `*` 直接回 True（`:605`）→ **admin 自動通過兩個守衛，不必改 admin 模板**。只需把 `PORTAL_PREVIEW` 加進 `principal` 模板（`:317` 附近）。`principal` 因而有 preview、無 impersonate。
- 前端 `hasPermission` 為純字串 `includes` 比對，新增權限只需兩端字串一致。

### 4.3 進入端點（`POST /api/auth/impersonate`）
- request body 改為 `{ employee_id: int, mode: "readonly" | "write" }`。
- **預設 `readonly`**（漏帶 mode 時走最安全模式）。
- 權限守衛（取代現有 `role=="admin"` 檢查）：
  - `mode == "write"` → 需 `PORTAL_IMPERSONATE`（admin 經 WILDCARD 通過；園長被擋）。
  - `mode == "readonly"` → 需 `PORTAL_PREVIEW`（admin + 園長通過）。
- 保留既有防呆：不能模擬 admin／停用帳號／離職員工。
- **操作者恆為 `current_user`（access_token）**：採「退出再進入」後，模擬中不會再呼叫本端點，進入當下 access_token 即發起 admin，無需 admin_token 解析。
- **防巢狀模擬**：若 `current_user` 已帶 `impersonated_by`（access_token 是模擬 token，理論上不該發生），回 409「請先退出目前模擬」。defense-in-depth。
- 簽發 target_token 時帶入 4.1 三個 claim（`impersonated_by` = 發起者 user_id、`impersonated_by_name` = 發起者 name、`impersonation_mode` = mode）。
- audit entry summary 補上模式，例如「[預覽] 操作者 X 檢視 老師A」/「[代操作] 操作者 X 切換為 老師A」。

### 4.4 唯讀守衛（全站）
- 新增一個輕量檢查：**當前 token `impersonation_mode == "readonly"` 且 HTTP method ∈ {POST, PUT, PATCH, DELETE} → 回 403**，友善訊息「唯讀預覽模式不可寫入」（走既有 BusinessError 風格）。
- **放全站**（middleware 或共用 auth dependency 層），不只 `/api/portal/*`——確保唯讀身份在任何端點都寫不了，避免漏網路徑。
- 實作位置候選：
  - (a) 一支 `ASGI/BaseHTTPMiddleware`，早於業務邏輯攔截；或
  - (b) 在 `get_current_user` 之後的共用 dependency 內檢查 `request.method` + claim。
  - 傾向 (a)：與既有 `AuditMiddleware` / kill_switch middleware 對齊，單點收斂；GET/HEAD/OPTIONS 放行。
- 防呆豁免：`POST /api/auth/end-impersonate`（退出模擬）必須放行，否則唯讀身份無法退出。`/api/auth/logout` 同理放行。

### 4.5 稽核歸屬（頭號缺陷修正）
- **資料模型**：`AuditLog`（`models/database.py`）新增兩欄：
  - `impersonated_by`：`Integer`, nullable
  - `impersonated_by_name`：`Text`, nullable
  - 一支 alembic migration（單 head；nullable 無 backfill 需求）。
- **寫入點**：三處取得 actor 時，一併從 token 解出 impersonation claim 並寫入：
  - `AuditMiddleware.dispatch` payload（`utils/audit.py:810` 附近）
  - `write_audit_in_session`（`:534`）
  - `write_explicit_audit`（`:598`）
  - 作法：`_extract_user_from_header` 旁新增 `_extract_impersonation_from_header`（共用 `decode_token_for_audit`，verify_exp=False），回 `(impersonated_by, impersonated_by_name)`；payload 補兩欄。
  - **actor 不變**：`user_id` 仍是老師（資料主體），額外兩欄標記「由誰代操作」。如此既有以 user_id 篩老師軌跡的查詢不破，又補回 admin 歸屬。
- **前端 AuditLogView**：當 `impersonated_by` 非空，顯示「老師A（代操作：王小明）」。

### 4.6 Refresh 防升級（`api/auth.py` refresh 路徑）
明文規則：**模擬 session 不可經 refresh 升級或洗掉歸屬。**
- 維持現況：impersonate 不簽發 `staff_refresh_token`（教師身份無 staff rotation 來源）。
- **決策（拍板）**：在 refresh 端點加防線——若**帶入的 access_token 含 `impersonated_by`**（用 `decode_token_for_audit` 靜默偵測），**拒絕該 refresh，回 401**，前端引導重新進入模擬。理由：模擬 session 短、有人監督，過期就請 admin 重新進入；「拒絕」比「保留 claim 重簽」更簡單，且零升級風險面。
- 不採「保留 claim 重簽」方案（會多一條需嚴防 readonly→write 升級的路徑，徒增風險面）。
- 測試必須涵蓋：帶 impersonation claim 的 access token 走 refresh → 回 401，**不會**產出乾淨無 claim 的 token。

### 4.7 前端（`ivy-frontend`）
- `src/api/auth.ts`：`impersonate(employeeId: number, mode: 'readonly' | 'write')`（對齊 OpenAPI 型別）。
- `AdminHeader.vue`（`:154-181`）：選老師後再選模式。**園長只看得到「預覽」**（依 `hasPermission('PORTAL_IMPERSONATE')` 決定是否顯示「代操作」選項）。
- `PortalLayout.vue`（`:231-289`）：**常駐橫幅 + 移除壞掉的切換器**
  - 橫幅：藍色「預覽中（唯讀）— 老師A」/ 紅橘色「代操作中（你的操作會被記錄）— 老師A」；一律含對象姓名 + **退出鈕**。
  - 唯讀模式：隱藏／禁用寫入按鈕（防呆，真正防線在後端 4.4）。
  - **移除 in-portal 直接切換器**（`handleSwitchUser` + 模擬中的 `fetchEmployees`）——對應 §1 problem 5 決策「退出再進入」。退出鈕走既有 `end-impersonate`。
- 模式來源：登入/impersonate 回應 `user` 物件補 `impersonation_mode`；以後端回傳為準。

---

## 5. 安全考量

- **唯讀真的寫不了**：靠後端全站守衛（4.4），前端隱藏按鈕僅為 UX 防呆，不作為安全邊界。
- **權限正確分流**：admin 經 WILDCARD 通過兩個守衛；園長僅 `PORTAL_PREVIEW` → write 模式被 403。需測試園長打 `mode=write` 被擋。
- **無 refresh 升級**：4.6 守住；測試覆蓋。
- **稽核完整**：每筆寫入可追回 admin（4.5）；進入/退出仍各記一筆。
- **既有防呆保留**：不能模擬 admin／停用／離職。

---

## 6. 測試計畫

### 後端 pytest
- 唯讀模式擋寫：portal 端點與**非 portal 端點**的 POST/PUT/PATCH/DELETE 皆回 403；GET 放行；`end-impersonate`/`logout` 放行。
- write 模式：寫入成功，且產生的 `audit_log` row `impersonated_by` = admin user_id、`impersonated_by_name` 正確。
- 權限分流：園長 `mode=readonly` 成功；園長 `mode=write` 403；admin 兩者皆成功。
- 防呆保留：模擬 admin／停用／離職皆被擋。
- refresh 不升級：帶 impersonation claim 的 token 走 refresh → 回 401，不產出乾淨 token（4.6 決策）。
- 進入 audit entry：summary 含模式。

### 前端 vitest
- 橫幅依模式渲染（顏色 + 文案 + 對象）。
- 唯讀模式隱藏/禁用寫入控制。
- `AdminHeader` 模式選項依權限顯示（園長無「代操作」）。
- `impersonate` API 正確帶 `mode`。

---

## 7. 邊界與已知限制

- **回程票**：`admin_token` cookie 遺失 → admin 卡在模擬身份（`end-impersonate` 容忍過期 token 緩解大半）；列為已知小限制，本次不處理。
- **多 worker**：唯讀守衛為純 stateless（讀 token claim），無跨 worker 狀態問題。
- **既有 audit row**：新增兩欄 nullable，歷史 row 為 NULL（無 backfill 需求，亦無法回溯）。

---

## 8. 實作順序（高階；細節留給實作計畫）

1. 後端 Permission（enum + label + principal 模板）。
2. 後端 token claim（impersonate 簽發帶 claim）+ 端點 mode 參數 + 權限守衛。
3. 後端唯讀全站守衛 middleware。
4. 後端 AuditLog 兩欄 + migration + 三處寫入點補 impersonation。
5. 後端 refresh 防升級。
6. 後端 pytest 全套。
7. 前端：api 型別 regen（OpenAPI）、AdminHeader 模式選擇、PortalLayout 橫幅 + 唯讀禁用、AuditLogView 顯示代操作者。
8. 前端 vitest。
9. 整合驗證（start.sh 起兩端、實際走預覽 + 代操作各一輪）。
10. 前後端分開 commit。

---

## 9. 跨端契約摘要

| 項目 | 後端 | 前端 |
|------|------|------|
| impersonate | `POST /api/auth/impersonate { employee_id, mode }` | `impersonate(employeeId, mode)` |
| 權限字串 | `PORTAL_PREVIEW` / `PORTAL_IMPERSONATE` | `hasPermission('PORTAL_IMPERSONATE')` 等 |
| 模式來源 | token claim `impersonation_mode` + user 回應欄位 | 橫幅與按鈕禁用依此 |
| 稽核欄位 | `AuditLog.impersonated_by` / `impersonated_by_name` | AuditLogView 顯示 |
