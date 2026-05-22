# 考核年終獎金 → 薪資的橋接（appraisal year-end payout）

- 日期：2026-05-22
- 狀態：design approved，待 plan
- 範圍：跨前後端（後端重心；前端少量 UI 改動）
- 起點：workspace 體檢報告觀察「薪資 × 考核無雙向連動：`salary_engine.py` 不 import `appraisal_service`；UI 上會計看不到『這筆獎金來自 Y 考核』溯源」

## 1. 背景

園內考核制度：

- 一學年兩個 appraisal cycle（學期制；`academic_year`+`semester`）
- 每位員工每 cycle 由現有流程算出 `AppraisalSummary.bonus_amount = base_amount × (total_score / 100)`，其中 `base_amount` 來自 `AppraisalBonusRate(effective_from, role_group, grade)` 表
- 上學年下學期 + 本學年上學期兩 cycle 的 bonus 合計，於**次年 2/5 與月薪同日發放，但屬獨立支付項**

現況差距：

- `services/salary/` 與 `services/appraisal_service.py` 完全 zero cross-import
- `AppraisalSummary.bonus_amount` 算好後僅停留在 appraisal 表，**沒有任何管道流入薪資**
- 薪資 slip、Excel、月度損益、銀行轉帳名冊都看不到這筆獎金

進一步發現（重要）：園內過去某輪 milestone 已實作 `models/year_end.py`（M1–M6 commit 完整 + race condition / row lock 加固）：

- `year_end_cycles`（每 academic_year 一筆）
- `year_end_settlements`（6 層算法完整年終結算單）
- `special_bonus_items`（8 種 special bonus 的統一表）含 `SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST/SECOND`，docstring 標明「來自 appraisal_summaries」
- 但 5 個 year_end 表在 dev DB **全部 0 row**——做完從未運營
- year_end engine 也未自動從 appraisal 同步、salary engine 未讀 year_end

結論：**APPRAISAL_HALF_BONUS_FIRST/SECOND 兩個 slot 就是為此需求預留**。本 spec 走復用既有 schema 路線，補兩條橋（appraisal→year_end、year_end→salary），其餘 YearEnd 系統（22 sheets 完整經營績效、6 層算法、`year_end_settlements`、`employee_year_end_snapshot`）保留不啟用，留作未來「完整年終經營績效」擴展。

## 2. 需求摘要（已對齊）

| 項 | 決議 |
|---|---|
| 業務本質 | 考核**年終獎金**（不是月薪扣款）；現有 `AppraisalSummary.bonus_amount` 是 source |
| 發放時機 | 每年 2/5、與月薪同日、獨立支付項 |
| 取的兩 cycle | 上學年下學期（`academic_year=N-1`, `semester=SECOND`）+ 本學年上學期（`academic_year=N`, `semester=FIRST`） |
| 觸發方式 | HR 在「考核年終 payout 管理頁」手動按「生成本年考核年終」按鈕 |
| 離職員工 | HR 在生成時 checkbox 逐位選；預設只生成 ACTIVE 員工 |
| 稅務 | 不接所得稅 / 二代健保補充保費；payout 金額 = 實領；會計表外報稅 |
| 資料層 | **不建新表**——復用 `year_end_cycles` + `special_bonus_items` 兩張現有表 |
| Salary engine 接法 | Pull plugin：2 月 calculate 時 query special_bonus_items 回填 SalaryRecord 新欄；payout 表是 single source of truth |
| FIRST / SECOND 語意 | 重新解讀為「時間順序」：FIRST = 較早 = `N-1.下`；SECOND = 較晚 = `N.上`。只改 `SpecialBonusType` docstring，不動 enum value |

## 3. 架構與資料流

```
[HR 後台]
  1. 進「考核年終獎金管理」頁 → 選年份（calendar year）
  2. preview API → 後端解析兩個 appraisal cycle、各員工 summary、在職狀態、finalize 警告
  3. UI 列出名單（active 預設全勾、inactive 預設不勾）
  4. HR 確認 → POST generate
       └→ upsert YearEndCycle(academic_year = civil_year - 1911 - 1)
       └→ 對每員工 upsert 2 筆 SpecialBonusItem，bonus_type 對 period_label 的對應：
            APPRAISAL_HALF_BONUS_FIRST  ← 較早 cycle（N-1 學年 SECOND 學期，period_label="113下"）
            APPRAISAL_HALF_BONUS_SECOND ← 較晚 cycle（N 學年 FIRST 學期，period_label="114上"）
          amount = summary.bonus_amount；source_ref = "appraisal_summary:{id}"
          ⚠️ SpecialBonusType 的 FIRST/SECOND 是「時間順序」，與 AppraisalCycle 的 Semester.FIRST/SECOND（學期上下）正好相反——請避免混淆

[Salary engine — 2 月 calculate]
  5. _apply_breakdown_to_record_locked() 多 1 行：
       salary_record.appraisal_year_end_bonus = query_appraisal_year_end_bonus(db, emp_id, year, month)
     query：if month != 2: 0；else SUM(special_bonus_items.amount) WHERE cycle.academic_year = target ∧ employee_id=X ∧ bonus_type IN (FIRST,SECOND)

[Salary slip / Excel / monthly P&L]
  6. PDF slip 多一行「考核年終獎金」+ cell-link 開 dialog 顯示 113下 X + 114上 Y + cycle link
  7. SalaryListView Excel 增一欄（2 月以外 0）
  8. monthly_pnl_report 既有 personnel cost 聚合自動吸收（依賴 SUM(salary_records.*) 的部分天然納入）
```

**核心原則**：`special_bonus_items` 是 source of truth；`SalaryRecord.appraisal_year_end_bonus` 是每月 calculate 時刷新的 cache，**不進 `gross_salary`**（與既有 `bonus_amount=festival+overtime+supervisor_dividend` 同樣「獨立轉帳、不影響勞健保 / 應發合計 / 扣款」語意）。

不做的事：
- 不啟用 `year_end_settlements` 6 層算法（`avg_performance_rate` / `org_achievement_rate` / `proration_rate` / `festival_total` / `deduction_*`）
- 不啟用 `employee_year_end_snapshot`
- 不引入正式 plugin protocol（YAGNI；新增一個 module 即可）
- 不接所得稅 / 二代健保 hook
- 不做「payout 已 paid 後追溯」UI flow

## 4. Database schema

### 4.1 既有表（不動 schema、僅復用）

```
year_end_cycles
  ├─ id (PK)
  ├─ academic_year (UNIQUE)
  └─ ... (本 spec 只用 academic_year + status；其他欄位保留)

special_bonus_items
  ├─ id (PK)
  ├─ year_end_cycle_id (FK)
  ├─ employee_id (FK)
  ├─ bonus_type (ENUM SpecialBonusType)
  ├─ period_label (e.g., "113下", "114上")
  ├─ amount (Numeric(10,2))
  ├─ source_ref (e.g., "appraisal_summary:42")
  ├─ calc_meta (JSONB) — {cycle_not_finalized: bool, summary_status, snapshot_at}
  └─ UNIQUE(year_end_cycle_id, employee_id, bonus_type, period_label)
```

### 4.2 唯一 schema 改動

**新增 column** `salary_records.appraisal_year_end_bonus`：

```python
appraisal_year_end_bonus = Column(
    Money, default=0,
    comment="考核年終獎金（2/5 與月薪同發，自 special_bonus_items 兩筆 SUM；不進 gross_salary）"
)
```

**修改 docstring**（不是 schema change）`models/year_end.py SpecialBonusType`：

```diff
- APPRAISAL_HALF_BONUS_FIRST  : 113上(或 N 上)考核獎金 — 來自 appraisal_summaries
- APPRAISAL_HALF_BONUS_SECOND : 113下(或 N 下)考核獎金 — 來自 appraisal_summaries
+ APPRAISAL_HALF_BONUS_FIRST  : 較早那一筆（年終發放時對應「上學年下學期 = N-1.下」）— 來自 appraisal_summaries
+ APPRAISAL_HALF_BONUS_SECOND : 較晚那一筆（年終發放時對應「本學年上學期 = N.上」）— 來自 appraisal_summaries
+ 由 services/year_end/appraisal_sync.py 依 calendar payout year 自動 map 進 period_label。
```

### 4.3 Migration

一支 migration `<rev>_add_appraisal_year_end_bonus_to_salary_records.py`：

- up：`ALTER TABLE salary_records ADD COLUMN appraisal_year_end_bonus NUMERIC(10,2) NOT NULL DEFAULT 0`
- down：`ALTER TABLE salary_records DROP COLUMN appraisal_year_end_bonus`
- 沿用 head：`20260521_3be2e40aaa42_merge_parlsr_recurr.py`（需要按 plan 開工時的最新 head 重新對齊）

## 5. Backend services & API

### 5.1 新檔 `services/year_end/appraisal_sync.py`

純函式 + thin DB writer：

```python
def resolve_target_cycles(payout_year: int) -> tuple[AppraisalCycle, AppraisalCycle]:
    """payout_year (civil 2026) → (earlier_cycle, later_cycle)
    
    Mapping（注意 academic semester 與時間順序「反向」）：
      civil_year N → target_academic_year = N - 1911 - 1  (e.g., 2026 → 114)
      earlier_cycle = AppraisalCycle(academic_year=N-1, semester=SECOND)  # 113 學年下學期 = 2025 春
      later_cycle   = AppraisalCycle(academic_year=N,   semester=FIRST)   # 114 學年上學期 = 2025 秋
    
    回傳順序 (earlier, later) 對應 SpecialBonusType (FIRST, SECOND)。
    """

def preview_payout(db, payout_year: int) -> list[PayoutPreviewRow]:
    """為兩個 cycle 的所有 participants 算金額 snapshot，回傳：
      employee_id, employee_name, role_group,
      earlier_summary_id?, earlier_amount, earlier_cycle_finalized,
      later_summary_id?, later_amount, later_cycle_finalized,
      total_amount, is_inactive, warnings: list[str]
    
    is_excluded=true 的 participant 不列出。
    某員工只在一個 cycle 出現 → 另一筆 0、warning 標 "not_participated_in_earlier" 或 "not_participated_in_later"。
    """

def generate_payouts(
    db,
    payout_year: int,
    included_inactive_employee_ids: set[int],
    generated_by: int,
) -> GenerateResult:
    """transactional + advisory lock:
      pg_advisory_xact_lock(hash('aye_payout', payout_year))
    
    步驟：
      1. upsert YearEndCycle(academic_year = civil_year - 1911 - 1)
      2. 對每位 ACTIVE 員工 + included_inactive 中 inactive 員工：
         - 對 FIRST (113下) 與 SECOND (114上) 各 upsert SpecialBonusItem
           (year_end_cycle_id, employee_id, bonus_type, period_label, amount,
            source_ref=f"appraisal_summary:{summary_id}",
            calc_meta={"cycle_not_finalized": bool, "summary_status": str,
                       "snapshot_at": now()})
      3. 寫 audit_log(event_type="appraisal_year_end_payout.generate", payload=summary)
    
    idempotent：UniqueConstraint (year_end_cycle_id, employee_id, bonus_type, period_label) 
    + ON CONFLICT DO UPDATE SET amount = EXCLUDED.amount, calc_meta = ...
    """

def void_payouts(db, payout_year: int, voided_by: int) -> int:
    """刪除 academic_year=target 的所有 APPRAISAL_HALF_BONUS_* items
    （不刪 cycle 本身、不影響 SEMESTER_DIVIDEND 等其他 special_bonus_items）
    回傳刪除筆數，寫 audit。
    """
```

純函式 + 純查詢分離（純函式不接 db、僅做 mapping 與商業規則）：

```python
# 純函式（單元測試重點）
def civil_year_to_target_academic_year(civil_year: int) -> int: ...
def map_period_label(bonus_type: SpecialBonusType, target_academic_year: int) -> str: ...
def build_calc_meta(summary: AppraisalSummary | None) -> dict: ...
```

### 5.2 新 router `api/year_end/appraisal_payout.py`

掛在 `/year-end-payout` prefix（不要塞進既有 `api/year_end/__init__.py`，分檔比較好維護）：

| Method | Path | Body / Query | Permission | Return |
|---|---|---|---|---|
| GET | `/year-end-payout/preview` | `?year=2026` | `APPRAISAL_FINALIZE` | `list[PayoutPreviewRow]` |
| POST | `/year-end-payout/generate` | `{ year, included_inactive_employee_ids: int[] }` | `APPRAISAL_FINALIZE` | `GenerateResult` |
| GET | `/year-end-payout` | `?year=2026` | `APPRAISAL_FINALIZE` | `list[PayoutItem]`（已生成 special_bonus_items by year） |
| DELETE | `/year-end-payout/{year}` | — | `APPRAISAL_FINALIZE` + 強制 `confirm=true` query | `{ deleted_count: int }` |

全部走既有 `request.state.audit_*` middleware，event_type prefix `appraisal_year_end_payout.*`。

`Pydantic schemas` 用既有 `schemas/year_end.py` 加 4 個 model（PayoutPreviewRow / PayoutItem / GenerateRequest / GenerateResult），全部標 `response_model=` 讓 OpenAPI 對前端落地 TS 型別。

### 5.3 Salary engine plugin

新檔 `services/salary/appraisal_year_end.py`（薄薄一層 query 函式）：

```python
def query_appraisal_year_end_bonus(
    db, employee_id: int, year: int, month: int
) -> Decimal:
    """2 月份 query special_bonus_items 兩筆 APPRAISAL_HALF_BONUS_* 的 SUM。
    其他月份 return 0。
    
    target_academic_year = year - 1911 - 1
    例：year=2026 → 114；2/5 發的兩筆掛在 year_end_cycles(academic_year=114) 之下。
    """
    if month != 2:
        return Decimal(0)
    target_academic_year = year - 1911 - 1
    result = db.query(func.coalesce(func.sum(SpecialBonusItem.amount), 0)).join(
        YearEndCycle, YearEndCycle.id == SpecialBonusItem.year_end_cycle_id
    ).filter(
        YearEndCycle.academic_year == target_academic_year,
        SpecialBonusItem.employee_id == employee_id,
        SpecialBonusItem.bonus_type.in_([
            SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
            SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
        ]),
    ).scalar()
    return Decimal(result or 0)
```

在 `services/salary/engine.py` `_apply_breakdown_to_record_locked()` 加 1 行：

```python
salary_record.appraisal_year_end_bonus = query_appraisal_year_end_bonus(
    db, salary_record.employee_id, salary_record.year, salary_record.month
)
```

**不進 `gross_salary`、不進 `bonus_amount`**（後者是 festival+overtime+supervisor_dividend aggregate）。

## 6. Frontend

### 6.1 新管理頁

`src/views/yearEnd/AppraisalPayoutView.vue`（與既有 `YearEndListView.vue` / `YearEndDetailView.vue` 同層）：

UI 結構（mockup 已在 brainstorm Section 4）：
- 頂部：年份切換 + 兩 cycle finalize 狀態指示 + warning banner（未 finalized 時）
- 中段：員工 preview 名單表格（columns：勾選 / 員工 / 113下金額（earlier）/ 114上金額（later）/ 合計 / 在職狀態 / warnings）
  - ACTIVE 員工：預設勾選
  - INACTIVE 員工：預設**不**勾、標紅、HR 主動勾才會帶入 generate
  - 只在單一 cycle 有 summary 的員工：另一邊金額 0、warning 「未參與 113 下 cycle」
- 底部：「確認生成 N 筆 payout（合計 NT$X）」+ 「清空本年 payout」（admin、需 confirm dialog 兩次）
- 已生成分頁：列出當年所有 special_bonus_items（FIRST + SECOND）+ source_ref + 跳轉 link 到 appraisal cycle

### 6.2 薪資 slip 整合

`src/views/SalaryView.vue`：
1. 月度列表「淨額」公式（line 685 附近）加 `+ (scope.row.appraisal_year_end_bonus || 0)`
2. 表格加一欄「考核年終獎金」（cell-link 開 breakdown dialog）
3. Breakdown dialog 顯示「113下 X + 114上 Y = 合計 Z」+ 兩個「查看 cycle」link 跳 `/appraisal/cycles/{id}`

`src/views/yearEnd/`（若有 slip PDF 預覽元件）：同步加欄

### 6.3 Router / Sidebar

`AdminSidebar.vue`：既有「年終獎金」項目下加子項「考核年終 payout」，path `/year-end/appraisal-payout`，permission gate `APPRAISAL_FINALIZE`。

`router/index.ts`：加 route `/year-end/appraisal-payout` → `AppraisalPayoutView.vue`。

### 6.4 API wrapper

`src/api/yearEnd.ts` 加 4 函式，全部用 `AxiosResp<'/year-end-payout/...', '...'>` typed return（需要先後端跑 `dump_openapi.py` + 前端 `npm run gen:api` 同步 schema.d.ts）：

```ts
export const appraisalPayoutPreview = (year: number) => api.get<...>('/year-end-payout/preview', { params: { year } })
export const appraisalPayoutList = (year: number) => api.get<...>('/year-end-payout', { params: { year } })
export const appraisalPayoutGenerate = (year: number, included_inactive_employee_ids: number[]) =>
  api.post<...>('/year-end-payout/generate', { year, included_inactive_employee_ids })
export const appraisalPayoutVoid = (year: number) =>
  api.delete<...>(`/year-end-payout/${year}`, { params: { confirm: true } })
```

## 7. Edge cases

| # | 情境 | 處理 |
|---|---|---|
| 1 | 未 finalize cycle 時生成 | 仍寫 special_bonus_item（amount = summary.bonus_amount），`calc_meta.cycle_not_finalized=true`；UI 顯示警告；HR 後續修 summary 後**不自動同步**（須重 generate） |
| 2 | `AppraisalParticipant.is_excluded=true` | preview 不列、generate 不寫 |
| 3 | 員工只在一個 cycle 出現 | 另一筆 amount=0、period_label 標 `"not_participated"`、warning |
| 4 | 重複生成同年 | ON CONFLICT DO UPDATE：idempotent；不會多寫；非 APPRAISAL_HALF 的 special_bonus_items 不動 |
| 5 | payout 已 paid 後改 appraisal | 不自動回算。HR 須先 unfinalize salary record + 重 generate payout + 重 calculate 薪資。UI 在管理頁 hint |
| 6 | Race condition（同時兩個 admin 按生成） | `pg_advisory_xact_lock(hash('aye_payout', year))` 包整個 transaction |
| 7 | `academic_year` mapping | civil 2026 → roc 114（不是 115）；單元測試 5 條 case 鎖定 |
| 8 | salary engine 非 2 月 query | return 0；測試覆蓋 jan / mar / dec |
| 9 | salary engine recalculate | 每次 calculate 重 query payout 表（不 cache）；改 payout 後 recalculate 自動更新 |
| 10 | 權限不足 | 4 endpoint 全部 `APPRAISAL_FINALIZE` 守衛，non-admin 應 403 |

## 8. 測試策略

| 層 | 測試檔 | 涵蓋 |
|---|---|---|
| Backend service | `tests/test_year_end_appraisal_sync.py` | resolve_target_cycles / preview / generate idempotent / void / academic_year mapping（5 case）/ 純函式 unit |
| Backend API | `tests/test_year_end_appraisal_payout_router.py` | 4 endpoint × 權限 / happy / 422（缺 year）/ 409（並行）/ confirm=true 守衛 |
| Salary engine integration | `tests/test_salary_appraisal_year_end_plugin.py` | 2 月 query / 非 2 月 0 / 重算回拉新 amount / SalaryRecord 寫入 / generate→calculate→改 payout→recalc 鏈 |
| Frontend (component) | `__tests__/AppraisalPayoutView.spec.ts` | preview render / inactive 勾選互動 / generate 後跳分頁 / 警告顯示 / confirm dialog |
| Frontend (salary slip) | `__tests__/SalaryView.appraisal-year-end-bonus.spec.ts` | 2 月列顯示金額 / breakdown dialog / 點 cycle link 跳轉 |

關鍵測試案例（必須有）：
- `test_civil_year_to_target_academic_year`：2026→114、2025→113、2027→115 等 5 case
- `test_generate_idempotent`：連按兩次只有一份 special_bonus_items
- `test_generate_includes_inactive_only_when_selected`
- `test_query_appraisal_year_end_bonus_february_only`
- `test_salary_recalculate_after_payout_void`：void → recalculate → amount = 0

## 9. PII & Audit

- 金額屬個資（與 salary 同等），既有 Sentry PII denylist（`utils/sentry_init._PII_KEY_SUBSTRINGS`）已含 `amount` / `salary` / `bonus`，**不需新增**
- audit_log event_type prefix `appraisal_year_end_payout.{preview,generate,void}`，payload 含 `year` / `affected_employee_ids` / `total_amount` / `generated_by`

## 10. 部署 & rollout

1. Migration 套用（dev → staging → prod）
2. 部署後端 `appraisal_sync` + `appraisal_payout` router + salary engine 1 行
3. 部署前端（含 schema.d.ts regen）
4. **首次運營**（HR 教育 + 第一年生成）：
   - 確認兩個 cycle 都 finalized
   - 進管理頁預覽、確認金額無誤
   - 確認 inactive 名單勾選正確
   - 按生成 → 確認 special_bonus_items 寫入
   - 2 月薪資 calculate → 確認 `SalaryRecord.appraisal_year_end_bonus` 寫入
   - 確認 slip / Excel / 月度損益顯示正確
5. CLAUDE.md（workspace）加一段 cross-system 提醒：salary 2/5 含 appraisal year-end + generate 流程

## 11. 影響檔案總覽

### 後端

| 檔 | 動作 |
|---|---|
| `alembic/versions/<rev>_add_appraisal_year_end_bonus_to_salary_records.py` | new |
| `models/salary.py` | 加 1 column |
| `models/year_end.py` | 修 `SpecialBonusType` docstring |
| `services/year_end/appraisal_sync.py` | new |
| `services/salary/appraisal_year_end.py` | new |
| `services/salary/engine.py` | 加 1 行（_apply_breakdown_to_record_locked） |
| `api/year_end/appraisal_payout.py` | new（4 endpoint） |
| `api/year_end/__init__.py` | include new router |
| `schemas/year_end.py` | 加 4 model |
| `tests/test_year_end_appraisal_sync.py` | new |
| `tests/test_year_end_appraisal_payout_router.py` | new |
| `tests/test_salary_appraisal_year_end_plugin.py` | new |

### 前端

| 檔 | 動作 |
|---|---|
| `src/api/yearEnd.ts` | 加 4 wrapper |
| `src/api/_generated/schema.d.ts` | regen via `npm run gen:api` |
| `src/views/yearEnd/AppraisalPayoutView.vue` | new |
| `src/views/SalaryView.vue` | 加 1 列 + breakdown dialog + 淨額公式 |
| `src/components/layout/AdminSidebar.vue` | 加 1 子項 |
| `src/router/index.ts` | 加 1 route |
| `src/views/yearEnd/__tests__/AppraisalPayoutView.spec.ts` | new |
| `src/views/__tests__/SalaryView.appraisal-year-end-bonus.spec.ts` | new |

### Workspace

| 檔 | 動作 |
|---|---|
| `CLAUDE.md`（workspace 根） | 加一段「跨端常見陷阱」：salary 2/5 含 appraisal year-end / 生成流程 / academic_year mapping |

### Commit 拆分（依本 workspace CLAUDE.md「跨前後端 SOP」）

- 後端 ~4 commit：(1) migration + model column (2) appraisal_sync service + tests (3) salary engine plugin + tests (4) API router + tests + audit + docstring 修
- 前端 ~3 commit：(1) schema.d.ts regen + api wrapper (2) AppraisalPayoutView + sidebar + router (3) SalaryView 整合 + tests

## 12. 不在本 spec 範圍

- 完整 YearEndSettlement 6 層算法啟用（保留作為「完整年終經營績效」未來擴展）
- 其他 SpecialBonusType（SEMESTER_DIVIDEND_FIRST/SECOND、AFTER_CLASS_AWARD、TEACHING_EXTRA、EXCESS_ENROLLMENT、FESTIVAL_DIFF）
- 所得稅扣繳 / 二代健保補充保費自動計算
- payout 已 paid 後追溯（不做 UI flow）
- 薪資 slip PDF 字型 / 排版調整（slip 結構自動繼承新 column）

## 13. 未決 / Follow-up

- (low) `period_label` 顯示格式：考慮「113-2」「113下」何者較直觀，brainstorm 暫定「113下」
- (low) preview API 是否要支援 dry-run reuse（先呼叫 preview、再用相同 payload 呼叫 generate 確保金額一致）—— V1 直接 generate 內部重 preview 一次即可
- (medium) 未來若 user 想啟用完整 YearEndSettlement 6 層算法：本 spec 寫入的 special_bonus_items 會被 step6 `SUM(special_bonus_items.amount)` 自動聚合進 `total_amount`，無相容性問題
