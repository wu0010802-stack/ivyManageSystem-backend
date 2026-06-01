# 課後才藝系統上線前風險修正 — Design Spec

- 日期：2026-06-01
- 範圍：跨 `ivy-backend`（主）+ `ivy-frontend`（A3 一處）
- 來源：4 個唯讀 audit agent 平行深查 + 親自驗證 P0 後產出的上線風險清單（聚焦家長端 public 報名 + 金流 POS/退費）
- 狀態：design approved，待寫 plan

---

## 1. 背景與目標

「課後才藝」模組已做過大量硬化（超賣鎖、退費三段計算 + 簽核閘、查詢碼 hash、枚舉 oracle 防護、idempotency UNIQUE、path traversal、500 不洩漏，54 個測試檔守著）。本次 audit 找到的是**少數新缺口**，集中在三面向：①單 worker 的 async/同步阻塞 ②PII 入 log ③兩條後台路徑的資料正確性。

**目標**：把這些缺口全部修掉，讓系統可安全上線；每條修正都先補能重現的回歸測試再修（TDD），分開 commit。

**非目標**：重構既有已硬化邏輯；改動 audit 已確認安全的部分；UI/UX 改版。

## 2. 兩條 Workstream

- **A — 才藝上線路徑外科修正**：已審查、自包含、各自 TDD、分開 commit。
- **B — 全 app `async def`→`def` 遷移**：交叉、機械、以完整測試套件當 gate，**獨立 commit/PR**。public.py 的 async 轉換歸到 B（避免與 A 重複改檔）。

---

## 3. Workstream A — 外科修正

> 通則：每項先寫能重現問題的回歸測試（紅）→ 修（綠）→ 分開 commit。被 audit 列為「已涵蓋」的項目不動。

### A2 — PII 入 log（P1）
- 問題：`api/activity/public.py:681`（`student=%s` 印 `reg.student_name`，每筆成功報名都記）、`:466-470`（silent-reject 印 `name=%r phone=%r`）、`:1073`、`:1251-1255`（inquiry name+phone）。`utils/sentry_init._PII_KEY_SUBSTRINGS` 只有 `student_name` 沒有裸 `student` → `student=` 逃過遮罩，幼兒全名原文進 Zeabur stdout log；啟用 Sentry 後經 breadcrumb egress 第三方（CLAUDE.md 陷阱 #8）。
- 修正：log 改記 `reg.id` + operator，不印姓名/電話/生日。silent-reject 只記事件 + IP（可選遮罩末 3 碼手機）。順帶解 P2 的「`name=%r phone=%r` 被 redaction filter 改成 placeholder 數不符 → `TypeError` 整行靜默丟失」（移除 PII 後 format 不再被改）。
- 測試：新增 redaction 回歸測試，斷言這些 log 不含 PII 值、且不拋 TypeError。

### A3 — `GET /public/query` → `POST`（P1）
- 問題：`public.py:340` GET，姓名+生日+家長手機進 URL query string → access log / 瀏覽器歷史 / Referer。與同檔 `query-by-token`（POST，docstring 明說避免進 log）自相矛盾。
- 修正：後端改 `@router.post("/public/query")`，三欄移到 Pydantic body（沿用既有限流 `_public_query_limiter` 與隨機延遲 + 統一回應）。前端 `ivy-frontend/src/api/activityPublic.ts:18` `api.get(..., {params})` → `api.post(..., body)`，呼叫端簽章不變（5 個 call site 不動）。
- 測試：後端端點測試（POST 正確、舊 GET 404/405）；前端 vitest（function mock 簽章不變，補一條斷言走 post）。

### A4 — `reject → 名額被補 → restore` 超賣（P1，本次最該修的資料正確性 bug）
- 問題：`reject_registration`（`registrations_pending.py:378-386`）只翻 `is_active=False`，不動 `RegistrationCourse` 列、不遞補；被拒報名的 `enrolled` RC 列保留。`restore_registration`（`:689-695`）翻回 `is_active=True`，唯一守衛是同名同生日 dedup，**不檢查容量、不鎖課程、不重算** → enrolled = capacity + 1（可累積）。
- 修正：`restore_registration` commit 前，對該 reg 每門 `enrolled`/`promoted_pending` 課程 `with_for_update` 鎖 `ActivityCourse` 列，重數佔位（排除本 reg），超出 capacity 者把該 RC 降級 `waitlist`（沿用 `_attach_courses` 的容量判定語意）。
- 測試：新增重現超賣的回歸測試（reject 佔位 reg → 名額補滿 → restore → 斷言不超賣、超出降 waitlist）。

### A5 — 學生離園/退學軟刪後不遞補候補（P1）
- 問題：`services/activity_student_sync.py:265` `_soft_delete_single_registration` 只設 `reg.is_active=False`，不呼叫 `_auto_promote_first_waitlist`（對比 `delete_registration:1056`、`public_update:971` 都會遞補）→ 名額空出但候補卡死。
- 修正：軟刪 + flush 後，對該 reg 原 `enrolled`/`promoted_pending` 課程逐一 `_auto_promote_first_waitlist(session, course_id)`。
- 測試：新增「軟刪釋出 → 候補第一位自動升上」回歸測試。

### A6 — 退費 idempotency 無 key fallback（P1）
- 問題：`idempotency_key: Optional[str]`（`pos.py:149`、`schemas/activity_admin.py:276`）；無 key 時退費可雙重出帳並繞過累積簽核。advisory lock 只序列化不去重。
- 決策：**伺服器端自動去重 fallback（不破壞契約）**。
- 修正：退費/繳費 POST 在 `idempotency_key is None` 時，以 (reg_id, type, amount, payment_date, operator) 在短窗（建議 60s）內查既有紀錄判定 replay；命中則回放既有結果（與帶 key 的 replay 行為一致）。官方 UI 仍照常帶 key 不受影響。
- 測試：新增「無 key 雙送 → 第二筆判 replay、不重複出帳、不繞簽核」回歸測試。

### A7 — `update_payment` 全額沖帳納入退費簽核閘（P1）
- 問題：`registrations_payments.py:159-200` 的 `is_paid=false` + `confirm_refund_amount=paid` 全額沖帳只跑 reason + cumulative 兩道閘，**未過** `require_approve_for_refund_diff`（同金額走 POS / `add_registration_payment` 會被擋）→ 未簽核退費旁路。
- 決策：**納入退費 diff 簽核閘**。
- 修正：此路徑補呼叫 `services.activity_payment_guards.require_approve_for_refund_diff`，與 POS 路徑對齊。
- 測試：新增「無簽核權限走全額沖帳旁路 → 被擋（需簽核）」回歸測試。

### A8 — availability 短 TTL 快取（P1）
- 問題：`public.py:266-317` 每次全表掃 + 即時 COUNT 聚合；`_public_etag_response` 的 ETag 對「算完結果」做 MD5（`:306`），命中 304 時查詢已跑完 → ETag 沒省到 DB。報名開放期高頻輪詢為單 worker DB 壓力主來源。
- 修正：availability 計算結果加短 TTL（5–10s）記憶體快取（沿用 `utils/cache_layer.get_cache()` / `_invalidate_activity_dashboard_caches` 既有基建），register/update/promote/容量異動時 invalidate。
- 測試：新增「TTL 內第二次請求不打 DB 聚合」測試 + invalidate 後重算測試。

### A9 — P2 小修
- **lock 降級收斂**：`pos.py:529-540`、`_shared.py:50-59` 的 `_lock_regs`/`_lock_registration` 目前對 `CompileError/OperationalError/NotImplementedError` 都靜默降級無鎖。改為**只對 SQLite 相關的 `CompileError/NotImplementedError` 降級，`OperationalError`（真 DB 錯誤/lock timeout）上拋**，避免極端情況失去併發保護。測試：mock `OperationalError` 斷言上拋而非無鎖續寫。
- **silent-reject log TypeError**：併入 A2 一併解。
- **availability 預設 capacity 硬編碼 30**（`public.py:300`）：course.capacity 為 NULL 時改顯示「不限/—」而非誤導性 30。測試：NULL capacity 顯示斷言。
- **honeypot 定位**：`schemas/activity_public.py:83` 註記為「輔助、非主要 anti-automation」，真正節流靠限流器（不改邏輯，只補 docstring/註解 + 文件）。
- **confirm/decline 三欄身分**（`public.py:1095`）：文件化為已知限制 + 列 follow-up（通知連結帶 token 當第二因素）。本次不動破壞性邏輯。

### A10 — XFF / rate-limit 可繞（P1，需 runtime 驗證 + ops）
- 問題：`config/network.py:15` `trusted_proxy_ips="*"`（解析失敗 fallback 只信 RFC1918）+ `main.py:1056` `forwarded_allow_ips="*"`。能否偽造取決於 Zeabur edge 是否 append 真實 client IP。
- 修正（程式端）：`request_ip._parse_trusted_proxies` 對字面 `"*"` 給明確 warning log（目前靜默 fallback 易誤判已設）；docstring 補「prod 必設 `TRUSTED_PROXY_IPS` 為 edge CIDR」。
- 交付（ops/文件）：`docs/sop/zeabur-deployment-runbook.md` 補一節：上線前用 `curl -H 'X-Forwarded-For: 1.2.3.4'` 從兩個真實來源 IP 各打一次，觀察 429 bucket 跟「偽造值」還是「真實 peer」；若跟偽造 → 設 `TRUSTED_PROXY_IPS`。此項 code 改動小，主體是 user 那邊跑驗證 + 設環境變數。

---

## 4. Workstream B — 全 app `async def`→`def` 遷移

### 問題
全 app 248 個 `async def` handler，內部用同步 `psycopg2` SQLAlchemy（`models/base.py` `create_engine`，無 `run_in_threadpool`）。單 uvicorn worker 下，`async def` 裡的同步 DB 呼叫不讓出 event loop → 任一慢查詢/鎖等待（最長 `statement_timeout=30s`）head-of-line blocking 住所有請求。報名開放瞬間最致命。

### 判定規則（per handler）
- handler 含**非 `asyncio.sleep` 的真 await**（`ws.*`、broadcast、`read_upload_with_size_check`、`file.read`、`request.body/json`、`run_in_executor`、`to_thread`）→ **保留 `async def`**（這些 handler 仍可能在 await 後跑同步 DB，屬更細緻的後續優化，本次不動，列 follow-up）。
- handler **零 await** → 轉 `def`。
- handler **唯一 await 是 `asyncio.sleep`** → 轉 `def` + `time.sleep`（public.py 353/416/901、settings.py）。

### 必須保留 async 的檔（含真 await，逐一保留對應 handler）
WebSocket / broadcast：`contact_book_ws.py`、`dismissal_ws.py`、`inbox_ws.py`、`dismissal_calls.py`、`events.py`、`announcements.py`、`portal/dismissal_calls.py`、`parent_portal/messages.py`、`portal/parent_messages.py`、`student_communications.py`（broadcast 處）。
上傳 / async I/O：`attachments.py`、`attendance/upload.py`、`parent_portal/events.py`、`parent_portal/medications.py`、`portal/leaves.py`、`students.py`、`vendor_payments.py`、`portfolio/reports.py`、`line_webhook.py`、`internal/uptime_webhook.py`。
> 注意：上述檔內**非** upload/ws 的 handler（零 await 者）仍要轉 `def`；保留只針對含真 await 的個別 handler。

### 方法
1. 寫一個一次性掃描腳本（AST，非正則）列出每個 route handler 的 await 清單，自動分類「轉 def / 保留 async」，產出清單供人工覆核（避免漏判 decorator 套疊或巢狀 async）。
2. 依清單機械轉換；`asyncio.sleep`→`time.sleep` 同步替換。
3. 移除轉換後不再需要的 `import asyncio`（若該檔還有保留 async 用到則留）。

### Gate
- 完整 `pytest` 套件**零新增 fail**（相對 main baseline）。
- 抽查保留清單：ws/upload handler 仍 `async def` 且功能測試綠。
- 抽查若干轉 `def` 的 handler 仍回正確 response（既有路由測試覆蓋）。

### 風險與緩解
- 動到大量未審查模組（健康/學生/薪資/portal/ws）→ 用「含真 await 才保留」硬規則 + 全測試套件守住；獨立 commit/PR 讓 review 與回滾邊界清楚。
- AST 腳本誤判 → 產出清單人工覆核後才執行。
- 既有測試對 handler 是否 async 有耦合（極少）→ 出現即個別修正。

---

## 5. 順序與交付

1. Workstream A（A2–A10）各自 TDD、分開 commit。
2. Workstream B 獨立 commit/PR（含 public.py async 轉換 + A1）。
3. 全程在 worktree（從 `origin/main`）進行；A/B 可同分支不同 commit 或分兩分支，依 plan 決定。
4. 完成後：完整測試套件、A 各項回歸測試綠、B gate 通過。

## 6. Out of scope / Follow-ups
- async 保留清單中「await 後的同步 DB」更細緻 threadpool 優化（B 之後另議）。
- confirm/decline 通知連結帶 token 第二因素（A9 文件化，實作另開）。
- A10 的 Zeabur `TRUSTED_PROXY_IPS` 設定與 runtime 驗證（user ops）。
- 多 worker 部署時的鎖序統一（audit P2，單 worker 不觸發）。

## 7. 成功標準
- audit 清單 P0/P1 全數有對應修正 + 回歸測試；P2 處理或明確文件化。
- 完整 pytest 套件相對 main 零新增 fail；前端 A3 vitest + typecheck 綠。
- 上線阻塞風險（單 worker event-loop 阻塞、PII 外洩、退費旁路、超賣）解除。
