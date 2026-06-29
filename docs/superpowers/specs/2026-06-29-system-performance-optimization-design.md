# 系統效能優化設計（2026-06-29）

## 背景

跨前後端做了一輪「全面量測健檢」（5 個唯讀量測 agent：後端 query/DB + postgres MCP 實查索引、後端運算熱路徑、後端 async 阻塞 I/O、前端 build 實測 bundle、前端 runtime/render）。

**健檢同時排除了大量假目標**（重要，避免白工）：
- DB 索引層健康：FK 覆蓋完整、`EXPLAIN` 確認熱查詢全走 index、最大表 `student_attendances` 31k 列有複合索引。
- 薪資 bulk 引擎、年終 build 主迴圈已充分批次化（預載 dict + 單次 commit + config snapshot cache）。
- auth/insurance 快取、WS 指數退避、87 條 router 全 lazy、charts/leaflet 全 async、輪詢計時器都有 cleanup。

因此這不是重構，而是**幾個有憑據的針對性熱點**。

## 範圍

實作 **群組 A（後端即時阻塞/熱點）+ B（列表分頁硬化）+ D（前端虛擬化 + 後端 compute 中項）**。

**明確不做（YAGNI / 另案）**：
- 群組 C：前端 bundle manualChunks 首屏瘦身（最大 user-facing 改善，但純 build config、需另案仔細驗證；本次 user 選擇略過）。
- DB 索引層調整（量測證實健康）。
- 薪資 bulk 引擎 / 年終 build 主迴圈（已批次化）。

## 原則

- **每項都走 TDD**：先補能 pin 住現有行為（或重現缺陷）的回歸測試 → RED → 修 → GREEN。多數是「行為保持的最佳化」，回歸測試要同時斷言①結果不變②查詢/呼叫次數下降（用 spy / `assert_called_once` 或查詢計數）。純 offload 項（A1/A4）以「既有測試保持綠」為驗證。
- **分軌道、分 commit**：依檔案不重疊切軌，每軌一筆 commit，訊息描述單一優化。
- **行號以實作當下 `Read` 為準**：本文件行號來自量測 agent，部分已知漂移；錨點 A1/A2 已人工核實更正，其餘在 TDD RED 階段重新定位。

---

## 群組 A：後端即時阻塞 & 運算熱點

### A1 ⭐ WS 週期重驗 token offload（HIGH，成本 S）— 已核實
- **位置**：`utils/ws_hub.py:155`，`_verify_loop` 內 `ok = verify()`。
- **機制**：`verify` 是同步 lambda（內部開 sync session 查 `User` + impersonator），每 `WS_REVERIFY_INTERVAL=60s`／**每條連線**在 event loop 上跑同步 psycopg2 query。單 worker + pool 20 下序列化阻塞**所有**請求/WS/心跳。HTTP 認證路徑（`utils/auth.py:484-497`）已用 threadpool offload，WS 路徑漏了。
- **修法**：`ok = await asyncio.to_thread(verify)`（1 行）。
- **TDD/驗證**：既有 WS 測試保持綠；新增測試斷言 `_verify_loop` 不在 event loop 上同步阻塞（可用一個 verify 內 `time.sleep` 的 fake，斷言 ping/recv loop 不被卡）。

### A2 ⭐ 年終考核 payout 同步重算 O(員工×全表) → dict 算一次（M-H，成本 S）— 已核實
- **位置**：`services/year_end/appraisal_sync.py`，`generate_payouts:522-523` 與 `void_payouts:572-573` 皆 `for emp_id in affected_emp_ids: _recompute_draft_settlement_total(db, cycle.id, emp_id)`；後者 `:293` 內呼 `compute_special_bonus_total_by_emp(db, cycle_id)` 掃**整個 cycle 全員** dict 卻只 `.get(emp_id)`。
- **機制**：O(受影響員工數 × 全 cycle SpecialBonusItem 全表掃描)。
- **修法**：迴圈外呼一次 `compute_special_bonus_total_by_emp(db, cycle.id)` 得 dict，`_recompute_draft_settlement_total` 改接受預算好的 `total_by_emp: dict` 或 `total_sum: Decimal` 參數；`generate_payouts` 與 `void_payouts` 共用同一份 dict。一併檢查 `api/year_end/__init__.py:669` 是否同 pattern（若在迴圈內亦修）。
- **TDD/驗證**：① 斷言 `compute_special_bonus_total_by_emp` 對多員工 cycle 只被呼叫一次（spy `assert_called_once`）；② 斷言 settlement.total_amount / special_bonus_total 結果與修前完全一致（excel-wins 去重口徑不變，CLAUDE.md §10 既有設計）。

### A3 請假套用考勤逐日 SELECT → 單一範圍查詢（Med，成本 S）
- **位置**：`services/student_leave_service.py:90-98`，`apply_attendance_for_leave` 內 `for d in dates:` 對最大表 `student_attendances` 逐日 `.filter(student_id, date==d).first()`。
- **機制**：多週家長假 → 15-25 次 SELECT。同檔 `revert_attendance_for_leave:117-127` 已用單一範圍查詢，照抄即可。
- **修法**：改一次 `WHERE student_id = ? AND date BETWEEN start AND end` 撈回 dict by date，迴圈內查 dict。
- **TDD/驗證**：① 多日假套用考勤結果不變（含部分假 per-day 攤分既有語意）；② 斷言對 attendance 表只發一次 SELECT。

### A4 async def 內同步 Excel 解析 / WS handshake offload（Med，成本 S×N）
- **WS handshake**：`api/dismissal_ws.py:143/220`、`api/contact_book_ws.py:89/156` 的 `verify_ws_token` 與 `_get_teacher_classroom_ids` 在 accept 前於 loop 上跑同步 DB → 包 `asyncio.to_thread`。
- **Excel import**：`api/events.py`（`import_holidays`）、`api/appraisal/__init__.py`（`import_excel`）、`api/year_end/__init__.py`（`import_excel`）、`api/art_teacher_payroll.py`（`batch_import`）在 `async def` 內同步跑 `pd.read_excel`/`xlrd`/`openpyxl` 解析 + DB import → 包 `run_in_threadpool` / `asyncio.to_thread`（與本 repo 既有 `import_overtimes`/`import_leaves`/`import_shifts` 的 `run_in_executor` 範式一致）。
- **TDD/驗證**：既有 import 端點測試保持綠；offload 不改解析/匯入結果。**WS handshake 與 Excel offload 屬第二優先**（admin-rare / bursty），A1 是 A 群唯一 recurring 熱路徑、最高優先。

---

## 群組 B：後端列表分頁/界限硬化

### B1 ⭐ 家長相簿假分頁 → 真 SQL 分頁（HIGH，成本 M）
- **位置**：`api/parent_portal/photos.py:144-153,162`，`GET /api/parent/photos`。
- **機制**：Query 收 `skip/limit`，但對每日成長最快的 `attachments` 表做 4 個 owner type 各無界 `.all()`、無日期窗、無 SQL LIMIT，載完整段歷史後 Python `all[skip:skip+limit]`。
- **修法**：4 個 owner 來源改 SQL `UNION ALL` + 統一 `ORDER BY created_at DESC` + SQL `LIMIT/OFFSET`（或 keyset/cursor）；total 用 `COUNT` 不 hydrate。
- **TDD/驗證**：① 第 2 頁回正確 slice、total 正確；② 斷言不再對 attachments 做無 LIMIT 的 `.all()`（查詢計數 / SQL 含 LIMIT）。

### B2 行政「列出全部」端點加界限（Med，成本 S×6）
照各 router 既有範式（required 日期窗 / `page+page_size` / 合理 `.limit`）硬化：
- `api/leaves.py:674`（leave_records，目前只 `.limit(5000)`）
- `api/overtimes.py:723`（overtime_records，同上）
- `api/punch_corrections.py:131-133`（月窗僅雙參數才生效，否則 `.limit(5000)`）
- `api/disciplinary.py:114-116`（**完全無 cap**）
- `api/events.py:129`（`GET /api/events`，省略 year 即無 cap；已有 `/events/calendar-feed` 窗版可參照）
- `api/parent_portal/medications.py:245-252`（無 limit + N3：每筆 order 再 `_load_logs`/`_load_photos`，一併收）
- 一併處理 self-scoped 對等端點 `api/portal/leaves.py:939`、`api/portal/punch_corrections.py:128`、admin `api/portfolio/measurements.py:323`（家長版已 `months≤36`+500 cap，admin 漏）。
- **TDD/驗證**：每端點斷言超量資料下回傳列數受 cap / 分頁參數生效；既有正常查詢結果不變。

### B3 student change-logs summary 改 SQL GROUP BY（Med，成本 S）
- **位置**：`api/student_change_logs.py:212`，`GET /students/change-logs/summary`。
- **機制**：整學期 hydrate 成 ORM 再 Python 計數。
- **修法**：改 SQL `GROUP BY event_type` 直接回計數。
- **TDD/驗證**：counts 與修前一致；斷言不再整表 hydrate。

---

## 群組 D：前端虛擬化 + 後端 compute 中項

### D1 ImportPreviewDialog 打卡預覽分頁/上限（Med，成本 M）— 前端
- **位置**：`ivy-frontend/src/components/attendance/ImportPreviewDialog.vue:257-262`。
- **機制**：`el-table :data="previewResult.rows"` 無 `max-height`、無分頁、無虛擬化，每列含自訂 cell template；匯入整月全員打卡（數百~上千列）一次掛 DOM 卡頓。
- **修法**：加 `max-height` + `el-pagination`（client 端切頁）或遷 `el-table-v2` 虛擬化。優先 `max-height + 分頁`（成本較低、風險小）。
- **TDD/驗證**：vitest 斷言大量 rows 下 DOM 實際 render 列數受限 / 分頁器存在；預覽資料正確性不變。

### D2 後端 compute 中項 N+1 → SQL 聚合（Med，成本 M）
- **auto_derive 每班 COUNT → `GROUP BY classroom_id`**：`services/year_end/.../after_class_award.py`、`semester_dividend.py`、`returning_rate.py`（derive 階段每班/每 target 各 1-2 條 COUNT）。
- **festival 預覽 N+1**：`api/salary/festival.py:213-235`（員工×passed_months 逐格班級反查；bulk 薪資已用 `classroom_for_emp` 預載，此預覽未跟進）。
- **enrollment_rates Python 計數**：`enrollment_rates.py:201-203`（被 `settlement_builder.py:511` 每班×6 月呼叫，Python 端掃全校在籍計數）→ SQL `GROUP BY classroom_id COUNT`。
- **TDD/驗證**：每項斷言聚合結果與修前完全一致 + 查詢次數下降。**D2 為次優先**（量級中等，可在 A/B 之後）。

---

## File-Disjoint 軌道規劃（每軌一筆 commit）

切軌以「檔案不重疊」為準，讓各軌可獨立 TDD、獨立 commit，避免互踩。已處理重疊檔（`api/events.py`、`api/year_end/__init__.py` 各只歸一軌）。

| 軌 | 群 | 動到的檔 | commit 主題 |
|---|---|---|---|
| T1 | A1+A4-WS | `utils/ws_hub.py`、`api/dismissal_ws.py`、`api/contact_book_ws.py` | WS reverify/handshake offload 出 event loop |
| T2 | A2 | `services/year_end/appraisal_sync.py`（+ 視情況 `api/year_end/__init__.py` payout 段） | 年終 payout 重算 dict 算一次，消除 O(員工×全表) |
| T3 | A3 | `services/student_leave_service.py` | 請假套用考勤改單一範圍查詢 |
| T4 | A4-imports | `api/events.py`、`api/appraisal/__init__.py`、`api/art_teacher_payroll.py` | Excel import 解析 offload 出 event loop |
| T5 | B1 | `api/parent_portal/photos.py` | 家長相簿改真 SQL 分頁 |
| T6 | B2 | `api/leaves.py`、`api/overtimes.py`、`api/punch_corrections.py`、`api/disciplinary.py`、`api/parent_portal/medications.py`、`api/portal/*.py`、`api/portfolio/measurements.py` | 列表端點加分頁/界限 |
| T7 | B3 | `api/student_change_logs.py` | change-logs summary 改 SQL GROUP BY |
| T8 | D2 | `services/year_end/.../after_class_award.py`、`semester_dividend.py`、`returning_rate.py`、`api/salary/festival.py`、`enrollment_rates.py` | compute N+1 改 SQL 聚合 |
| T9 | D1 | `ivy-frontend/src/components/attendance/ImportPreviewDialog.vue`（前端 repo） | 打卡預覽分頁/虛擬化 |

> ⚠ `api/year_end/__init__.py` 同時被 T2（payout 段 :669）與 A4-imports（`import_excel` :765）碰到。為維持 file-disjoint，`import_excel` 的 offload 併入 T2（或 T2 完成 push 後再做），不放 T4。`api/events.py` 的 `import_holidays` offload 與 events list cap 都歸 **T6**（與 T4 分開），T4 只含 appraisal/art_teacher。

## 建議實作順序（CP 值）

1. **T1（A1）+ T2（A2）+ T5（B1）+ T3（A3）**：最高槓桿、低風險、可 TDD。
2. **T6（B2）+ T7（B3）**：照範式硬化，防永久成長。
3. **T4（A4-imports）+ T8（D2）+ T9（D1）**：次優先，量級中等。

## 收尾

依 workspace「Definition of Done」：本次比照既有工作法 **commit 進 local main、不 push**（push 後端會觸發 Zeabur 正式部署 + 跑 migration，由 user 決定時機）。每軌 commit 後跑該軌相關 pytest（後端 `-o addopts=""` 關 coverage 加速）/ vitest（前端）。⚠ 後端工作樹現有平行 session WIP（`observability.md`、salary-config spec），本次只動上列軌道檔、不碰其 WIP。
