# 學期改為系統全自動判斷（移除手動設定）— 設計文件

- 日期：2026-06-03
- 範圍：跨前後端（ivy-backend + ivy-frontend）
- 狀態：設計待實作

## 1. 背景與目標

系統設定目前有「學年/學期」分頁（`SettingsView` → `SettingsAcademicTermsTab`），管理員可新增/編輯每個學期並**手動填入** `start_date` / `end_date`，再手動按「設為當前」切換學期。

但台灣幼兒園學期日期是固定的、可由「學年 + 上/下學期」完全推算：

| 學期 | 期間 | 民國學年對應 |
|------|------|------------|
| 上學期（semester 1） | **8/1 ～ 隔年 1/31** | 8 月起算為當學年 |
| 下學期（semester 2） | **2/1 ～ 同年 7/31** | 接續同一學年 |

ROC → 西元：`西元 = 民國學年 + 1911`。例：114 學年上學期 = 2025/8/1～2026/1/31；114 學年下學期 = 2026/2/1～2026/7/31。

**目標**：移除「學年/學期」這個設定，讓系統依「今天日期」全自動判斷當前學期，並在學期跨界（8/1、2/1）時自動完成既有的結轉副作用（班級延續、假別額度結轉），管理員不需任何手動操作。

**使用者已確認的決策**：
- 學期由系統依日期全自動判斷（不保留手動切換）。
- 學期自動切換時：寫一筆稽核紀錄 + 後台提醒 admin。
- `academic_terms` 的 HTTP 端點（含唯讀 GET）一併移除整個 router。

## 2. 既有現況（探查結論）

讀取面其實已大致就緒，本次主要工作是「移除設定 UI」與「新增自動切換驅動器」。

- **後端 `utils/academic.py`**：已有純函式 `_resolve_by_date(date)`，邏輯正好就是 8 月/2 月邊界（見現碼 `:25-36`）。`resolve_current_academic_term()` 目前「先查 `AcademicTerm.is_current`、查不到才 fallback 日期」。
- **後端 `academic_terms` 表**：欄位 `school_year` / `semester` / `start_date` / `end_date` / `is_current`（partial unique index 保證最多一筆 `is_current=true`）。
- **學期切換事件鏈** `utils/term_events.py:fire_term_changed(old, new, session)`：目前由 `api/academic_terms.py` 的 set-current handler 同 transaction 呼叫，觸發三個 subscriber：
  - `services/term_subscribers/classroom_carry_over.py`（同學年 1→2 班級延續）
  - `services/term_subscribers/leave_quota_cutover.py`（跨學年下→上，為在職員工建新學年假別額度；**已有防重複守衛** `:67-83`）
  - `services/term_subscribers/activity_semester_tag.py`（目前 placeholder）
- **前端**：所有「選學年/學期」下拉**已用純函式** `getCurrentAcademicTerm()`（`src/utils/academic.ts`）+ store（`src/stores/academicTerm.ts`），**零 API 呼叫**。`src/api/academicTerms.ts` 的 CRUD **僅** `SettingsAcademicTermsTab.vue` 使用。
- **後端其他 `AcademicTerm` query 站點**（`recruitment_lifecycle.py:35`、`student_lifecycle_overview.py:383`、`recruitment_term_advance_scheduler.py:66`）**都帶 null 防守**，不假設任意學期 row 必存在。
- **排程基建**：`config/scheduler.py` 集中 enable/interval；`main.py` lifespan 用 `scheduler_enabled()` + `asyncio.create_task` 啟動；`utils/scheduler_observability.py:scheduler_iteration()` 提供 heartbeat。`recruitment_term_advance_enabled` 已是預設 `True`。

## 3. 架構決策

### 決策 A：保留 `academic_terms` 表，改由系統維護

不拆表。表留著且由系統自動填，則所有讀 `school_year/semester` 的消費端**完全不用改**。只移除「讓使用者寫入」的部分（設定 UI + 全部 HTTP 端點）。

`is_current` 旗標**降級為「排程器的結轉標記」**——只用來判斷「這個學期的結轉事件是否已觸發過」（冪等），**不再是「現在是哪個學期」的讀取來源**。

### 決策 B：日期純函式為「當前學期」的唯一真相

把 `resolve_current_academic_term()`（無 `target_date` 時）改為**直接回傳 `_resolve_by_date(today_taipei())`**，不再讀 `is_current` row。如此前端（已是日期推算）與後端對「現在是哪個學期」**永遠一致**，不會在跨界當下出現一端已換、另一端未換的窗口。

學期切換的**副作用**（班級延續、假別結轉）由排程器在背景補上，最多落後一個排程週期（預設 1 小時），不影響「當前學期」這個答案。

> 已稽核（grep `AcademicTerm.is_current` 於 api/ services/ utils/）：**唯一**讀 `AcademicTerm.is_current` 的位置是 `utils/academic.py:65`，由 B1 改掉；`classrooms.py` 等是透過 `resolve_current_academic_term()` / `resolve_academic_term_filters()` 間接取得，改 B1 後自動變日期推導，不需逐一修改。（`api/auth.py` 的 `is_current` 屬 StaffRefreshToken 同名欄位，無關。）`is_current` 自此僅供排程器當結轉標記。

### 決策 C：唯一的學期切換驅動器 = 每日排程器

新增 `services/academic_term_turnover_scheduler.py`，每個週期執行一次 reconcile：

```
T = _resolve_by_date(today_taipei())          # 日期算出的當前學期
C = session.query(AcademicTerm).filter(is_current == True).first()

if C is None:
    # 缺：全新 DB / 從未種子化 → 靜默建立基準，【不觸發事件】
    ensure_row(T); set_current(T)
elif (C.school_year, C.semester) != (T.school_year, T.semester):
    # 真的跨界 → 結轉【觸發事件】
    ensure_row(T)
    C.is_current = False; new.is_current = True
    fire_term_changed(old=C, new=new, session=session)
    write_audit_in_session(..., entity_type="academic_term",
        summary=f"學期自動切換：{C.school_year}-{C.semester} → {T.school_year}-{T.semester}")
# else: 已對齊 → no-op（用 is_current 當標記，天然冪等）
```

- `ensure_row(T)`：找不到 `(school_year, semester)` 的 row 就用 `term_bounds()` 算出固定起訖日建立。
- 重用既有 `fire_term_changed()`，不重寫事件觸發。
- 在 `main.py` lifespan 註冊；**並在啟動時主動跑一次**（補抓「系統關機期間跨界」的情形——此時 C≠T 是正當跨界，理應觸發結轉）。

### 決策 D：防「部署當下誤觸發」(首次部署防護)

最危險情境：既有 prod 的 `is_current` 剛好停在一個與日期不符的學期（例如 admin 之前設了 113-2 但現在已是 114-1），上線後排程器第一次跑就把它判成「跨界」→ 無人在場下批次建假別額度。

防法：**一次性 data migration 在部署時（`alembic upgrade`，App 啟動前）靜默對齊**：
1. 把所有現有 row 的 `start_date`/`end_date` 正規化成 `term_bounds()` 的固定值。
2. 把 `is_current` 校正成 `_resolve_by_date(today)` 對應的 row（缺則建立），其餘清 `is_current`。
3. **完全不觸發任何事件**（純資料操作）。

如此 App 啟動時 `C == T`，排程器看到已對齊 → 不誤觸發。之後唯有「時間真的走過 8/1 或 2/1」才會出現 `C ≠ T`。

> 全新 / CI 測試 DB 走 `create_all + stamp` 會跳過此 migration（無資料、`is_current` 為 None）→ 由排程器/啟動種子的「缺 → 靜默建立」分支處理，亦不觸發事件。兩條路徑都安全。

### 決策 E：防重複硬化

- `leave_quota_cutover.py` 已有防重複（檢查該學年 quota 是否已建）。
- `classroom_carry_over.py` 無顯式防重複，僅靠「只在同年 1→2 執行」+ `is_current` 標記保證單次。本次**補一個防重複守衛**（建立目標學期班級前，先檢查該班級於目標 semester 是否已存在）作為保險。

## 4. 一個要講白的行為變更

招生排程器 `recruitment_term_advance_scheduler.py` 在「學期 `start_date == 今天`」時把 enrolled→active。`start_date` 固定成 8/1 後，**學生會在 8/1 轉為在學，而非學校實際開學日**。這符合「上學期永遠 8 月起」的模型，但在此明確記錄為已知行為，而非隱性副作用。

## 5. 改動清單

### 5.1 後端（ivy-backend）

| # | 檔案 | 動作 |
|---|------|------|
| B1 | `utils/academic.py` | `resolve_current_academic_term()` 無 `target_date` 時改回傳 `_resolve_by_date(today_taipei())`；移除/更新「請至 /academic-terms UI 設定」的 warning 文案。新增純函式 `term_bounds(school_year, semester) -> (date, date)`。 |
| B2 | `services/academic_term_turnover_scheduler.py` | **新檔**。決策 C 的 reconcile 邏輯 + `scheduler_enabled()` + `run_*` async loop（用 `scheduler_iteration` heartbeat）。 |
| B3 | `config/scheduler.py` | 新增 `academic_term_turnover_enabled: BoolEnv = True`、`academic_term_turnover_check_interval: int = 3600`。 |
| B4 | `main.py` | lifespan 註冊新排程器（對齊既有 pattern）；啟動時主動跑一次 reconcile。 |
| B5 | `api/academic_terms.py` + `main.py` | **移除整個 router**（list / current / POST / PUT / DELETE / set-current）並取消註冊。`Permission.SETTINGS_WRITE` enum 本身保留（他處仍用）。 |
| B6 | `services/term_subscribers/classroom_carry_over.py` | 補防重複守衛（決策 E）。 |
| B7 | `services/dashboard_query_service.py` | `build_notification_summary()` 加一則 reminder：當 `today` 在當前學期 `start_date` 起 7 天內，顯示「本學期已自動切換為 N 學年上/下學期，已完成班級延續與假別結轉」。**純日期計算、無狀態、自動消失、不需新表**。 |
| B8 | `alembic/versions/*.py` | **新 migration**：決策 D 的正規化 + 靜默對齊。日期在 Python 端用 `term_bounds()` 算、以 bound param UPDATE（避免內嵌字面冒號被當 bind param）。down 還原為 no-op 資料遷移（不可逆的資料正規化，down 留註解說明）。 |
| B9 | `tests/` | 見 §6。 |

### 5.2 前端（ivy-frontend）

| # | 檔案 | 動作 |
|---|------|------|
| F1 | `src/views/SettingsView.vue` | 移除「學年/學期」tab 與其 `<SettingsAcademicTermsTab />` 掛載。 |
| F2 | `src/components/settings/SettingsAcademicTermsTab.vue` | **刪除**。 |
| F3 | `src/components/settings/__tests__/SettingsAcademicTermsTab.test.ts` | **刪除**。 |
| F4 | `src/api/academicTerms.ts` | **刪除**（無其他消費端）。 |
| F5 | codegen | 後端移除端點後跑 `dump_openapi.py` + `npm run gen:api`，更新 `schema.d.ts`（針對性移除，勿整檔重生）。 |

> 前端 `stores/academicTerm.ts` 與 `utils/academic.ts` 的純函式**不動**——它們已是日期推算，正是新模型要的。

## 6. 測試

- **B1 純函式**：`_resolve_by_date` 邊界（1/31、2/1、7/31、8/1）+ `term_bounds` 正反推一致性（ROC↔西元；上學期跨年；下學期同年）。
- **B2 排程器 reconcile**（三分支，建議用 in-memory SQLite + 注入 `today`）：
  - `C is None` → 靜默建立、**不呼叫** `fire_term_changed`（用 mock/spy 斷言）。
  - `C == T` → no-op、不觸發。
  - `C != T` → 翻 `is_current`、**呼叫一次** `fire_term_changed`、寫一筆 audit。
  - 冪等：同一週期重跑第二次 → 已對齊 → 不再觸發。
- **B6**：classroom carry-over 重跑不 double-create。
- **B7**：term `start_date` 起算 0/6/7/8 天的 reminder 出現/消失。
- **B8 migration**：載入既有自訂日期 + 錯誤 `is_current` 的 fixture，upgrade 後日期正規化、`is_current` 對齊日期、**無事件副作用**（quota row 數不變）。
- **回歸**：`resolve_current_academic_term()` 改動後，依賴它的薪資/考核/班級/招生測試需綠。

## 7. 部署注意

- migration 含 backfill：合併前在 dev DB 手動 `alembic upgrade heads` 驗證；確認未產生多 head（必要時補 merge head）。
- 排程器**預設開啟**（它是唯一的學期切換驅動，關閉等於學期永不換）。決策 D 的 migration 已保證上線不誤觸發。
- prod 上線順序：後端先（migration + 端點移除 + 排程器）→ 前端後（移除 tab + codegen）。
- 上線後更新 workspace `CLAUDE.md`：學期改為日期自動推導、設定頁已移除、`academic_terms` 由排程器維護。

## 8. 不做（YAGNI）

- 不做可設定的「學期月份邊界」（業主明確說固定 8 月/2 月）。
- 不保留手動「設為當前」/緊急覆寫（使用者選全自動；日後若需要再加）。
- 不建新的通知表（重用既有 dashboard notification summary + audit log）。
- 不重寫 20+ 消費端（保留表即免改）。
