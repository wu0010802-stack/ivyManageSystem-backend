# SPEC-011：招生漏斗與流失預警

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `api/analytics.py` + `services/analytics/`（`constants.py`、`funnel_service.py`、`churn_service.py`）+ `services/recruitment_funnel.py` + `services/recruitment_conversion.py` + `services/recruitment_lifecycle.py` + `services/recruitment_market_intelligence.py` + `services/recruitment_term_advance_scheduler.py` + `services/report_cache_service.py` + `schemas/recruitment_funnel.py` + `models/report_cache.py` + `models/recruitment.py` |
| Related | `docs/superpowers/specs/2026-05-22-recruitment-funnel-phase-a-design.md` |

---

## Overview

「經營分析」模組為 MVP 階段的雙引擎報表服務：

1. **招生漏斗（funnel）** — 6 階段轉換率視圖，採「**雙源拼接**」設計：visit 端（`RecruitmentVisit` + `ParentInquiry`）與 student 端（`Student.lifecycle_status`）拼成完整漏斗。由於 `RecruitmentVisit` 與 `Student` 無直接 FK，切片時改以「招生年月 + 班別 + 來源」作為共用維度（不直接 join）。
2. **流失預警（churn）** — A/C/D 三訊號實作（A=連續缺勤 3 工作日、C=`on_leave` 滿 30 天、D=當期學費逾期 14 天），對 `active`/`on_leave` 學生做嚴重度評分；同時提供 12 個月歷史 `withdrawn`/`transferred` 趨勢。

設計約束：

- **derived state**：funnel 4 階段（`visited`/`deposited`/`enrolled`/`active`）完全由 `(RecruitmentVisit.has_deposit, RecruitmentVisit.enrolled, Student.lifecycle_status, Student.recruitment_visit_id)` 推導，**不在 `recruitment_visits` 加 stage 欄位**，避免 dual source of truth（出處：`services/recruitment_funnel.derive_stage()`）。
- **無 FK join**：analytics 漏斗端 (`services/analytics/funnel_service.py`) 不 join visit ↔ student，僅各自統計。Phase A 狀態機 (`services/recruitment_funnel.py`) 才透過 `Student.recruitment_visit_id` 關聯。
- **權限模型**：所有 `/api/analytics/*` 端點走 `require_staff_permission(Permission.BUSINESS_ANALYTICS)`（bit `1 << 40`），預設僅核心 7 角色中的 admin/supervisor 持有；at-risk 端點對 `STUDENTS_READ` 缺權者額外將 `student_name` 遮罩為 `***`。
- **快取**：三端點皆透過 `ReportCacheService.get_or_build()` 走 `report_snapshots` 表，TTL 三檔分流（funnel 30 min、at-risk 5 min、history 1 hr）。**目前僅靠 TTL 失效**，學生 lifecycle 轉移與學費繳清等變動點未主動 invalidate（已知 MVP 折衷）。

---

## Interface Definitions

### HTTP 端點

#### `GET /api/analytics/funnel`

| 欄位 | 內容 |
|------|------|
| 權限 | `BUSINESS_ANALYTICS` |
| Query | `start: date`（必填，含）、`end: date`（必填，含）、`grade: str?`、`source: str?` |
| 回傳 | `{ stages: [...], no_deposit_reasons: [...], by_source: [...], by_grade: [...], filters: {...} }` |
| Cache | `category=analytics_funnel`、TTL 1800 秒（30 min） |
| 驗證 | `start > end` 回 HTTP 400「start 必須 ≤ end」 |
| Cache key 組成 | `{start.isoformat, end.isoformat, grade, source, today.isoformat}` |

#### `GET /api/analytics/churn/at-risk`

| 欄位 | 內容 |
|------|------|
| 權限 | `BUSINESS_ANALYTICS` |
| Query | （無） |
| 回傳 | `[{student_id, student_name, classroom_name, lifecycle_status, signals: [...], primary_severity}, ...]` |
| Cache | `category=analytics_churn_at_risk`、TTL 300 秒（5 min） |
| 額外規則 | 缺 `STUDENTS_READ` 時 `student_name` 顯示 `"***"`（個資遮罩） |
| Cache key 組成 | `{today.isoformat, can_read}` |

#### `GET /api/analytics/churn/history`

| 欄位 | 內容 |
|------|------|
| 權限 | `BUSINESS_ANALYTICS` |
| Query | `months: int = 12`（範圍 1~36） |
| 回傳 | `{ monthly: [...], by_reason: [...] }` |
| Cache | `category=analytics_churn_history`、TTL 3600 秒（1 hr） |
| Cache key 組成 | `{months, today.isoformat}` |

---

### 內部 Python public function

#### `services/analytics/funnel_service.py`

| 函式 | 簽章 | 用途 |
|------|------|------|
| `build_funnel()` | `(session, *, start_date, end_date, today, grade_filter=None, source_filter=None) -> dict` | API 入口；聚合 visit/student 端統計、原因分布、來源/班別切片。回傳含 `stages/no_deposit_reasons/by_source/by_grade/filters` |
| `count_visit_side_stages()` | `(session, *, start_date, end_date, grade_filter=None, source_filter=None) -> dict` | 回 `{lead, deposit, enrolled}`；`lead = visit 數 + ParentInquiry 數`（兩源不去重） |
| `count_student_side_stages()` | `(session, *, start_date, end_date, today, grade_filter=None, source_filter=None) -> dict` | 回 `{active, retained_1m, retained_6m}`；`source_filter` 設定時誠實回傳全 0（Student 無 source 欄位） |
| `summarize_no_deposit_reasons()` | `(session, *, start_date, end_date, grade_filter=None, source_filter=None) -> list[dict]` | 彙整 `has_deposit=False AND no_deposit_reason 非空` 的 visit；按 count desc 排序 |
| `slice_by_source()` | `(session, *, start_date, end_date) -> list[dict]` | 依 `RecruitmentVisit.source` 切片；含 `conversion = enrolled / lead`，3 位小數 |
| `slice_by_grade()` | `(session, *, start_date, end_date, today) -> list[dict]` | 依 grade 切片；grade 集合 = `visit.grade ∪ ClassGrade.name` |
| `_visit_in_range()` | `(visit, start, end) -> bool`（私有） | 用 `parse_roc_month(visit.month)` 把民國月份字串轉成西元月份範圍判定 |
| `_is_retained()` | `(student, today, window_days) -> bool`（私有） | 留存判定：入學日 + window ≤ today 且未在窗口內退/轉；`graduated` 不視為流失 |

#### `services/analytics/churn_service.py`

| 函式 | 簽章 | 用途 |
|------|------|------|
| `detect_at_risk_students()` | `(session, *, today, can_read_students=True) -> list[dict]` | 彙總 A/C/D 訊號去重學生，計算 `primary_severity`；按嚴重度 → 訊號數排序 |
| `build_churn_history()` | `(session, *, months=12, today) -> dict` | 過去 N 月 `退學`/`轉出` 趨勢 + 流失原因分布 |
| `detect_signal_consecutive_absence()` | `(session, *, today) -> list[dict]` | A 訊號：active 學生末端連續 ≥ 3 工作日「缺席」 |
| `detect_signal_long_on_leave()` | `(session, *, today) -> list[dict]` | C 訊號：`on_leave` 學生最近一筆「休學」`StudentChangeLog.event_date` 距今 ≥ 30 天 |
| `detect_signal_fee_overdue()` | `(session, *, today) -> list[dict]` | D 訊號：當期 `StudentFeeRecord.payment_date IS NULL` 且 `today - term_start_date ≥ 14` |
| `_is_workday()` | `(d) -> bool`（私有） | 簡易：`weekday() < 5`；MVP **不引入** `services.workday_rules` 精確假日表 |
| `_build_unrecorded_class_days()` | `(students, by_student, candidate_days) -> set`（私有） | 整班漏點名過濾：若某天某班所有 active 學生皆無紀錄或皆「缺席」，視為老師當日漏點名 |

#### `services/analytics/constants.py`

| 名稱 | 值 | 用途 |
|------|------|------|
| `CHURN_CONSECUTIVE_ABSENCE_DAYS` | `3` | A 訊號閾值 |
| `CHURN_ON_LEAVE_DAYS` | `30` | C 訊號閾值 |
| `CHURN_FEE_OVERDUE_DAYS` | `14` | D 訊號閾值 |
| `FUNNEL_STAGES` | `["lead","deposit","enrolled","active","retained_1m","retained_6m"]` | 6 階段順序 |
| `FUNNEL_STAGE_LABELS` | `{...}` | 階段中文標籤 |
| `RETENTION_WINDOWS_DAYS` | `{"1m": 30, "6m": 180}` | 留存判定窗口 |
| `parse_roc_month(raw)` | `(roc.month str) -> (year, month) or None` | e.g. `'115.03' → (2026, 3)`；無效格式回 None |
| `term_start_date(period)` | `(period str) -> date or None` | `'2025-1' → date(2025, 9, 1)`；`'2025-2' → date(2026, 2, 1)` |

#### `services/recruitment_funnel.py`（漏斗狀態機）

| 函式 | 用途 |
|------|------|
| `derive_stage(visit, student) -> Stage` | 從 `(visit.has_deposit, student.lifecycle_status)` 推導 4 階段，**student 存在性優先** |
| `can_transition(from, to) -> bool` | Phase A 一律 `True`，保留位置給未來收緊 |
| `is_destructive(from, to) -> bool` | `from ∈ {enrolled, active}` 且 `to` 排序 < `from` 視為 destructive |
| `transition_visit(session, visit_id, to_stage, actor_user_id, *, classroom_id=None, reason=None) -> TransitionResult` | 單一 atomic stage transition；流程：lock visit → derive from_stage → 規則檢查 → dispatch sub-action → 寫 event log → flush |
| `assert_student_revertable(session, student_id)` | destructive revert 前檢查 9 類下游業務記錄；任一存在則 raise `RecruitmentFunnelError("REVERT_STUDENT_HAS_DATA")` |
| `next_student_id_code(session, school_year, class_code) -> str` | 產 `{year}-{class_code}-{NN}`（NN 兩位數零填）；PG 用 `pg_advisory_xact_lock` 防並發撞號 |

#### `services/recruitment_conversion.py`

| 函式 | 用途 |
|------|------|
| `convert_recruitment_to_student(session, recruitment_visit_id, student_id_code=None, *, classroom_id=None, enrollment_date=None, initial_lifecycle_status="enrolled", gender=None, recorded_by=None) -> ConversionResult` | 從 `RecruitmentVisit` 建立正式 `Student`（原子操作）；包括 Guardian 建立、`StudentChangeLog("入學")` 寫入、`RecruitmentEventLog("converted")` 寫入、`visit.enrolled=True`。`initial_lifecycle_status` 僅允許 `enrolled` 或 `active`。 |

#### `services/recruitment_lifecycle.py`

| 函式 | 用途 |
|------|------|
| `advance_term_to_active(session, school_year, semester) -> dict` | 把該學期 window 內的 `enrolled` 學生升 `active`；Window = `[term.start_date - window_days, term.start_date]`；window_days 取 `settings.scheduler.recruitment_term_advance_window_days`（預設 90）。回 `{advanced, skipped, failed}` |

#### `services/recruitment_term_advance_scheduler.py`

| 函式 | 用途 |
|------|------|
| `scheduler_enabled() -> bool` | 讀 `settings.scheduler.recruitment_term_advance_enabled` |
| `run_recruitment_term_advance_scheduler(stop_event)` | 每日輪詢；今天 = 某 `AcademicTerm.start_date` 則對該 term 跑 `advance_term_to_active`。`check_interval` 由 settings 控制 |

#### `services/recruitment_market_intelligence.py`（與分析有關的公開入口）

| 函式 | 用途 |
|------|------|
| `search_nearby_kindergartens(session, *, radius_km=10.0, bounds=None) -> dict` | Google Places + MOE DB + geocode cache 三段融合，回鄰近幼兒園清單 |
| `build_market_intelligence_snapshot(session, dataset_scope=None) -> dict` | 行政區層級市場情報快照（含人口密度、0-6 歲人口、競品分布、平均通勤分鐘） |
| `sync_market_intelligence(session, *, hotspot_limit=200) -> dict` | 同步熱點地址 geocode、人口資料、行政區洞察快取 |
| `get_or_create_campus_setting(session) -> RecruitmentCampusSetting` | 取得或建立單筆本園設定 |
| `upsert_campus_setting(session, payload) -> RecruitmentCampusSetting` | 更新本園設定；座標缺漏或地址變更時自動 geocode |
| `geocode_all_competitor_schools(session, *, limit=100) -> dict` | 批量 geocode 尚無座標的 `CompetitorSchool` |
| `resolve_address_metadata(address, *, campus=None, include_land_use=True) -> dict` | 單一地址完整 metadata 解析（geocode + 行政區 + 通勤）|

#### `services/report_cache_service.py`（cache layer 接口）

| 方法 | 用途 |
|------|------|
| `build_cache_key(category, params) -> str` | `"{category}:{json.dumps(params, sort_keys=True)}"` |
| `get_or_build(session, *, category, ttl_seconds, builder, params=None, force_refresh=False)` | 若 snapshot 未過期回 cached payload；否則 `builder()` 後寫入 `report_snapshots` |
| `invalidate_category(session, category) -> int` | 刪除單一 category 全部 snapshot |
| `invalidate_categories(session, *categories) -> int` | 多 category 批次刪除 |

---

## DTO Definitions

### `RecruitmentVisit`（`models/recruitment.py`）— 漏斗相關欄位

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `int PK` | |
| `month` | `String(10)` | 民國月份字串，如 `"115.03"`；funnel 解析靠 `parse_roc_month()` |
| `child_name` | `String(50) NOT NULL` | 幼生姓名 |
| `birthday` | `Date?` | |
| `grade` | `String(20)?` | 適讀班級；funnel `grade_filter` 比對對象 |
| `phone` | `String(100)?` | |
| `address` | `String(200)?` | |
| `district` | `String(30)?` | 行政區 |
| `source` | `String(50)?` | 幼生來源；funnel `source_filter` 比對對象 |
| `referrer` | `String(50)?` | 介紹者 |
| `has_deposit` | `Boolean NOT NULL` | 是否預繳；`deposit_count` 統計來源 |
| `enrolled` | `Boolean NOT NULL` | 是否已實際註冊；`enrolled_count` 統計來源 |
| `transfer_term` | `Boolean NOT NULL` | 是否轉到其他學期 |
| `no_deposit_reason` | `String(60)?` | 未預繳原因分類；`summarize_no_deposit_reasons()` 過濾 NOT NULL & != "" |
| `no_deposit_reason_detail` | `Text?` | |
| `expected_start_label` | `String(30)?` | 預計就讀月份標籤 |
| `created_at` / `updated_at` | `DateTime` | 預設 `now_taipei_naive` |

### `RecruitmentEventLog`（`models/recruitment.py`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `int PK` | |
| `recruitment_visit_id` | `int FK CASCADE` | 漏斗 visit |
| `event_type` | `String(40)` | `deposit_added`/`deposit_removed`/`converted`/`activated`/`revert_converted`/`revert_activated` 等 |
| `from_stage`, `to_stage` | `String(20)` | `visited`/`deposited`/`enrolled`/`active` |
| `student_id` | `int FK SET NULL` | converted 後寫入 |
| `reason` | `Text?` | destructive 操作必填 |
| `actor_user_id` | `int FK SET NULL` | |
| `metadata_json` | `JSON / JSONB` | PG 用 JSONB；SQLite 退化 JSON |
| `created_at` | `DateTime NOT NULL` | |

### `Student.lifecycle_status` 列舉（`models/classroom.py`）

| 常數 | 值 | 漏斗對應 |
|------|------|---------|
| `LIFECYCLE_PROSPECT` | `prospect` | 未進入漏斗（招生中） |
| `LIFECYCLE_ENROLLED` | `enrolled` | `enrolled` 階段 |
| `LIFECYCLE_ACTIVE` | `active` | `active` 階段（funnel 留存判定起點） |
| `LIFECYCLE_ON_LEAVE` | `on_leave` | C 訊號偵測對象 |
| `LIFECYCLE_TRANSFERRED` | `transferred` | 終態；history 統計 |
| `LIFECYCLE_WITHDRAWN` | `withdrawn` | 終態；history 統計 |
| `LIFECYCLE_GRADUATED` | `graduated` | 終態；`_is_retained()` **不視為流失** |

### `Student` 漏斗相關欄位

| 欄位 | 型別 | 用途 |
|------|------|------|
| `enrollment_date` | `Date?` | 入學日；funnel `active`/留存判定起點 |
| `withdrawal_date` | `Date?` | 退學/轉出日；`_is_retained()` 排除窗口內離校 |
| `lifecycle_status` | `String(20)` | 階段判定主鍵 |
| `recruitment_visit_id` | `int FK?` | 與 `RecruitmentVisit` 關聯（Phase A 狀態機用；analytics 端不 join） |
| `classroom_id` | `int FK?` | 透過 `Classroom → ClassGrade.name` 切 `grade_filter` |

### `StudentChangeLog`（`models/student_log.py`）— churn 相關

| 欄位 | 用途 |
|------|------|
| `event_type` | `入學`/`復學`/`退學`/`轉出`/`轉入`/`畢業`；history 統計取 `退學`、`轉出`；C 訊號取 `休學` |
| `event_date` | `Date NOT NULL` |
| `reason` | `String(50)?` | history `by_reason` 分布來源 |

### `StudentFeeRecord`（`models/fees.py`）— D 訊號相關

| 欄位 | 用途 |
|------|------|
| `period` | `'{民國年+1911}-{semester}'` 西元字串；`term_start_date()` 解析 |
| `payment_date` | `Date?`；NULL 視為未繳 |
| `fee_item_name` | 顯示在 `detail` 文字 |

### `FunnelResult`（`build_funnel()` 回傳；非 Pydantic schema）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `stages` | `list[{key, label, count, rate_from_prev}]` | 6 階段；`rate_from_prev = count / prev_count`，3 位小數；首階段 `None` |
| `no_deposit_reasons` | `list[{reason, count}]` | 按 count desc |
| `by_source` | `list[{source, lead, deposit, enrolled, conversion}]` | 按 lead desc |
| `by_grade` | `list[{grade, lead, deposit, enrolled, active, conversion}]` | 按 lead desc |
| `filters` | `{start, end, grade, source}` | echo input |

### `AtRiskStudent`（`detect_at_risk_students()` element；非 Pydantic schema）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `student_id` | `int` | |
| `student_name` | `str` | 缺 `STUDENTS_READ` 顯示 `"***"` |
| `classroom_name` | `str?` | |
| `lifecycle_status` | `str` | |
| `signals` | `list[{type, severity, detail}]` | type ∈ `consecutive_absence`/`long_on_leave`/`fee_overdue` |
| `primary_severity` | `str` | `low`/`medium`/`high`，取訊號中最高 |

### `FunnelCard` / `FunnelSummary` / `FunnelBoardOut`（`schemas/recruitment_funnel.py`）

| Schema | 欄位 | 用途 |
|------|------|------|
| `FunnelCard` | `visit_id, child_name, grade, phone, district, source, deposited_at, student_id, current_stage` | Phase A Kanban 卡片 |
| `FunnelSummary` | `visited_count, deposited_count, enrolled_count, active_count` | 4 階段計數 |
| `FunnelBoardOut` | `stages: dict[Stage, list[FunnelCard]], summary: FunnelSummary` | Phase A Kanban board |
| `TransitionIn` | `to_stage, classroom_id?, reason?` | `transition_visit()` 請求體 |
| `TransitionOut` | `visit_id, from_stage, to_stage, student_id?, event_log_id, warnings[]` | `transition_visit()` 回傳 |
| `TimelineEvent` | `source ∈ ['recruitment','student'], event_type, from_stage?, to_stage?, actor_user_id?, reason?, created_at` | 漏斗 + 學生雙源 timeline 元素 |
| `TimelineOut` | `events: list[TimelineEvent]` | |
| `Stage` | `Literal["visited","deposited","enrolled","active"]` | Phase A 4 階段（**注意：與 analytics 6 階段 funnel 不同**，後者另含 lead/retained_1m/retained_6m） |

### `ReportSnapshot`（`models/report_cache.py`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `int PK` | |
| `cache_key` | `String(255) UNIQUE NOT NULL` | `"{category}:{json sort_keys}"` |
| `category` | `String(50) NOT NULL` | `analytics_funnel`/`analytics_churn_at_risk`/`analytics_churn_history` 等 |
| `payload` | `Text NOT NULL` | JSON 序列化內容 |
| `computed_at` | `DateTime NOT NULL` | 預設 `now_taipei_naive` |
| `expires_at` | `DateTime NOT NULL` | `computed_at + ttl_seconds` |
| `created_at` / `updated_at` | `DateTime NOT NULL` | |
| Index | `ix_report_snapshots_category` / `ix_report_snapshots_expires_at` | |

---

## Business Rules

### 1. 6 階段漏斗定義

漏斗依序為 `lead → deposit → enrolled → active → retained_1m → retained_6m`：

| 階段 | 來源 | 計數規則 |
|------|------|---------|
| `lead` | visit + ParentInquiry | `count(RecruitmentVisit in range) + count(ParentInquiry.created_at in range)`。兩源**不去重**。[needs review] |
| `deposit` | visit | `count(visit.has_deposit=True)` |
| `enrolled` | visit | `count(visit.enrolled=True)` |
| `active` | Student | `count(Student.enrollment_date in range AND lifecycle_status ∈ {active, on_leave, graduated, transferred, withdrawn})` |
| `retained_1m` | Student | active 子集 ∩ `_is_retained(student, today, 30)` |
| `retained_6m` | Student | active 子集 ∩ `_is_retained(student, today, 180)` |

`rate_from_prev = round(count / prev_count, 3)`；`prev_count == 0` 時 rate=0.0；首階段 rate=None。

### 2. 雙源拼接維度

`RecruitmentVisit` 無 `student_id` FK，因此 visit 端與 student 端用「**招生年月 + 班別 + 來源**」當共用維度，**不直接 join**（出處：`CLAUDE.md` services/analytics 章節）。

- visit 端：`v.grade == grade_filter`、`v.source == source_filter`、`parse_roc_month(v.month) ∈ [start, end]`
- student 端：透過 `Classroom → ClassGrade.name == grade_filter` 比對；`enrollment_date ∈ [start, end]`
- **`ParentInquiry` 計數不支援 grade/source filter**（無對應欄位），lead count 永遠含全量 inquiry — 已知 MVP 折衷
- **`Student` 無 source 欄位**：`source_filter` 設定時 student 端誠實回傳全 0（而非全量），避免誤導
- `slice_by_source` 僅切 visit 端；`slice_by_grade` 同時切 visit + student 兩端的 `active count`

### 3. A/C/D 三訊號定義

| 訊號 | 代號 | 閾值 | 對象 | 偵測邏輯 |
|------|------|------|------|---------|
| 連續缺勤 | A `consecutive_absence` | `CHURN_CONSECUTIVE_ABSENCE_DAYS = 3` 工作日 | `lifecycle_status == "active"` | 從 today 往前掃工作日，僅 `"缺席"` 計入連續串；`"病假"`/`"事假"`/`"出席"`/`"遲到"` 皆中斷 |
| 長期休學 | C `long_on_leave` | `CHURN_ON_LEAVE_DAYS = 30` 天 | `lifecycle_status == "on_leave"` | 最近一筆 `StudentChangeLog(event_type="休學")` 距 today 天數 ≥ 30 |
| 學費逾期 | D `fee_overdue` | `CHURN_FEE_OVERDUE_DAYS = 14` 天 | `lifecycle_status ∈ ("active", "on_leave")` | 當期 `StudentFeeRecord.payment_date IS NULL` 且 `today >= term_start_date(period) + 14` |

嚴重度：

- A 固定 `high`
- C 固定 `medium`
- D：`actual_overdue_days >= 30` → `high`；否則 `medium`
- `_severity_rank`：`low=1, medium=2, high=3`
- `primary_severity = max(signals, key=_severity_rank)`；同學多筆 D 記錄取嚴重度最高者
- 排序：`primary_severity desc → signals 數 desc`

學生請假 [unverified]：CLAUDE.md / churn_service module docstring 註明「學生請假直接反映在 `StudentAttendance.status`（"病假"/"事假"），沒有獨立的學生假單表 — `LeaveRecord` 為員工專用」。

### 4. 學期起始日 proxy

`StudentFeeRecord` 模型**無 `due_date` 欄位**，故 D 訊號改用「**學期起始日 + 14 天**」當 proxy（出處：`CLAUDE.md` services/analytics 章節）：

| `period` 字串 | 學期起始日 |
|------|------|
| `"{year}-1"`（上學期） | `date(year, 9, 1)` |
| `"{year}-2"`（下學期） | `date(year + 1, 2, 1)` |

當期判定走 `utils.academic.resolve_current_academic_term(today)` 回 `(year_民國, semester)` → 轉成 `f"{year+1911}-{semester}"` 比對 `StudentFeeRecord.period`。

歷史說明：c2 前曾 JOIN `FeeItem`；`fee_items` 表 DROP 後改 denormalized `period` 直接過濾。

### 5. 整班漏點名假缺勤過濾

A 訊號掃描前先計算 `unrecorded` 集合 `(classroom_id, day)`：若某天某班所有 active 學生皆無紀錄或皆「缺席」，視為老師當日漏點名 → 該天該班略過，**不計入連續缺勤串**。

判定式：班內所有 active 學生 `statuses` 中**沒有任何**「非缺席」紀錄（即 `non_absent == []`） → 整班該日視為漏點名。

連續性語意：**末端連續**缺勤 — 從 today 往前掃，遇非缺席即停。

工作日判定 [needs review]：`_is_workday()` 採簡易版 `d.weekday() < 5`；註解標示「精確假日表（`services.workday_rules`）需 load holiday/makeup map，MVP 不引入依賴」。

### 6. 12 個月歷史趨勢

`build_churn_history(months=12)`：

- 從 `date(today.year, today.month, 1)` 往回推 `months-1` 個月當 `earliest`
- 查 `StudentChangeLog.event_type ∈ ("退學", "轉出")` 且 `event_date ∈ [earliest, today]`
- 月度桶按 `(year, month)` 分組，每桶 `{year, month, withdrawn, transferred}`
- 額外彙整 `by_reason: [{reason, count}]`，按 count desc
- `months` 參數範圍 `1~36`（由 API `Query(12, ge=1, le=36)` 限制）

### 7. Cache TTL 三類別

| Category | TTL | 來源常數 | 理由 |
|------|------|------|------|
| `analytics_funnel` | 1800 秒（30 min） | `FUNNEL_TTL` | 漏斗變動慢，30 min 可接受 |
| `analytics_churn_at_risk` | 300 秒（5 min） | `AT_RISK_TTL` | 預警需較高即時性 |
| `analytics_churn_history` | 3600 秒（1 hr） | `CHURN_HISTORY_TTL` | 歷史月度趨勢變動極慢 |

**已知折衷**：目前僅 TTL 失效；學生 lifecycle 轉移、學費繳清等變動點**未主動 invalidate**，因此 dashboard 在 TTL 內可能仍顯示已處理的預警（出處：`api/analytics.py` L30-31 註解）。

Cache key 由 `report_cache_service.build_cache_key(category, params)` 產生：`"{category}:{json.dumps(params, sort_keys=True)}"`。

### 8. ROC 月份解析（`parse_roc_month`）

`parse_roc_month(raw: Optional[str]) -> Optional[Tuple[int, int]]`：

- 輸入：`"115.03"` 等民國年.月格式字串
- 輸出：`(西元年, 月)` e.g. `(2026, 3)`；無效格式回 `None`
- 解析失敗時呼叫端決定是否略過或 log
- 校驗 `1 <= month <= 12`，否則回 `None`

`_visit_in_range(visit, start, end)`：用解析後的 `(y, m)` 求該月份的西元 `[month_start, month_end)`，與 `[start, end]` 區間重疊即視為命中（任何一天落入即算）。

### 9. 權限模型

- 三個 `/api/analytics/*` 端點 **必須** 持有 `Permission.BUSINESS_ANALYTICS`（bit `1 << 40`），由 `require_staff_permission()` 守衛（同時擋 teacher/parent 撞管理端）
- `at-risk` 端點額外讀 `current_user.permission_names` 檢查 `STUDENTS_READ`（bit `1 << 9`）；缺權者 `student_name` 顯示 `"***"`（個資遮罩）
- 預設角色配置（`utils/permissions.py`）：`BUSINESS_ANALYTICS` 為 admin/supervisor 預設持有，teacher/parent **未持有**
- Phase A 漏斗狀態機（`api/recruitment/funnel.py` 與 `services/recruitment_funnel.py`）走獨立權限：`RECRUITMENT_READ`（看 board / timeline）、`RECRUITMENT_WRITE`（visited↔deposited）、`RECRUITMENT_CONVERT`（deposited→enrolled 及含 Student 操作）+ `STUDENTS_WRITE`（涉及 active→enrolled revert 等）

### 10. 漏斗狀態機（Phase A）原則

| 規則 | 內容 |
|------|------|
| 完全 derived | Stage 由 `(visit.has_deposit, visit.enrolled, student.lifecycle_status, recruitment_visit_id)` 推導；**不在 `recruitment_visits` 加 stage 欄位** |
| student 優先 | `derive_stage()`：student 存在性優先 — `student is not None` 時無視 visit.has_deposit |
| Destructive 必須 reason | `is_destructive()` 為 True 時 `reason` 必填，否則拋 `RecruitmentFunnelError("REASON_REQUIRED")` |
| Revert blocker | `_REVERT_BLOCKERS` 列出 9 類下游業務 model（`StudentAttendance`/`StudentFeeRecord`/`StudentAssessment`/`StudentIncident`/`StudentObservation`/`StudentAllergy`/`StudentMedicationOrder`/`StudentMeasurement`/`StudentMilestone`）；任一存在則禁止 destructive revert |
| 並發鎖 | `_load_visit_locked()`：PG 用 `SELECT FOR UPDATE`；其他 dialect 跳過鎖 |
| 學號自動產生 | `next_student_id_code()`：PG 用 `pg_advisory_xact_lock(hash(year, class_code) % 2^31)` 防撞號 |

### 11. 半自動學期推進

`recruitment_term_advance_scheduler`：

- 每日輪詢（`check_interval` 由 `settings.scheduler.recruitment_term_advance_check_interval` 控）
- `today == AcademicTerm.start_date` 時對該 term 跑 `advance_term_to_active(school_year, semester)`
- Window: `[term.start_date - window_days, term.start_date]`；`window_days` 預設 90
- 篩選條件：`recruitment_visit_id IS NOT NULL` 且 `enrollment_date ∈ window` 且 `lifecycle_status == enrolled`
- 對每位學生呼叫 `transition_visit(to_stage="active", reason="scheduler:term_start")`；失敗 case 分 `advanced`/`skipped`/`failed` 統計
- 啟用旗標：`settings.scheduler.recruitment_term_advance_enabled`

### 12. 市場情報模組（`recruitment_market_intelligence.py`）

非 analytics 直接呼叫，但屬招生模組分析能力，**權限** `RECRUITMENT_READ` / `RECRUITMENT_WRITE`：

- **資料融合三源**（出處：`CLAUDE.md` 招生模組附近幼兒園資料）：
  1. `competitor_school` DB（教育部爬蟲快取）— 電話、地址、類型、核定人數、月費、`has_penalty`
  2. kiang.github.io — 月費備援、裁罰詳情文字
  3. Google Places API — 名稱、座標、評分、Maps 連結
- **裁罰顯示規則**：`has_penalty=False` → 不顯示裁罰；`has_penalty=True` → 顯示 kiang 裁罰詳情
- **月費規則**：DB 有值優先，DB null 才用 kiang
- **名稱比對閾值**：`MATCH_THRESHOLD = 80`、`HIGH_CONFIDENCE = 120`；採名稱 + 地址 + 距離多信號評分
- **MOE gap-fill 上限**：`MOE_GEOCODE_PER_REQUEST_LIMIT = 5`（每次 request 對新學校呼叫 geocoding 上限）
- **Google Places 上限**：`GOOGLE_PLACES_MAX_RESULTS = 60`、`GOOGLE_PLACES_PAGE_SIZE = 20`

---

## Changelog

| 版本 | 日期 | 內容 |
|------|------|------|
| v0.1 | 2026-05-28 | Initial draft — 提取 analytics MVP 6 階段漏斗（雙源拼接）+ A/C/D 三訊號流失預警，含 Phase A 狀態機 / 市場情報模組 / cache layer 三個關聯 service 的介面定義 |
