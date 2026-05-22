# 招生漏斗 Phase A（後端） — Design

- 日期：2026-05-22
- 範疇：後端（`ivy-backend`）
- 後續：Phase B（前端 Kanban）另開 spec，在本 Phase merge 後啟動

## 1. 背景與痛點

現況：

- `services/recruitment_conversion.py:convert_recruitment_to_student()` 函式存在，但無工作流驅動 — 會計需手動按鈕。
- 無「招生漏斗狀態」可視化（已訪視 / 已預繳 / 已報到 / 已開學）。
- `recruitment_visits` 與 `students.lifecycle_status` 分別存兩段狀態，無一致性視圖。
- 訪視/預繳階段沒有事件流（`student_change_logs` 從 Student 建立後才開始）。

目標：

- 4 階段有狀態 funnel + 推進入口（Kanban 拖拉觸發）。
- 自動寫 timeline（recruitment + student 雙層 union）。
- 半自動推進「已報到 → 已開學」（依學期開學日批量觸發）。

## 2. 4 階段狀態機

**核心原則**：stage 完全 derived（純函數於 `(has_deposit, enrolled, students.lifecycle_status, recruitment_visit_id JOIN)`）。不在 `recruitment_visits` 加 stage 欄位，避免 dual source of truth。

| stage | 推導規則 | 前進動作 |
|---|---|---|
| `visited` | visit 存在 ∧ `has_deposit=false` ∧ 無關聯 Student | flip `has_deposit=true` → `deposited` |
| `deposited` | `has_deposit=true` ∧ 無關聯 Student | 呼叫 `convert_recruitment_to_student()`（service 自產學號，需 `classroom_id`）→ `enrolled` |
| `enrolled` | 有關聯 Student ∧ `lifecycle_status='enrolled'` | flip `lifecycle_status='active'` → `active` |
| `active` | 有關聯 Student ∧ `lifecycle_status='active'` | — |

**反向（destructive）**：

| 反向 | 動作 |
|---|---|
| `deposited → visited` | flip `has_deposit=false`；event log `deposit_removed` |
| `enrolled → deposited` | 刪 Student（含 Guardian、student_change_logs）；flip `visits.enrolled=false`；event log `revert_converted`；前置：`assert_student_revertable()` 通過 |
| `active → enrolled` | flip `lifecycle_status='enrolled'`（不刪 Student）；event log `revert_activated` |
| `active → deposited` 或 `active → visited` | 拆成多段（active→enrolled→...），每段獨立檢查、各自寫 event log |

學號生成：`{民國學年}-{class_code}-{NN}`（流水兩位數零填，同年同班遞增）。

**注意**：`recruitment_visits.enrolled` 欄位保留向下相容 — `derive_stage()` 不讀它，但 `transition_visit()` 在 `deposited↔enrolled` 段仍會同步維護其值（避免既有讀此欄的 query 看到陳舊資料）。日後若全 codebase 改用 stage derivation，再考慮 drop 此欄。

## 3. 資料表變更

### 3.1 `academic_terms`（新表）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | int PK | |
| `school_year` | int not null | 民國學年 |
| `semester` | int not null | 1=上、2=下 |
| `start_date` | date not null | 開學日 |
| `end_date` | date not null | |
| `created_at`/`updated_at` | datetime | |

Constraints：`UNIQUE(school_year, semester)`、`CHECK(end_date > start_date)`、`CHECK(semester IN (1,2))`。

### 3.2 `recruitment_event_log`（新表）

| 欄位 | 型別 | 說明 |
|---|---|---|
| `id` | int PK | |
| `recruitment_visit_id` | int FK→`recruitment_visits.id` not null | |
| `event_type` | varchar(40) not null | `created` / `deposit_added` / `deposit_removed` / `converted` / `revert_converted` / `activated` / `revert_activated` |
| `from_stage` | varchar(20) nullable | `created` 事件為 null |
| `to_stage` | varchar(20) not null | |
| `student_id` | int FK→`students.id` nullable, `ON DELETE SET NULL` | |
| `reason` | text nullable | destructive 必填 |
| `actor_user_id` | int FK→`users.id` nullable | scheduler 觸發為 null |
| `metadata` | jsonb nullable | 結構化附加 |
| `created_at` | datetime not null | |

Indexes：`(recruitment_visit_id, created_at)`、`(event_type)`、`(actor_user_id)`。

### 3.3 不改的東西

- `recruitment_visits` schema
- `students` / `student_change_logs`
- `convert_recruitment_to_student()` 介面（僅 `student_id_code` 從必填改 optional）

### 3.4 Migration

- 一支 alembic migration 同時建 `academic_terms` + `recruitment_event_log`
- 純 CREATE TABLE，downgrade DROP TABLE
- 不需 backfill — 既有 visits 自動推導 stage、event log 空表起步（老 visits 在 timeline 無歷史）

## 4. Service 層

### 4.1 `services/recruitment_funnel.py`（新）

純函式 + 1 個 orchestrator：

```python
def derive_stage(visit: RecruitmentVisit, student: Optional[Student]) -> Stage
def can_transition(from_stage: Stage, to_stage: Stage) -> bool
def is_destructive(from_stage: Stage, to_stage: Stage) -> bool
def next_student_id_code(session, school_year: int, class_code: str) -> str
def assert_student_revertable(session, student_id: int) -> None
def transition_visit(
    session, visit_id, to_stage, actor_user_id, *, classroom_id=None, reason=None
) -> TransitionResult
```

`transition_visit()` 是**唯一寫入入口**：

1. `SELECT ... FOR UPDATE` 鎖 visit row
2. 讀 visit + student → `derive_stage()` 推 from_stage
3. 規則檢查：
   - `from_stage == to_stage` → 409
   - destructive 缺 reason → 400
   - `deposited → enrolled` 缺 classroom_id → 400
   - destructive 反向：先 `assert_student_revertable()`
4. Dispatch sub-action
5. 寫 event log
6. flush（commit/rollback 由 router 控）

### 4.2 `services/recruitment_conversion.py`（既有，輕修）

- `student_id_code: str` → `Optional[str] = None`（None 時內部呼叫 `next_student_id_code()`）
- 函式內部新增寫 `recruitment_event_log`（`converted`）— 將既有 ChangeLog 寫入流程保持不變
- OpenAPI 端點 `/api/recruitment/{id}/convert`（記錄裡 line 320 那支）標 `deprecated=True`，建議改走 `/funnel/visits/{id}/transition`

### 4.3 `services/recruitment_lifecycle.py`（新）

```python
def advance_term_to_active(session, school_year, semester) -> dict
```

Scheduler 用 — 逐筆 dispatch `transition_visit(visit_id, to_stage='active', ...)`。

## 5. API endpoints

### 5.1 `api/recruitment/funnel.py`（新，prefix `/recruitment/funnel`）

| Method | Path | Permission | Body / Query | Response |
|---|---|---|---|---|
| `GET` | `/funnel/board` | `RECRUITMENT_READ` | `school_year?` `semester?`（預設當期） | `{ stages: {visited, deposited, enrolled, active}, summary: {visited_count, ...} }`，每張卡 `{visit_id, child_name, grade, phone, district, source, deposited_at?, student_id?, current_stage}` |
| `POST` | `/funnel/visits/{visit_id}/transition` | 動態（下表） | `{ to_stage, classroom_id?, reason? }` | `{ visit_id, from_stage, to_stage, student_id?, event_log_id, warnings?: string[] }` |
| `GET` | `/funnel/visits/{visit_id}/timeline` | `RECRUITMENT_READ` | — | union `recruitment_event_log` + `student_change_logs`（若有 student），按 `created_at` 排序 |

**Permission dispatch for transition**：

| from → to | require |
|---|---|
| visited ↔ deposited | `RECRUITMENT_WRITE` |
| deposited → enrolled | `RECRUITMENT_CONVERT` |
| enrolled → deposited/visited | `RECRUITMENT_CONVERT && STUDENT_WRITE` |
| enrolled ↔ active | `STUDENT_WRITE` |
| active → enrolled/deposited/visited | `STUDENT_WRITE`（destructive 需 reason） |

### 5.2 `api/academic_terms.py`（新，prefix `/academic-terms`）

| Method | Path | Permission |
|---|---|---|
| `GET /academic-terms` | list | `RECRUITMENT_READ` |
| `GET /academic-terms/current` | 當期推導（依今天落在哪段 start/end） | `RECRUITMENT_READ` |
| `POST /academic-terms` | create | `SETTINGS_WRITE` |
| `PUT /academic-terms/{id}` | update | `SETTINGS_WRITE` |
| `DELETE /academic-terms/{id}` | delete | `SETTINGS_WRITE` |

### 5.3 既有 `api/recruitment/records.py`

- 第 320 行 `convert_recruitment_to_student` 端點：OpenAPI `deprecated=True`、保留運作不刪
- 其餘端點不動

### 5.4 響應大小

- `/funnel/board` 預設不分頁（當期估 200-400 筆級）
- Phase B 若卡片數增長，再加 server-side 過濾 / virtual scroll

## 6. Scheduler

新檔 `services/recruitment_term_advance_scheduler.py`，鏡像 `services/graduation_scheduler.py` 結構。

### 6.1 觸發

每日跑一次（`CHECK_INTERVAL_SECONDS = 86400`）：
- `today = _today_taipei()`
- 查 `academic_terms where start_date = today` → 對每個 term 呼叫 `advance_term_to_active(year, semester)`

### 6.2 `advance_term_to_active(year, semester)`

1. 查所有 `students` where `lifecycle_status='enrolled'` ∧ `recruitment_visit_id IS NOT NULL` ∧ `enrollment_date IS NOT NULL` ∧ `enrollment_date ∈ [term.start_date - 90 天, term.start_date]`
   - `enrollment_date IS NULL` 的學生直接跳過（極少數異常資料，不在此 scheduler 處理範疇）
2. 逐筆取 `visit_id = student.recruitment_visit_id` → `transition_visit(visit_id, to_stage='active', actor_user_id=None, reason='scheduler:term_start')`
3. 失敗（如已 active）跳過 + warning log，不中斷整批
4. 回傳 `{advanced, skipped, failed}` 記入心跳

### 6.3 Idempotency

- `derive_stage()` 已 `active` → 直接 409 / 跳過
- 重啟服務當日重跑 → 同上跳過
- admin 在開學日當日把 active 退回 enrolled，scheduler 之後又跑回 active：語義上正確，timeline 留兩筆相反事件（acceptable）
- **錯過開學日（如服務當天 down）**：當天沒推進的學生不會自動補推，需 admin 手動拖。Phase A 不做 catch-up 邏輯（避免「過了一個月才發現 scheduler 壞掉、一次批量推一堆」的意外）— Kanban UI 上仍可一目瞭然看到「該開學但仍卡 enrolled」的卡片。

### 6.4 設定

- `settings.scheduler.recruitment_term_advance_enabled: bool = True`
- `settings.scheduler.recruitment_term_advance_check_interval: int = 86400`
- `settings.scheduler.recruitment_term_advance_window_days: int = 90`

`main.py` 在 startup 加 `init_recruitment_term_advance_scheduler()`，與既有 4 個 scheduler 同位置註冊。

### 6.5 不做（YAGNI）

- 開學日預覽 / 通知
- 手動觸發 endpoint（Kanban 拖拉 = 手動觸發；批次推進按鈕 Phase B 加，呼叫同一 service）

## 7. 錯誤處理與並發

### 7.1 Transition 原子契約

`transition_visit()` 內部：

1. `SELECT FOR UPDATE` on visit
2. 重新讀 `from_stage`（不 trust 入參）
3. 規則檢查（見 §4.1）
4. Dispatch sub-action
5. 寫 event log
6. flush（router commit/rollback）

### 7.2 Destructive 反向白名單

`assert_student_revertable(student_id)` 檢查無下游業務記錄：
- `student_attendances`
- `student_fees` / `student_fee_invoices`
- `parent_users`（LINE 綁定）
- `student_assessments`
- `student_temperature` / `medication_records`
- `student_incidents`（獎懲）

任一存在 → raise BadRequest `"該學生已有業務資料，請走退學流程"`。

`active → enrolled`：不刪 Student、不擋；若該學生開學後已有 `student_attendances` 記錄，在 response body 帶 `warnings: ["student_has_attendance_after_active"]`（純資訊，不影響 200 成功）。

### 7.3 跨段反向

`active → deposited/visited` 拆多步（each `transition_visit()`），每段獨立檢查 + event log。任一段失敗整體 rollback。

### 7.4 Race 場景

| 場景 | 防護 |
|---|---|
| 兩 admin 同 visit | `SELECT FOR UPDATE` |
| Scheduler 與 admin | 同上 |
| 學號並發 | `next_student_id_code()` 用 `pg_advisory_xact_lock(hash(year, code))`（首選；不持表鎖、開銷低）— `SELECT FOR UPDATE` 為備案，若 implementer 評估 advisory lock 不適用再切 |
| 開學日重啟重觸發 | §6.3 idempotent |

### 7.5 Error response

| HTTP | 場景 | code 範例 |
|---|---|---|
| 400 | reason 缺 / classroom_id 缺 / 下游有資料無法 revert | `DEPOSIT_TOGGLE_NOOP`、`CONVERT_NEED_CLASSROOM`、`REVERT_STUDENT_HAS_DATA` |
| 403 | 權限不足 | — |
| 404 | visit_id 不存在 | — |
| 409 | from == to | `STAGE_ALREADY` |
| 500 | 內部錯誤 | `raise_safe_500` |

## 8. 測試

### 8.1 純函式單測

`tests/test_recruitment_funnel_pure.py`：

- `derive_stage()` 4+ 邊界 case
- `can_transition()` 16 種 (from, to)
- `is_destructive()` 全列舉
- `next_student_id_code()` 同班遞增 / 跨班 / 跨年 / 空表

### 8.2 Transition 整合

`tests/test_recruitment_funnel_transitions.py`：

- 4 個前進 happy path
- 3 個 destructive 反向 happy path
- 缺欄位 400、same stage 409、destructive 無 reason 400
- revert 有下游資料 400

### 8.3 學號並發

`tests/test_recruitment_student_id_concurrency.py`：

- 50 thread 同 `(year, class_code)` 拿學號 → 流水 1..50 連續無重複
- 用 real Postgres test DB（SQLite 不支援 advisory lock）

### 8.4 Scheduler

`tests/test_recruitment_term_advance.py`：

- mock `_today_taipei()` 回開學日 → term 內 enrolled 學生升 active
- 在 window 外的 enrollment_date → 不動
- 已 active → 跳過、無新 event log
- enabled=False → noop

### 8.5 API endpoint

`tests/test_recruitment_funnel_api.py` / `tests/test_academic_terms_api.py`：

- 權限 gate 對映
- `/funnel/board` 預設當期、空 term 回空陣列
- timeline union 排序正確
- 各 transition 端點 happy path

### 8.6 不測

- 前端 Kanban UI（Phase B）
- 既有 `convert_recruitment_to_student` regression（保持原 test）

## 9. 部署順序

1. Alembic migration（`academic_terms` + `recruitment_event_log`）
2. Service 層 + scheduler（含 settings）
3. API endpoints
4. OpenAPI codegen → 前端 schema.d.ts 自動更新（含 ts strict mode 對映）
5. Phase B 在前端起手

## 10. 不做（範疇外）

- 前端 Kanban / academic_terms 設定頁（Phase B）
- 「批次推進」按鈕（Phase B 視需要加）
- 開學日預覽通知（YAGNI）
- 訪視階段加 `classroom_id` FK（user 否決 — 班別在報到時才鎖）
- 學號規則可配置化（目前 hardcode `{年}-{code}-{NN}`）
