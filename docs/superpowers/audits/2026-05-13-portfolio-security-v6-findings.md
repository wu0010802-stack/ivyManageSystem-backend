# Portfolio 模組安全體檢（v6）— 2026-05-13

## Scope

本輪 audit 對象：
- `api/portfolio/*`（admin 端 8 子模組：measurements / milestones / observations / reports / student_attachments / timeline / auto_milestone / __init__）
- `api/parent_portal/{milestones,photos,growth_reports,measurements,timeline}.py`（家長端對應端點）
- `api/parent_portal/parent_downloads.py`（家長端附件下載）
- `api/attachments.py`（共用上傳 / admin 下載 / 軟刪）
- `utils/portfolio_access.py`、`utils/portfolio_storage.py`、`utils/file_upload.py`
- `models/portfolio.py`

**不在本輪範圍**（延 v7）：JWT blocklist GC、CSP `style-src 'unsafe-inline'`、rate_limit PG fail-open、WebSocket endpoints、其他模組 BackgroundTasks idempotency、LINE webhook signature / reply 路徑、家長 LINE refresh token rotation。

威脅模型沿用 [`2026-04-28-idor-findings.md`](2026-04-28-idor-findings.md)：a) 員工互查 b) 跨班教師 c) 家長跨家庭 d) 未認證公開 e) 高權限欄位級。

### 已先行修補（本 audit 落地前）
- `2026-05-13 09:49` `55523c62` `fix(portfolio): LINE 推送 5 分鐘冪等防重複` — 加 5 分鐘冪等檢查（但有 TOCTOU，見 F-V6-01）
- `2026-05-13 10:14` `21eb4ffe` `fix(parent-portal): photos endpoint 過濾草稿與軟刪附件 [P0]` — 修真實 IDOR：家長端照片牆會看到老師草稿聯絡簿照（`published_at IS NULL`）與軟刪聯絡簿/觀察的歷史照；改建獨立 `_parent_owner_ids` 加 deleted_at + published_at + status='ready' 過濾

---

## Finding 排序

按可利用性（exploitability）排序，**不**按嚴重度標籤。
P0/P1 為實際可觸發；P2 為利用門檻高或影響輕；INFO 為設計提示，非可利用 bug。

---

## P0 — 可立即觸發、影響家長 / 稽核

### F-V6-01：`send_growth_report_to_line` 5 分鐘冪等 TOCTOU
- **檔案**：`api/portfolio/reports.py:553-630`（端點 `POST /api/students/{student_id}/growth-reports/{report_id}/send-line`）
- **威脅**：(a) 員工互查不適用；屬「並發/重試濫用」型
- **問題**：上一個 commit（`55523c62`）加的 5 分鐘冪等檢查為 plain `.first()` 撈 row，無 row lock。
  ```
  if r.line_sent_at and (datetime.utcnow() - r.line_sent_at < timedelta(minutes=5)):
      raise 409
  # 後續 push + r.line_sent_at = datetime.utcnow()
  ```
- **觸發**：admin 前端連點 / network retry / 雙開分頁 → 兩個 request 同時進來 → 兩個都看到 `line_sent_at` 為 None 或已逾 5 分鐘 → 都通過守衛 → 兩個都 push → 家長 LINE 收兩則重複訊息（浪費月配額 + 體驗差）
- **修法**：`StudentGrowthReport` 撈取改用 `.with_for_update()`；模板同 round 3 dismissal_calls 修補（commit `748e31b8`）
- **參考**：[[project-bug-sweep-round3-2026-05-12]] dismissal acknowledge/complete/cancel 列鎖

#### F-V6-01b（延伸）：sent_count=0 時 `line_sent_at` 仍被寫
- **檔案**：`api/portfolio/reports.py:611-625`
- **問題**：
  ```
  for uid in line_user_ids:
      ok = _line_service.push_to_user(uid, base_text)
      if ok: sent_count += 1
  r.line_sent_at = datetime.utcnow()  # 不管 sent_count 都寫
  ```
  - 全部 push 都失敗（LINE token 過期 / 網路抖動）仍把 `line_sent_at` 標起來
  - admin 看到 200 + sent_count=0 容易忽略；5 分鐘內無法重試
- **修法**：`sent_count == 0` → 不寫 `line_sent_at` 並回 502（user 已在 `test_send_line_all_failed_releases_idempotency_lock` 寫測試驗證；WIP 於 `tests/test_growth_report_api.py`）

---

### F-V6-02：`create_growth_report` 無 dedup → 繞過 F-V6-01
- **檔案**：`api/portfolio/reports.py:360-412`（端點 `POST /api/students/{student_id}/growth-reports`）+ `models/portfolio.py:487-494`
- **威脅**：「並發/重試濫用」型；可獨立利用、也用來繞 F-V6-01
- **問題**：handler 直接 `INSERT StudentGrowthReport`，無 (student_id, period_label, period_start, period_end) dedup 檢查；migration `ix_growth_reports_student_period` 是 **non-unique** index
- **觸發**：admin 連點 POST → 兩筆 `StudentGrowthReport` 同 period 各自獨立；兩個 BG task 跑 → 兩份 PDF 寫盤；兩個 send-line 端點分別走（兩個 row 各自的 line_sent_at 為 None）→ **F-V6-01 row lock 修法無效**（lock 在不同 row 上）
- **修法**：加 partial unique index `(student_id, period_label, period_start, period_end) WHERE status != 'failed'` 或 handler 端 `with_for_update` + dedup query + `409` 帶既有 report_id
- **注意**：本 finding 與 F-V6-01 必須**一起修**，否則 F-V6-01 修法可被繞過

---

## P1 — 防禦深度 / 合規

### F-V6-03：`/api/students/{id}/attachments` 與 `/timeline` 缺 explicit audit
- **檔案**：
  - `api/portfolio/student_attachments.py:90-143`
  - `api/portfolio/timeline.py:177-242`
- **威脅**：(e) 高權限欄位級；事後追蹤
- **問題**：兩個聚合端點吐出學生跨模組 PII（觀察、量測、用藥單、聯絡簿、出勤、活動、評估、incident、communication_log），全程無 `request.state.audit_*` / `write_explicit_audit`；AuditMiddleware 對 GET 預設不記
- **觸發**：teacher / admin 一次 GET 拿到單一學生數十條跨模組 PII，事後無稽核可查「誰看了什麼」
- **修法**：補 `request.state.audit_entity_id = str(student_id)` + `audit_summary`；對齊 round 1 portal GET audit 修法（commit `7a25d767` 用於 `/api/portal/profile`、`/api/portal/students/{id}/detail`）
- **參考**：[[project-audit-coverage-gap-2026-05-12]] 敏感 GET 14 個白名單

---

### F-V6-04：`parent_react` / `parent_acknowledge` 無 row lock
- **檔案**：`api/parent_portal/milestones.py:82-126, 129-166`
- **威脅**：(c) 家長跨家庭不適用；屬「同家庭多監護人並發」
- **問題**：兩個 row 撈取無 lock，後贏者覆蓋。`parent_reaction` 業務上是「最後一次按算」可接受；但 `parent_acknowledged_by` 在「第一次 ack 即綁定」語意下會被覆蓋（爸爸/媽媽同時 react → 誰先誰後 attribution 錯誤）
- **觸發**：同學生兩位 guardian 並發 react → `parent_acknowledged_by` 寫的是後贏者；稽核軌跡記錯人
- **修法**：撈 milestone 改 `.with_for_update()`，重新檢查 `parent_acknowledged_at is None` 才寫 acknowledged_by

---

### F-V6-05：`parent_download_report` parent_first_viewed_at / view_count 並發 lost update
- **檔案**：`api/parent_portal/growth_reports.py:57-94`
- **威脅**：「並發/重試濫用」型
- **問題**：
  ```
  if r.parent_first_viewed_at is None:
      r.parent_first_viewed_at = datetime.utcnow()  # TOCTOU
  r.parent_view_count = (r.parent_view_count or 0) + 1  # lost update
  ```
  - `parent_first_viewed_at` 兩個並發都看到 None，都寫，後贏者覆蓋（影響輕，但破壞「首次檢視時間」語意）
  - `parent_view_count` 並發 N 個 +1 可能少算
- **觸發**：家長前端瀏覽器 retry / iOS WKWebView preview 加正式下載 → 兩個 GET 同秒到 → view_count race
- **修法**：`UPDATE student_growth_reports SET parent_view_count = parent_view_count + 1, parent_first_viewed_at = COALESCE(parent_first_viewed_at, NOW()) WHERE id = :id`（單 SQL atomic；不需 row lock）

---

## P2 — 影響輕 / 利用門檻高

### F-V6-06：parent 端 `_row_to_dict` 暴露 admin 內部欄位
- **檔案**：`api/parent_portal/growth_reports.py:16,48` 重用 `api/portfolio/reports.py:_row_to_dict`
- **威脅**：(e) 高權限欄位級
- **問題**：`_row_to_dict` 含 `error_message`、`file_path`、`generated_by`；家長端通常只看 status=READY 的 row，`error_message` 應為 None，但若 admin 對失敗 report 補 patch 後改 status 回 READY，`error_message` 殘留會洩漏內部例外
- **修法**：parent 端獨立 dict converter，只含 `id / period_label / period_start / period_end / generated_at / line_sent_at / parent_view_count / teacher_narrative`

---

### F-V6-07：`parent_react` 無 rate limit
- **檔案**：`api/parent_portal/milestones.py:82`
- **威脅**：濫用型
- **問題**：parent A 對 own student 的 milestone 連點 reaction，每次都 UPDATE `parent_reaction` + `updated_at`；無防 spam；audit log 也會被灌
- **修法**：套 `rate_limit_decorator` 或 nginx 層限制；建議 10/60s/IP

---

### F-V6-08：`upload_attachment` 順序使 PIL 解碼早於 owner ACL
- **檔案**：`api/attachments.py:134-209`
- **威脅**：teacher（有 PORTFOLIO_WRITE）對非自己班 owner_id 上傳 → CPU 浪費
- **問題**：
  ```
  await read_upload_with_size_check(...)  # multipart 已被 starlette buffer，省不下 IO
  validate_file_signature(content, ext)   # magic bytes CPU
  ...storage.put_attachment(...)          # PIL 解碼 + variants（CPU 重）
  # 最後才 _resolve_owner_student_id + assert_student_access
  ```
- **觸發**：teacher A 上傳 50MB 圖片到 teacher B 班的 observation_id；server 跑完 PIL 變體生成才回 403
- **影響**：multipart body 已被 starlette buffer（FastAPI 預設）所以 IO 已吃下；本 finding **只省 magic_bytes + PIL CPU**，不省網路 IO
- **修法**：handler 第一行先開 session、`_resolve_owner_student_id`、`assert_student_access`，再 `read_upload_with_size_check` 與後續處理

---

## INFO — 設計提示，非可利用 bug

### F-V6-INFO：`_resolve_owner_student_id` 覆蓋面與 attachment URL 一致性
- **檔案**：
  - `api/attachments.py:93-112`（admin 端反查；目前只支援 `observation`）
  - `api/parent_portal/parent_downloads.py:42-91`（家長端反查；支援 medication_order / event_ack / message / student_leave）
  - `api/parent_portal/photos.py`（2026-05-13 `21eb4ffe` 已建獨立 `_parent_owner_ids`；但 `_attachment_to_dict` 仍 import 自 admin，URL 走 `/api/uploads/portfolio/{key}` 需 PORTFOLIO_READ，家長 mask=0 → 點圖 403）
  - `api/portal/contact_book.py:139, 509`（staff portal 用家長 path URL）
- **狀態**：屬功能 / 設計不一致；不是 security exploit（外部攻擊者只是看不到資料，沒繞守衛）
- **建議**：另開 functional bug ticket 統一 attachment URL 規則；每側獨立 dict converter 自己定 base path，避免 cross-import 把 admin path 帶到家長端；同時補 `_resolve_owner_student_id` 與 `_resolve_student_id_for_parent` 對 `observation / contact_book / report` 三種 owner_type 的反查
- **不在本 v6 audit 修補範圍**

---

## 修補建議順序

| 順序 | Finding | 理由 |
|------|---------|------|
| 1 | F-V6-02 + F-V6-01 一起修 | 不一起修等於沒修；用一個 commit 落地（partial unique index + with_for_update） |
| 2 | F-V6-03 | 對齊 audit-coverage-a 既有樣板；改動小、ROI 高 |
| 3 | F-V6-04 | 同 round 3 dismissal 模板，5 分鐘可修 |
| 4 | F-V6-05 | 單 SQL atomic update，10 分鐘可修 |
| 5 | F-V6-06 + F-V6-08 | 防禦深度，可一起 commit |
| 6 | F-V6-07 | 視業務量決定要不要做 |
| INFO | F-V6-INFO | 另案處理，不在 v6 |

---

## 採用 SOP

- 修補一條 → 一個 commit，commit message 帶 finding 代號（如 `fix(security): F-V6-01 send-line TOCTOU row lock`）
- 每條都補 pytest 回歸（priority: F-V6-01/02/03/04/05 必補）
- 修完後在 SECURITY_AUDIT.md 新章節「2026-05-13 Portfolio v6」逐條註記 ✅ 已修
- 不要把 INFO 修法塞進 v6 commit；屬另案
