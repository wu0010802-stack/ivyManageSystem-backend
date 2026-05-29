# SPEC-006：年終考核獎金

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | services/salary/appraisal_year_end.py; services/{appraisal,year_end}/; api/{appraisal,year_end}/; models/{appraisal,year_end}.py |
| Related | SPEC-001（engine 主流程，`appraisal_year_end_bonus` 不進 `gross_salary`）；`docs/superpowers/specs/2026-05-22-salary-appraisal-year-end-payout-design.md`（年終考核獎金 payout 設計規格） |

## Overview

「年終考核獎金（appraisal year-end bonus）」是把上半年/下半年考核（`AppraisalSummary.bonus_amount`）累計後，於每年 2 月隨月薪發放的獨立金額。整條流水線跨四個子系統：

1. **半年考核（`services/appraisal/`、`api/appraisal/`、`models/appraisal.py`）**
   每個半年 cycle 內，主任打分 → 自動 sync 14 條 score_items → 引擎 5-step 算出 `AppraisalSummary.total_score` + `grade` + `bonus_amount`，三階簽核（SUPERVISOR → ACCOUNTING → FINALIZE）。

2. **年終 payout 同步（`services/year_end/appraisal_sync.py`、`api/year_end/appraisal_payout.py`）**
   HR 在每年 2 月發放前手動 trigger `POST /api/year_end/appraisal-payout/generate`，把目標學年的兩個 cycle（前一學年下學期 + 本學年上學期）的 `AppraisalSummary.bonus_amount` 寫入 `special_bonus_items` 表，type 分別為 `APPRAISAL_HALF_BONUS_FIRST`（時間順序較早 = 前一學年下學期）與 `APPRAISAL_HALF_BONUS_SECOND`（時間順序較晚 = 本學年上學期）。

3. **薪資 engine 拉取（`services/salary/appraisal_year_end.py`、`services/salary/engine.py`）**
   月薪 calculate 時呼叫 `_fill_salary_record(session=...)`，當 `salary_month == 2` 時 plugin `query_appraisal_year_end_bonus()` 從 `special_bonus_items` SUM 兩筆 `APPRAISAL_HALF_BONUS_*`，寫入 `SalaryRecord.appraisal_year_end_bonus` 獨立欄位。

4. **年終獎金結算（另一條獨立 6-step 計算，`services/year_end/engine.py`、其餘 `api/year_end/`）**
   與本 SPEC 主題（appraisal year-end bonus）共享 `YearEndCycle` 與 `SpecialBonusItem` 表，但是另一個業務流（每員工算 `YearEndSettlement`）。本 SPEC 不展開該流程，僅紀錄其 endpoints 與 schemas 與 appraisal payout 共用基礎設施。

關鍵不變式：`appraisal_year_end_bonus` 是 `SalaryRecord` 上的 **獨立欄位**，**不進** `gross_salary` / `total_deduction` / `net_salary`，也不影響勞健保計算。

---

## Interface Definitions

### 內部 Python 函式

#### `services/salary/appraisal_year_end.py`（43 行）

- `query_appraisal_year_end_bonus(db: Session, employee_id: int, year: int, month: int) -> Decimal`
  - 入口；薪資 engine 唯一呼叫點。
  - `month != 2` → 直接回 `Decimal(0)`。
  - `month == 2` → 以 `target_academic_year = civil_year_to_target_academic_year(year)`（`year - 1911 - 1`，例：2026 國曆 → 民國 114 學年）找 `YearEndCycle`，SUM `special_bonus_items.amount` where `bonus_type IN (APPRAISAL_HALF_BONUS_FIRST, APPRAISAL_HALF_BONUS_SECOND)` AND `employee_id = ?`。

#### `services/year_end/appraisal_sync.py`

- `civil_year_to_target_academic_year(civil_year: int) -> int`
  - 國曆年 N → 對應本學年（民國）= `N - 1911 - 1`。`2026 → 114`（學年 8 月起算，2 月 5 日發 = 114 學年下學期）。
- `map_bonus_type_to_period_label(bonus_type: SpecialBonusType, target_academic_year: int) -> str`
  - `APPRAISAL_HALF_BONUS_FIRST` → `f"{target_academic_year - 1}下"`（前一學年下學期）。
  - `APPRAISAL_HALF_BONUS_SECOND` → `f"{target_academic_year}上"`（本學年上學期）。
  - 其他 `bonus_type` → `ValueError`。
- `resolve_target_cycles(db, payout_year: int) -> tuple[AppraisalCycle, AppraisalCycle]`
  - 由 `payout_year` 解析「前一學年下學期」+「本學年上學期」兩個 `AppraisalCycle`。任一不存在 → `LookupError`。
- `preview_payout(db, payout_year: int) -> list[PayoutPreviewRow]`
  - 為兩 cycle 的 participants 算金額 snapshot。`is_excluded=True` 不列出；只在一個 cycle 出現 → 另一筆為 0 加 warning（`not_participated_in_earlier` / `not_participated_in_later`）；兩 cycle 都沒參與 → 不列出。Summary 非 `FINALIZED` 額外加 warning（`earlier_summary_not_finalized` / `later_summary_not_finalized`）。
- `generate_payouts(db, payout_year, included_inactive_employee_ids, generated_by) -> GenerateResult`
  - 交易式：upsert `YearEndCycle`（不存在則自動建立最小 shell，`start_date=N/8/1`、`end_date=(N+1)/7/31`、`bonus_calc_date=payout_year/1/15`），再對每員工 upsert 兩筆 `SpecialBonusItem`（key = `(year_end_cycle_id, employee_id, bonus_type, period_label)`，由 `_upsert_special_bonus_item` 處理）。ACTIVE 員工預設全寫；INACTIVE 員工必須在 `included_inactive_employee_ids` 集合內才寫，否則 `skipped_inactive_count += 1`。PostgreSQL 環境以 `pg_advisory_xact_lock` (`hashlib.md5("aye_payout|{payout_year}").digest()` 取前 8 bytes 作為 lock key) 防並行 generate。SQLite 測試環境 advisory lock no-op。本函式僅 `db.flush()`，呼叫端負責 `commit`。
- `void_payouts(db, payout_year, voided_by) -> int`
  - 刪除目標 academic year 下所有 `APPRAISAL_HALF_BONUS_FIRST` / `APPRAISAL_HALF_BONUS_SECOND` items，回傳刪除筆數；不動其他 `SpecialBonusType`；`voided_by` 由 router 層 audit middleware 處理，函式內僅接受但不直接寫 DB。

#### `services/salary/engine.py`（`_fill_salary_record` 內 appraisal 區塊）

```python
# engine.py L194-203
# 考核年終獎金（2 月發放；不進 gross_salary；source of truth = special_bonus_items）
if session is not None:
    salary_record.appraisal_year_end_bonus = query_appraisal_year_end_bonus(
        session,
        salary_record.employee_id,
        salary_record.salary_year,
        salary_record.salary_month,
    )
    # Layer 2：撈 scheduler 已寫入但尚未綁定到本 SalaryRecord 的 pending log
    _pull_pending_payout_logs(session, salary_record)
```

- `_fill_salary_record(salary_record, breakdown, engine, session=None)`：當 `session is None` 時跳過 plugin（向下相容既有不接 DB 的 unit test）；當 `session` 提供時，**無條件** 呼叫 `query_appraisal_year_end_bonus()`（plugin 內自行檢查 `month == 2`）。

#### `services/appraisal/` package（半年考核計算引擎；M2 重寫）

- `engine.py`：
  - `class BonusRateLookup`：`(effective_from, role_group, grade) → base_amount` 三維索引。
  - `class SummaryComputed`：考核 5-step 計算結果 DTO（`base_score`、`event_score_sum`、`total_score`、`grade`、`bonus_amount`）。
  - `compute_base_score(...)`、`sum_score_items(...)`、`compute_total_score(...)`、`classify_grade(...)`、`compute_bonus_amount(...)`、`compute_summary(...)`：純函式，無 DB 依賴。
  - `proration_rate(hire_months_in_cycle) -> Decimal`：到職比例（半年最多 6 月）。
- `excel_io.py`：`parse_half_year_excel(path)` / `import_half_year_to_db(...)` / `export_half_year_xlsx(...)` / `export_transfer_roster_xlsx(rows)`。
- `rule_applier.py`：`compute_all_deltas(session, cycle) -> dict[(participant_id, item_code), DeltaResult]`；14 條 auto item delta 即時計算。
- `sign_workflow.py`：`can_advance` / `advance_target` / `can_reject` / `default_reject_to_status` / `write_summary_log` / `clear_rejection_state` / `apply_reject`。
- `status_aggregator.py`：`aggregate_cycle_status(session, cycle)`、`aggregate_all_active_employees_status(session, cycle)`，回 `list[ParticipantStatus]`。
- `employee_inference.py`：`infer_role_group(employee)` / `infer_classroom_id(employee)`。

#### `services/year_end/` package（年終獎金 6-step 計算引擎；非本 SPEC 主流，僅列必要 surface）

- `engine.py`：`compute_settlement(...)` 等 6-step 純函式。
- `excel_io.py`：`parse_year_end_excel(path)` / `import_year_end_to_db(...)` / `export_year_end_summary_xlsx(...)` / `export_year_end_transfer_xlsx(...)`。
- `print_pdf.py`：`generate_personal_bonus_slip_pdf(...)` / `generate_transfer_roster_pdf(...)` / `generate_summary_table_pdf(...)`。

#### 空殼模組

- `services/appraisal_service.py`、`services/appraisal_excel.py`：M1 重構後皆為空殼（docstring 註明 CRUD/Excel I/O 已搬至 `services/appraisal/` package）。

---

### HTTP 端點

#### `api/year_end/appraisal_payout.py`（核心，本 SPEC 主軸）

router 掛載點：`/api/year_end/appraisal-payout`（由 `api/year_end/__init__.py` `include_router` 進 `year_end_router`，最終 path 為 `/api/year_end/appraisal-payout/*`）。

| 端點 | 描述 |
|------|------|
| Function | `GET /api/year_end/appraisal-payout/preview` |
| Permission | `APPRAISAL_FINALIZE`（`1 << 59`） |
| Request | Query: `year: int ge=2024 le=2099` |
| Response | `list[PayoutPreviewRow]` |
| 行為 | 呼叫 `preview_payout(session, payout_year=year)` |

| 端點 | 描述 |
|------|------|
| Function | `POST /api/year_end/appraisal-payout/generate` |
| Permission | `APPRAISAL_FINALIZE`（`1 << 59`） |
| Request | Body: `PayoutGenerateRequest`（`year`, `included_inactive_employee_ids: list[int]`） |
| Response | `PayoutGenerateResult` |
| 行為 | `generate_payouts(..., generated_by=current_user.user_id)` + `session.commit()` |

| 端點 | 描述 |
|------|------|
| Function | `GET /api/year_end/appraisal-payout` |
| Permission | `APPRAISAL_FINALIZE`（`1 << 59`） |
| Request | Query: `year: int ge=2024 le=2099` |
| Response | `list[PayoutItem]` |
| 行為 | 列出對應學年 cycle 下所有 `APPRAISAL_HALF_BONUS_*` items。Cycle 不存在 → `[]`。 |

| 端點 | 描述 |
|------|------|
| Function | `DELETE /api/year_end/appraisal-payout/{year}` |
| Permission | `APPRAISAL_FINALIZE`（`1 << 59`） |
| Request | Path: `year`；Query: `confirm: bool`（必須 `=true`） |
| Response | `{"deleted_count": int}` |
| 行為 | `confirm=false` → 400；否則 `void_payouts(..., voided_by=user)` + commit |

#### `api/appraisal/` 主要端點（半年考核 + 上游簽核）

router prefix：`/api/appraisal`。

| 端點 | 權限 | 用途 |
|------|------|------|
| `GET /api/appraisal/cycles` | `APPRAISAL_READ` (`1<<55`) | 列出 cycle |
| `GET /api/appraisal/current` | `APPRAISAL_READ` | 取當前學期 cycle（`resolve_current_academic_term`） |
| `GET /api/appraisal/by_year/{academic_year}` | `APPRAISAL_READ` | 取某學年所有 cycle |
| `GET /api/appraisal/cycles/{cycle_id}/aggregated_status` | `APPRAISAL_READ` | 彙整 participant 的四指標（不寫 DB） |
| `GET /api/appraisal/cycles/{cycle_id}/all_employees_status` | `APPRAISAL_READ` | 含未加入考核員工 |
| `POST /api/appraisal/cycles` | `APPRAISAL_FINALIZE` (`1<<59`) | 建 cycle |
| `PATCH /api/appraisal/cycles/{cycle_id}` | `APPRAISAL_FINALIZE` | 更新 cycle；非 OPEN 不可改 base_score / enrollment_* |
| `GET /api/appraisal/catalog` | `APPRAISAL_READ` | 16 項加減分 catalog |
| `GET /api/appraisal/cycles/{cycle_id}/participants` | `APPRAISAL_READ` | 列出 participants |
| `POST /api/appraisal/cycles/{cycle_id}/participants` | `APPRAISAL_EVENT_WRITE` (`1<<56`) | 加 participant |
| `POST /api/appraisal/cycles/{cycle_id}/participants:bulk_from_active` | `APPRAISAL_EVENT_WRITE` | 把在職員工批次加入；非 OPEN cycle 拒 |
| `GET /api/appraisal/participants/{participant_id}/score_items` | `APPRAISAL_READ` | 列出 score items |
| `POST /api/appraisal/participants/{participant_id}/score_items` | `APPRAISAL_EVENT_WRITE` | 加 score item |
| `POST /api/appraisal/cycles/{cycle_id}/sync_score_items` | `APPRAISAL_EVENT_WRITE` | 把 14 項 auto delta 同步寫入；非 OPEN 拒；`dry_run=true` 不寫 |
| `GET /api/appraisal/cycles/{cycle_id}/summaries` | `APPRAISAL_READ` | 列出 summaries |
| `POST /api/appraisal/cycles/{cycle_id}/summaries:recompute` | `APPRAISAL_EVENT_WRITE` | 重算全 cycle summary；非 OPEN 拒；`FINALIZED` summary 不覆寫 |
| `POST /api/appraisal/summaries/{summary_id}/sign_supervisor` | `APPRAISAL_REVIEW` (`1<<57`) | 主管簽；自簽防呆；非 OPEN cycle 拒 |
| `POST /api/appraisal/summaries/{summary_id}/sign_accounting` | `APPRAISAL_ACCOUNTING` (`1<<58`) | 會計簽 |
| `POST /api/appraisal/summaries/{summary_id}/finalize` | `APPRAISAL_FINALIZE` | 核定 |
| `POST /api/appraisal/summaries/{summary_id}/reject` | `APPRAISAL_READ` + 二次驗對應 stage 權限 | 退簽；DRAFT 拒；with_for_update timing 改為「先 perm check 再持鎖」（bug sweep 2026-05-18 P2） |
| `POST /api/appraisal/summaries/{summary_id}/comment` | `APPRAISAL_READ` + 自簽防呆 | 留言 |
| `POST /api/appraisal/cycles/{cycle_id}/summaries:batch_sign` | `APPRAISAL_READ` + 二次驗 stage 權限 | 批次簽核；每筆獨立 savepoint，失敗只記入 failed |
| `GET /api/appraisal/summaries/{summary_id}/logs` | `APPRAISAL_READ` | 簽核軌跡 log |
| `GET /api/appraisal/cycles/{cycle_id}/sign_status_summary` | `APPRAISAL_READ` | 簽核狀態聚合 + buckets |
| `GET /api/appraisal/bonus_rates` | `APPRAISAL_READ` | 列出獎金率 |
| `POST /api/appraisal/bonus_rates` | `APPRAISAL_FINALIZE` | 新建獎金率 |
| `POST /api/appraisal/cycles/import_excel` | `APPRAISAL_EVENT_WRITE` | 上傳 .xls/.xlsx 匯入 cycle |
| `GET /api/appraisal/cycles/{cycle_id}/export.xlsx` | `APPRAISAL_READ` | 匯出半年考核 Excel |
| `GET /api/appraisal/cycles/{cycle_id}/transfer_roster.xlsx` | `APPRAISAL_READ` | 匯出轉帳名冊（bonus>0 員工） |
| `GET /api/appraisal/scoring_rules` | `APPRAISAL_READ` | 列出規則（指定日期當前有效，每 code 取最新版） |
| `GET /api/appraisal/scoring_rules/history` | `APPRAISAL_READ` | 單一 item_code 版本歷史 |
| `POST /api/appraisal/scoring_rules` | `APPRAISAL_RULE_WRITE` (`1<<53`) | 建新版規則；`effective_from` 不可早於 `today_taipei()` |
| `GET /api/appraisal/cycles/{cycle_id}/manual_event_counts` | `APPRAISAL_READ` | 列出已填手填事件次數 |
| `PUT /api/appraisal/cycles/{cycle_id}/manual_event_counts:batch` | `APPRAISAL_EVENT_WRITE` | Batch UPSERT 手填次數；非 OPEN 拒 |
| `POST /api/appraisal/cycles/{cycle_id}/score_preview` | `APPRAISAL_READ` | Dry-run 算 14 條 delta + 對比 DB 標 highlight |

#### `api/year_end/__init__.py` 其他端點（非本 SPEC 核心，僅列總覽）

router prefix：`/api/year_end`。

| 端點 | 權限 |
|------|------|
| `GET /api/year_end/cycles` | `YEAR_END_READ` (`1<<52`) |
| `POST /api/year_end/cycles` | `YEAR_END_FINALIZE` (`1<<61`) |
| `GET /api/year_end/cycles/{cycle_id}/org_settings` | `YEAR_END_READ` |
| `POST /api/year_end/cycles/{cycle_id}/org_settings` | `YEAR_END_WRITE` (`1<<60`) |
| `GET /api/year_end/cycles/{cycle_id}/class_targets` | `YEAR_END_READ` |
| `GET /api/year_end/cycles/{cycle_id}/settlements` | `YEAR_END_READ` |
| `POST /api/year_end/settlements/{settlement_id}/sign_supervisor` | `APPRAISAL_REVIEW` |
| `POST /api/year_end/settlements/{settlement_id}/sign_accounting` | `APPRAISAL_ACCOUNTING` |
| `POST /api/year_end/settlements/{settlement_id}/finalize` | `YEAR_END_FINALIZE` |
| `GET /api/year_end/cycles/{cycle_id}/special_bonuses` | `YEAR_END_READ` |
| `POST /api/year_end/cycles/{cycle_id}/special_bonuses` | `YEAR_END_WRITE` |
| `POST /api/year_end/cycles/import_excel` | `YEAR_END_WRITE` |
| `GET /api/year_end/cycles/{cycle_id}/summary.xlsx` | `YEAR_END_READ` |
| `GET /api/year_end/cycles/{cycle_id}/transfer_roster.xlsx` | `YEAR_END_READ` |
| `GET /api/year_end/cycles/{cycle_id}/settlements/{settlement_id}/slip.pdf` | `YEAR_END_READ` |
| `GET /api/year_end/cycles/{cycle_id}/transfer_roster.pdf` | `YEAR_END_READ` |
| `GET /api/year_end/cycles/{cycle_id}/summary.pdf` | `YEAR_END_READ` |

---

## DTO Definitions

### `SalaryRecord.appraisal_year_end_bonus`（`models/salary.py` L250-256）

```python
appraisal_year_end_bonus = Column(
    Money,
    default=0,
    nullable=False,
    server_default="0",
    comment="考核年終獎金（2/5 與月薪同發；自 special_bonus_items 兩筆 APPRAISAL_HALF_BONUS_* SUM；不進 gross_salary）",
)
```

Alembic migration：`alembic/versions/20260522_ayebsr1_add_appraisal_year_end_bonus_to_salary_records.py`。

### `appraisal_summaries` 表（`models/appraisal.py` L386-461，`class AppraisalSummary`）

| 欄位 | 型別 | 註解 |
|------|------|------|
| `id` | BigInteger PK | |
| `participant_id` | BigInteger FK→appraisal_participants | UNIQUE |
| `cycle_id` | BigInteger FK→appraisal_cycles | |
| `base_score` | Numeric(5,2) | 基礎分數 |
| `event_score_sum` | Numeric(6,2) | 加減分總和 |
| `total_score` | Numeric(6,2) | 總分 |
| `grade` | enum | OUTSTANDING/GOOD/PASS/WARN/FAIL |
| `bonus_amount` | Numeric(10,2) | **半年考核獎金額**（payout sync 拉取的源） |
| `leave_note` | String(120) | Excel 事假/病假備註欄 |
| `status` | enum | DRAFT/SUPERVISOR_SIGNED/ACCOUNTING_SIGNED/FINALIZED |
| `supervisor_signed_at` / `supervisor_signed_by` / `supervisor_comment` | | |
| `accounting_signed_at` / `accounting_signed_by` / `accounting_comment` | | |
| `finalized_at` / `finalized_by` / `finalized_comment` | | |
| `rejected_at` / `rejected_by` / `rejected_from_stage` / `rejected_reason` | | |
| `version` | Integer | 樂觀鎖版本號 |
| `created_at` / `updated_at` | DateTime(timezone=True) | |

### `special_bonus_items` 表（`models/year_end.py` L547-613，`class SpecialBonusItem`）

| 欄位 | 型別 | 註解 |
|------|------|------|
| `id` | BigInteger PK | |
| `year_end_cycle_id` | BigInteger FK→year_end_cycles ON DELETE CASCADE | |
| `employee_id` | Integer FK→employees ON DELETE RESTRICT | |
| `bonus_type` | enum `SpecialBonusType` | 9 種 + CUSTOM |
| `period_label` | String(40) default="" | 期間標籤（如 113上、114-08、114上）；upsert 鍵之一 |
| `amount` | Numeric(10,2) | 獎金金額；`FESTIVAL_DIFF` 可為負（多退） |
| `classroom_id` | Integer FK→classrooms ON DELETE SET NULL nullable | |
| `calc_meta` | JSONB | per-type 計算明細 |
| `source_ref` | String(120) nullable | 來源 Excel sheet 名或內部 ref（如 `appraisal_summary:{id}`） |
| `created_by` | Integer FK→users ON DELETE SET NULL nullable | |
| `created_at` / `updated_at` | DateTime(timezone=True) | |

UNIQUE：`(year_end_cycle_id, employee_id, bonus_type, period_label)` (`uq_special_bonus_item`)。

### `SpecialBonusType` enum（`models/year_end.py` L65-91）

| 常數 | 用途 | 出自 |
|------|------|------|
| `APPRAISAL_HALF_BONUS_FIRST` | **本 SPEC 焦點**；較早 = 前一學年下學期（N-1.下） | `models/year_end.py` L83 |
| `APPRAISAL_HALF_BONUS_SECOND` | **本 SPEC 焦點**；較晚 = 本學年上學期（N.上） | `models/year_end.py` L84 |
| `SEMESTER_DIVIDEND_FIRST` / `SEMESTER_DIVIDEND_SECOND` | 學期紅利（非本 SPEC） | |
| `AFTER_CLASS_AWARD` / `TEACHING_EXTRA` / `EXCESS_ENROLLMENT` / `FESTIVAL_DIFF` / `CUSTOM` | 其他年終特別獎金（非本 SPEC） | |

關鍵命名陷阱：`APPRAISAL_HALF_BONUS_FIRST/SECOND` 的 FIRST/SECOND 是 **時間順序**（FIRST=較早=前一學年下學期，SECOND=較晚=本學年上學期），與 `AppraisalCycle.Semester.FIRST/SECOND`（學期上下）正好 **相反**。由 `services/year_end/appraisal_sync.map_bonus_type_to_period_label()` 與 `resolve_target_cycles()` 自動 map。

### `YearEndCycle`（`models/year_end.py` L114-150）

| 欄位 | 型別 | 註解 |
|------|------|------|
| `id` | BigInteger PK | |
| `academic_year` | Integer UNIQUE | 民國學年 |
| `start_date` / `end_date` | Date | 學年 = N年8月～N+1年7月 |
| `bonus_calc_date` | Date | 結算基準日（如 1/15） |
| `status` | enum | OPEN/LOCKED/CLOSED |
| `params_snapshot` | JSONB default={} | 鎖定當下的計算參數 snapshot |
| `created_by` / `created_at` / `updated_at` | | |

### Pydantic schema

`schemas/year_end.py`（payout 段，L141-183）：

- `PayoutPreviewRow(BaseModel)`：對應 `services/year_end/appraisal_sync.PayoutPreviewRow` dataclass，含 `employee_id`、`employee_name`、`role_group`、`earlier_summary_id?`、`earlier_amount`、`earlier_cycle_finalized`、`later_summary_id?`、`later_amount`、`later_cycle_finalized`、`total_amount`、`is_inactive`、`warnings`。
- `PayoutGenerateRequest`：`year: int ge=2024 le=2099` + `included_inactive_employee_ids: list[int]`。
- `PayoutGenerateResult`：`cycle_id`、`generated_count`、`affected_employee_count`、`total_amount`、`skipped_inactive_count`、`warnings`。
- `PayoutItem`：`id`、`employee_id`、`bonus_type: str`、`period_label`、`amount`、`source_ref?`、`calc_meta: dict[str, Any]`。

`services/year_end/appraisal_sync.py` 內部 dataclass：
- `PayoutPreviewRow`（dataclass）：與 Pydantic 同名同欄位。
- `GenerateResult`（dataclass）：與 `PayoutGenerateResult` 同欄位。

---

## Business Rules

| # | 規則 | 證據出處 |
|---|------|---------|
| 1 | 拉取時點：僅 `salary_month == 2` 時 plugin 真的去 DB SUM；其他月份直接回 `Decimal(0)` | `services/salary/appraisal_year_end.py` L26-27 |
| 2 | `query_appraisal_year_end_bonus` 僅當 `session is not None` 時被呼叫；`session=None` path（舊 unit test）會跳過整段 plugin | `services/salary/engine.py` L195-201 |
| 3 | 半年範圍：每年 2 月的 payout 含兩筆 = 前一學年下學期（`APPRAISAL_HALF_BONUS_FIRST`） + 本學年上學期（`APPRAISAL_HALF_BONUS_SECOND`） | `services/year_end/appraisal_sync.py` L43-61, L82-107 |
| 4 | 學年換算：`target_academic_year = civil_year - 1911 - 1`（2026 國曆 → 114 民國學年） | `services/year_end/appraisal_sync.py` L43-48 |
| 5 | `SalaryRecord.appraisal_year_end_bonus` 為 **獨立欄位**，不進 `gross_salary` / `total_deduction` / `net_salary`；engine `_fill_salary_record` 在算完 `gross_salary` 與 `total_deduction` 之後才賦值 | `models/salary.py` L250-256；`services/salary/engine.py` L184-201 |
| 6 | `appraisal_year_end_bonus` 不影響勞健保計算（不在 `insurance_salary` 投保薪資基準也不在補充保費基準）| `services/salary/appraisal_year_end.py` docstring L1-5 [unverified — 設計意圖明確，但未在本次掃描內逐一驗證 `insurance_salary` / `supplementary_premium` 路徑] |
| 7 | 來源 single source of truth：`special_bonus_items` 表（兩筆 `APPRAISAL_HALF_BONUS_*`）；每月 calculate 重新 query（**不** 在 `SalaryRecord` cache） | `services/salary/appraisal_year_end.py` L1-5；`services/salary/engine.py` L195-201 |
| 8 | Payout generate 透過 `pg_advisory_xact_lock` 防並行；lock key 由 `hashlib.md5("aye_payout|{payout_year}").digest()` 取前 8 bytes（避免 `PYTHONHASHSEED` 隨機化問題） | `services/year_end/appraisal_sync.py` L216-236 |
| 9 | Payout generate 為 idempotent：`(year_end_cycle_id, employee_id, bonus_type, period_label)` UNIQUE 衝突走「先 SELECT 後 INSERT/UPDATE」（同時相容 SQLite 與 PostgreSQL）；advisory lock 已防並行 | `services/year_end/appraisal_sync.py` L249-292 |
| 10 | Payout generate 自動 upsert `YearEndCycle`（不存在時新建 shell：`start=N/8/1`、`end=(N+1)/7/31`、`bonus_calc_date=payout_year/1/15`） | `services/year_end/appraisal_sync.py` L312-332 |
| 11 | Payout 時 ACTIVE 員工預設全寫；INACTIVE 員工必須在 `included_inactive_employee_ids` 集合內才寫，否則 `skipped_inactive_count += 1` | `services/year_end/appraisal_sync.py` L343-346 |
| 12 | Preview 規則：`is_excluded=True` 不列出；只在一個 cycle 出現 → 另一筆為 0 + warning；兩 cycle 都沒參與 → 不列出；summary 非 `FINALIZED` → 加 warning | `services/year_end/appraisal_sync.py` L115-209 |
| 13 | `void_payouts` 只刪 `APPRAISAL_HALF_BONUS_FIRST/SECOND`，不動其他 `SpecialBonusType` | `services/year_end/appraisal_sync.py` L410-444 |
| 14 | Payout DELETE 強制 `confirm=true` query param | `api/year_end/appraisal_payout.py` L97-106 |
| 15 | 全部 4 個 payout endpoint 統一權限 `Permission.APPRAISAL_FINALIZE`（`1 << 59`） | `api/year_end/appraisal_payout.py` L37, L47, L63, L101 |
| 16 | period_label 對 type 的 mapping：`FIRST → f"{target-1}下"`、`SECOND → f"{target}上"`（如 target=114 → "113下" + "114上"） | `services/year_end/appraisal_sync.py` L51-61 |
| 17 | `APPRAISAL_HALF_BONUS_FIRST/SECOND` 命名陷阱：與 `AppraisalCycle.Semester.FIRST/SECOND` 反向 — 前者為時間順序，後者為學期上下；由 `services/year_end/appraisal_sync.py` 自動 map | `models/year_end.py` L79-81；`services/year_end/appraisal_sync.py` L9-14 |
| 18 | 雙向同步契約 A：**改 payout 後須重 calculate 2 月薪資** — 因為 `SalaryRecord.appraisal_year_end_bonus` 是 `_fill_salary_record` 寫入；改 `special_bonus_items` 不會反向觸發 `SalaryRecord` 重算 | `services/salary/engine.py` L194-201 [needs review — code 上沒有 trigger / FK cascade；CLAUDE.md 把此契約交給 HR 操作流程保證] |
| 19 | 雙向同步契約 B：**改 `appraisal_summary.bonus_amount` 後須重 generate payout** — 因為 `generate_payouts` 是 snapshot；`special_bonus_items.amount` 不會反向追隨 `appraisal_summary.bonus_amount` | `services/year_end/appraisal_sync.py` L295-407 [needs review — code 上沒有反向 trigger；CLAUDE.md 把此契約交給 HR 操作流程保證] |
| 20 | Cycle 非 OPEN 時，appraisal 不可修改 `base_score` / `enrollment_target` / `enrollment_actual`（會讓已 FINALIZED summary 與重算結果不一致） | `api/appraisal/__init__.py` `update_cycle` L341-353 |
| 21 | Summary 已 `FINALIZED` → `recompute_summaries` 不覆寫（要改需先走 reject 流程） | `api/appraisal/__init__.py` `recompute_summaries` L774-777 |
| 22 | 全部 appraisal 簽核端點均有自簽防呆（`assert_not_self_approval` with `doc_label="考核獎金"`） | `api/appraisal/__init__.py` 各 sign endpoint |
| 23 | 簽核端點均用 `SELECT ... FOR UPDATE`（`with_for_update`）；`reject` 為「先 perm check 再持鎖」（bug sweep 2026-05-18 P2） | `api/appraisal/__init__.py` 各 sign/reject endpoint |
| 24 | Year-end 結算端點 `add_special_bonus` 守備：必須先有對應 `YearEndSettlement` 才能加 special_bonus；`FINALIZED` settlement 拒新增（事後改錢） | `api/year_end/__init__.py` `add_special_bonus` L320-340 |

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft（從 code 反向擷取；對齊 design doc `2026-05-22-salary-appraisal-year-end-payout-design.md` 但不複製其內容） |
