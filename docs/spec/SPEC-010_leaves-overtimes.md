# SPEC-010：請假與加班

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `api/leaves.py` + `api/leaves_quota.py` + `api/leaves_workday.py` + `api/leave_quota_expiry.py` + `api/student_leaves.py` + `api/overtimes.py` + `services/leave_*` + `services/leave_quota_expiry/*` + `services/overtime_conflict_service.py` + `services/overtime_pay_calculator.py` + `services/approval/cross_type_offset.py` + `utils/leave_overtime_conflict.py` + `utils/leave_validators.py` + `utils/leave_quota_helpers.py` + `models/leave.py` + `models/overtime.py` + `models/overtime_comp_leave_grant.py` |
| Related | SPEC-001／SPEC-004（請假扣款進薪資）、SPEC-003（加班費時薪基底）、SPEC-005（離職特休結算 + `UnusedLeavePayoutLog`）、SPEC-007（`LEAVES_*` / `OVERTIME_*` 權限位元）、SPEC-009（考勤 leave↔attendance sync hook）、`docs/superpowers/specs/2026-05-26-overtime-quarterly-cap-138h-design.md`、`docs/superpowers/plans/2026-05-13-attendance-request-consolidation.md`、`RELEASE_NOTES.md`（leave↔OT offset v1） |

---

## Overview

本 SPEC 涵蓋幼稚園員工的 **請假**（含勞基法 18 種假別、補休、特休週年制配額）與 **加班**（含 §32 II 月度 46h 與季度 138h 雙重上限、補休轉換、補休 1 年內折現）兩條主流程，以及兩者之間的耦合（同員工同日時段衝突檢查、leave↔OT 跨類抵扣 v1、補休假單與 OT grant 之 FIFO 帳本）。

主要設計切分：

- **`api/leaves.py`**（管理端假單 CRUD／核准／批次／匯入／附件下載，9 個端點）：請假申請的最完整路徑，包含 OT 衝突檢查、補休 grant 消耗／退回、leave↔OT 跨類抵扣、考勤 sync hook、薪資重算降級樣板。
- **`api/leaves_quota.py`**（配額查詢／初始化／手調，3 端點）：法定配額常數 (`LEAVE_DEDUCTION_RULES` / `STATUTORY_QUOTA_HOURS` / `ANNUAL_MAX_HOURS` / `SINGLE_REQUEST_MAX_HOURS` / `MONTHLY_MAX_HOURS` / sick 雙配額) + 共用 `_check_quota` / `_check_leave_limits` / `_check_compensatory_quota` / `_resolve_quota_row` / `assert_sick_leave_within_statutory_caps` / `_calc_annual_leave_hours`。
- **`api/leaves_workday.py`**（工作時數預計算，1 端點）：依排班 + 國定假日 + 週末計算可請假上限，呼叫端用 `validate_leave_hours_against_schedule` 擋超額假單。
- **`api/leave_quota_expiry.py`**（補休／特休到期管理，4 端點）：即將到期 grant、即將週年員工、payout 歷史、手動 trigger scheduler。
- **`api/student_leaves.py`**（教師端唯讀清單，1 端點）：家長端提交即自動核准（家長 portal 路徑不在本 SPEC，見 SPEC-013（家長入口）），教師端僅 list。
- **`api/overtimes.py`**（管理端加班 CRUD／核准／批次／匯入，8 端點）：加班申請主路徑，含 §32 II 月度 + 季度雙重上限同步檢查、補休配額 upsert／撤銷、考勤 grant ledger row、薪資重算降級樣板。
- **`services/overtime_conflict_service.py`**：抽出與 admin/portal 共用的 4 條 helper（`check_employee_has_conflicting_leave` / `check_overtime_overlap` / `check_overtime_type_calendar` / `check_monthly_overtime_cap` / `check_quarterly_overtime_cap`）。
- **`services/overtime_pay_calculator.py`**：純函式 `calculate_overtime_pay`，時薪基底 = `emp.base_salary / 30 / 8`（與 SPEC-003 一致）。
- **`services/leave_quota_expiry/`**：scheduler 子服務（補休 grant 結算、特休週年 cutover、7 天前 LINE 提醒、純函式 helpers）。
- **`services/approval/cross_type_offset.py`**：leave↔OT 跨類抵扣 v1（feature flag `ENABLE_LEAVE_OT_OFFSET`；metadata-only，不接 salary engine）。
- **`services/leave_overlap_service.py`**：假單重疊偵測共用 helper（admin + portal 共用）。
- **`services/leave_bonus_skip.py`**：產假／育嬰留職停薪／流產假 ⇒ 該月跳過節慶 + 超額獎金（業主慣例，由 SPEC-002 引用）。
- **`services/leave_policy.py`**：純規則 helper（>2 日需證明、事假至少提前 2 日、病假 4h 為單位）。
- **`utils/leave_overtime_conflict.py`**：跨檔共用的 `to_time` / `times_overlap` 時段比對。
- **`utils/leave_validators.py`**：跨檔共用的 `validate_leave_hours_value`（≥0.5、≤480、0.5 倍數）+ `validate_leave_date_order`（先後順序 + **跨月禁止**）。
- **`utils/leave_quota_helpers.py`**：`get_annual_leave_balance` 供 `services/offboarding` 共用（SPEC-005 引用）。

不變式（**critical**）：

1. **跨月請假禁止**：`utils/leave_validators.validate_leave_date_order` + `api/leaves.py` 兩處（create / update）+ Excel import 路徑都會擋 `start_date.month != end_date.month` → 400。前端引導使用者「拆成兩張假單」。
2. **§32 II 雙重上限同步檢查**：每次建立／更新／核准／批次／匯入加班，**必須並排呼叫** `check_monthly_overtime_cap` + `check_quarterly_overtime_cap` 兩個函式，缺一即繞過。共 6 個 enforce 點（見 Business Rules）。
3. **補休假單 ↔ OT grant 帳本一致性**：core ledger 為 `OvertimeCompLeaveGrant.consumed_hours`，FIFO 從最早 `expires_at` 扣／退；補休假單若已綁定 `source_overtime_id`，cross-type offset **必須** 短路避免雙重抵扣（防線在 `services/approval/cross_type_offset.resolve_cross_type_offset`）。
4. **特休週年 cutover idempotency**：partial unique index `uq_leave_quotas_emp_period_annual` 擋同日重跑；scheduler + `POST /leave-quota-expiry/run-now` 都走 advisory lock。
5. **薪資狀態一致性**：所有「approve / reject-of-approved / 修改／刪除已核准假單／加班」路徑必須 (a) 過 `assert_months_not_finalized` 封存守衛、(b) 呼叫 `lock_and_premark_stale` 提前標 `needs_recalc=True`、(c) 重算失敗時 fallback `mark_salary_stale` + commit。

---

## Interface Definitions

### A. HTTP 端點

#### A.1 請假（admin）— `api/leaves.py`，prefix `/api`

| Method | Path | 權限 | 用途 |
|--------|------|------|------|
| GET | `/api/leaves` | `LEAVES_READ` | 查詢請假記錄（依 `employee_id` / `year` / `month` / `status`），N+1 預載 user_roles / substitute_names / related_swap |
| POST | `/api/leaves` | `LEAVES_WRITE` | 新增請假；過 overlap + cross-type OT 衝突 + schedule + limits + quota |
| PUT | `/api/leaves/{leave_id}` | `LEAVES_WRITE` | 更新請假；已核准 → 自動退回 pending 並重算薪資；含考勤 sync hook 3/4 |
| DELETE | `/api/leaves/{leave_id}` | `LEAVES_WRITE` | 刪除請假；已核准 → 退回補休 grant + 觸發薪資重算；含考勤 sync hook 5 |
| PUT | `/api/leaves/{leave_id}/approve` | `LEAVES_WRITE` | 核准／駁回；可帶 `force_without_substitute` 或 `force_overlap`（需 `ACTIVITY_PAYMENT_APPROVE`） |
| POST | `/api/leaves/batch-approve` | `LEAVES_WRITE` + rate limit 10/60s | 批次核准；兩階段提交（Pass 1 純驗證 → Pass 2 套用 setattr/lock/grant） |
| GET | `/api/leaves/import-template` | `LEAVES_WRITE` | 下載 Excel 範本 |
| POST | `/api/leaves/import` | `LEAVES_WRITE` | 批次匯入草稿假單（status=pending） |
| GET | `/api/leaves/{leave_id}/attachments/{filename}` | `LEAVES_READ` | 附件下載（local：stream；supabase：302 signed URL） |

#### A.2 請假配額 — `api/leaves_quota.py`，prefix `/api`

| Method | Path | 權限 | 用途 |
|--------|------|------|------|
| GET | `/api/leaves/quotas` | `LEAVES_READ` | 查詢配額（含動態 used / pending / remaining，批次 GROUP BY 避免 N+1） |
| POST | `/api/leaves/quotas/init` | `LEAVES_WRITE` | 依勞基法重算指定員工年度配額（特休依年資、其餘走法定固定值；禁止對過去年份初始化） |
| PUT | `/api/leaves/quotas/{quota_id}` | `LEAVES_WRITE` | 手動調整配額（HR override） |

#### A.3 請假工時計算 — `api/leaves_workday.py`，prefix `/api`

| Method | Path | 權限 | 用途 |
|--------|------|------|------|
| GET | `/api/leaves/workday-hours` | `LEAVES_READ` | 計算指定區間每日工時（整合 Holiday／DailyShift／ShiftAssignment／週末／預設 8h；上限 90 天） |

#### A.4 補休／特休到期 — `api/leave_quota_expiry.py`，prefix `/leave-quota-expiry`

| Method | Path | 權限 | 用途 |
|--------|------|------|------|
| GET | `/leave-quota-expiry/upcoming` | `LEAVES_READ` | 列出未來 N 天到期的 active 補休 grant |
| GET | `/leave-quota-expiry/anniversaries` | `LEAVES_READ` | 列出未來 N 天滿週年的在職員工（hire_date ≥180 天） |
| GET | `/leave-quota-expiry/payout-history` | `SALARY_READ` | 結算歷史（`unused_leave_payout_log`，最新在前） |
| POST | `/leave-quota-expiry/run-now` | `SALARY_WRITE` | 手動 trigger scheduler（advisory lock `leave_quota_expiry` + `run_key=today`） |

#### A.5 學生請假（教師唯讀）— `api/student_leaves.py`，prefix `/api/student-leaves`

| Method | Path | 權限 | 用途 |
|--------|------|------|------|
| GET | `/api/student-leaves` | `STUDENTS_READ` | 列出班級 scope 內的學生請假；非 unrestricted 角色受 `accessible_classroom_ids` 限制 |

#### A.6 加班（admin）— `api/overtimes.py`，prefix `/api`

| Method | Path | 權限 | 用途 |
|--------|------|------|------|
| GET | `/api/overtimes` | `OVERTIME_READ` | 查詢加班記錄；N+1 預載 user_roles |
| POST | `/api/overtimes` | `OVERTIME_WRITE` | 新增加班；過 overlap + cross-type leave 衝突 + 月度 + 季度 + 國定假日對齊 |
| PUT | `/api/overtimes/{overtime_id}` | `OVERTIME_WRITE` | 更新加班；已核准 → 自動退回 pending 並重算薪資；schema **拒絕翻轉 `use_comp_leave`**（須 reject + recreate） |
| DELETE | `/api/overtimes/{overtime_id}` | `OVERTIME_WRITE` | 刪除加班；已核准 → 自動撤銷補休配額 + 觸發薪資重算 |
| PUT | `/api/overtimes/{overtime_id}/approve` | `OVERTIME_WRITE` | 核准／駁回；補休模式核准後 upsert quota + 建 `OvertimeCompLeaveGrant` ledger row；body 優先於 query parameter |
| POST | `/api/overtimes/batch-approve` | `OVERTIME_WRITE` + rate limit 10/60s | 批次核准；兩階段提交 |
| GET | `/api/overtimes/import-template` | `OVERTIME_WRITE` | 下載 Excel 範本 |
| POST | `/api/overtimes/import` | `OVERTIME_WRITE` | 批次匯入草稿加班（status=pending） |

**端點總計：26 個**（leave 9 + quota 3 + workday 1 + expiry 4 + student-leaves 1 + overtime 8）。

---

### B. 內部 Python public function

#### B.1 `services/overtime_conflict_service.py`

```python
def check_employee_has_conflicting_leave(session, employee_id, overtime_date, start_time, end_time) -> None
def check_overtime_overlap(session, employee_id, overtime_date, start_time, end_time, exclude_id=None) -> Optional[OvertimeRecord]
def check_overtime_type_calendar(session, target_date, overtime_type) -> None
def check_monthly_overtime_cap(session, employee_id, target_date, new_hours, exclude_id=None) -> None
def check_quarterly_overtime_cap(session, employee_id, target_date, new_hours, exclude_id=None) -> None

# 純函式（無 session）
def _assert_within_monthly_cap(existing_hours, new_hours, year, month) -> None
def _assert_within_quarterly_cap(existing_hours, new_hours, window_label, employee_id) -> None
def _validate_overtime_type_matches_calendar(overtime_type, is_statutory_holiday) -> None
def _shift_month(year, month, offset) -> tuple[int, int]
```

#### B.2 `services/overtime_pay_calculator.py`

```python
def calculate_overtime_pay(base_salary: float, hours: float, overtime_type: str) -> float
```

時薪 = `base_salary / 30 / 8`；不同 `overtime_type` 套不同倍率（見 Business Rules）。

#### B.3 `services/approval/cross_type_offset.py`

```python
def resolve_cross_type_offset(session, leave: LeaveRecord) -> Optional[OvertimeRecord]
def _is_enabled() -> bool  # 讀 ENABLE_LEAVE_OT_OFFSET env via settings.misc
```

#### B.4 `services/leave_overlap_service.py`

```python
def find_overlapping_leave(session, employee_id, start_date, end_date, start_time=None, end_time=None, exclude_id=None, include_pending=False) -> LeaveRecord | None
def find_approved_overlapping_leave(session, employee_id, start_date, end_date, start_time=None, end_time=None, exclude_id=None) -> LeaveRecord | None
```

#### B.5 `services/leave_policy.py`

```python
def get_requested_calendar_days(start_date, end_date) -> int
def requires_supporting_document(start_date, end_date) -> bool  # > 2 日
def validate_portal_leave_rules(leave_type, start_date, end_date, leave_hours, *, today=None) -> None
```

常數：`SUPPORTING_DOCUMENT_THRESHOLD_DAYS = 2`、`PERSONAL_ADVANCE_NOTICE_DAYS = 2`、`SICK_LEAVE_INCREMENT_HOURS = 4.0`。

#### B.6 `services/leave_bonus_skip.py`

```python
SKIP_BONUS_LEAVE_TYPES = frozenset(["maternity", "parental_unpaid", "miscarriage"])

def should_skip_bonuses_for_month(session, employee_id, year, month, *, leave_types=None) -> tuple[bool, list[LeaveRecord]]
def format_skip_reason(leaves: list[LeaveRecord]) -> str
```

#### B.7 `services/leave_quota_expiry/`

```python
# comp_leave_expiry.py
def expire_comp_leave_grants(today: date, session: Session) -> dict
#   → {'paid_employees', 'total_amount', 'expired_grant_count'}

# annual_cutover.py
def cutover_annual_leave_anniversaries(today: date, session: Session) -> dict
#   → {'paid_employees', 'cold_start_employees', 'total_amount', 'total_anniversaries'}

# comp_grant_reminder.py
def init_comp_grant_reminder_line_service(svc: "LineService") -> None
def remind_upcoming_comp_grants(today: date, session: Session, days_ahead=7) -> dict
#   → {'reminded_employees', 'skipped_no_line'}

# helpers.py（純函式）
def _next_month(today) -> tuple[int, int]
def _add_one_year_with_feb29_handling(d) -> date
def _resolve_hourly_wage(emp, ref_date) -> float
def _is_anniversary_today_sql(hire_date_col, today)  # SQL expr
def _approved_annual_used_in_period(employee_id, period_start, period_end, session) -> float
def _find_or_none_salary_record(employee_id, year, month, session)
def _compensatory_balance(employee_id, session) -> float
```

#### B.8 `services/leave_quota_expiry_scheduler.py`

```python
def scheduler_enabled() -> bool
async def run_leave_quota_expiry_scheduler(stop_event: asyncio.Event) -> None
```

每日輪詢：撈到期 grant 結算 → 跑特休週年 cutover → 推 7 天前 LINE 提醒，三步用 `try_scheduler_lock` 防多 worker 重跑。

#### B.9 `api/leaves_quota.py` — public quota helpers

```python
def assert_sick_leave_within_statutory_caps(outpatient_used_hours, hospitalized_used_hours, new_hours, is_hospitalized) -> None
def _get_sick_committed_hours(session, employee_id, year, is_hospitalized, exclude_id=None) -> float
def _calc_annual_leave_hours(hire_date, year, reference_date=None) -> float
def _resolve_quota_row(session, employee_id, leave_type, *, target_date=None) -> LeaveQuota | None
def _check_quota(session, employee_id, leave_type, year, leave_hours, exclude_id=None, include_pending=True) -> None
def _check_compensatory_quota(session, employee_id, year, leave_hours, exclude_id=None, include_pending=True) -> None
def _check_leave_limits(session, employee_id, leave_type, start_date, leave_hours, exclude_id=None, include_pending=True) -> None
```

#### B.10 `api/leaves_workday.py` — public workday helpers

```python
def calculate_leave_work_hours(session, employee_id, start_date, end_date, start_time=None, end_time=None) -> float
def validate_leave_hours_against_schedule(session, employee_id, start_date, end_date, leave_hours, start_time=None, end_time=None) -> None
def _build_workday_hours_payload(session, employee_id, start_date, end_date) -> dict
def _calc_shift_hours(work_start, work_end) -> float
def _calc_bounded_shift_hours(work_start, work_end, start_bound, end_bound) -> float
```

#### B.11 `utils/leave_overtime_conflict.py`

```python
def to_time(val) -> dt_time  # str ('HH:MM') / datetime.time / datetime.datetime → time
def times_overlap(start1, end1, start2, end2) -> bool  # 開放端點重疊
```

#### B.12 `utils/leave_validators.py`

```python
def validate_leave_hours_value(v: float) -> float  # ≥0.5、≤480、0.5 倍數
def validate_leave_date_order(start_date, end_date) -> None  # 含跨月禁止
```

#### B.13 `utils/leave_quota_helpers.py`

```python
def get_annual_leave_balance(session, employee_id, snapshot_date) -> dict
#   → {'total_hours', 'used_hours', 'remaining_hours', 'remaining_days', 'snapshot_date'}
```

**public function 總計：≈ 45 個**（含純函式 helper）。

---

## DTO Definitions

### Models（ORM）

#### `LeaveRecord`（`models/leave.py`）

| Column | Type | 說明 |
|--------|------|------|
| `id` | Integer PK | — |
| `employee_id` | FK employees | — |
| `leave_type` | String(20) | 18 種假別字串（含 `compensatory`） |
| `start_date` / `end_date` | Date | 跨月禁止 |
| `start_time` / `end_time` | String(5) `HH:MM` | 半日請假必填 |
| `leave_hours` | Float, default 8 | ≥0.5、≤480、0.5 倍數 |
| `is_deductible` | Boolean default True | — |
| `deduction_ratio` | Float default 1.0 | 0.0 / 0.5 / 1.0 三檔（由 `LEAVE_DEDUCTION_RULES`） |
| `is_hospitalized` | Boolean default False | sick 雙配額專用 |
| `reason` | Text | — |
| `attachment_paths` | Text (JSON list) | 超過 2 日請假需要附件 |
| `status` | String(20) default `pending` | P1 dual-write SoT；values: pending / approved / rejected |
| `approved_by` | String(50) | core 角色 username |
| `rejection_reason` | Text | 駁回必填 ≥1 字（leaves） |
| `source_overtime_id` | FK overtime_records ON DELETE SET NULL | 補休假單溯源（`leave_type='compensatory'`） |
| `substitute_employee_id` | FK employees | 代理人 |
| `substitute_status` | String(20) default `not_required` | `not_required` / `pending` / `accepted` / `rejected` / `waived` |
| `substitute_responded_at` | DateTime | naive，Taipei |
| `substitute_remark` | Text | — |
| `created_at` / `updated_at` | DateTime naive Taipei | `now_taipei_naive` |

`@property approval_status`：回傳 `self.status`。

Indexes：`(employee_id, start_date, end_date)`、`(employee_id, status)`、`(status, start_date)`、`(employee_id, leave_type, status)`。

#### `LeaveQuota`（`models/leave.py`）

| Column | Type | 說明 |
|--------|------|------|
| `id` | Integer PK | — |
| `employee_id` | FK employees ON DELETE CASCADE | — |
| `year` | Integer | 西元年（legacy） |
| `school_year` | Integer NULL | 民國學年（新格式） |
| `leave_type` | String(20) | — |
| `total_hours` | Float | — |
| `note` | String(200) | 年資計算依據等備註 |
| `period_start` / `period_end` | Date NULL | 週年制配額（**annual** 專用，`hire_date` 基準） |
| `created_at` / `updated_at` | DateTime naive | — |

Partial unique indexes（讀寫關鍵）：

- `uq_leave_quota_legacy`：`(employee_id, year, leave_type) WHERE school_year IS NULL`
- `uq_leave_quotas_employee_school_year_type`：`(employee_id, school_year, leave_type) WHERE school_year IS NOT NULL`
- `uq_leave_quotas_emp_period_annual`：`(employee_id, period_start, leave_type) WHERE period_start IS NOT NULL AND leave_type = 'annual'` ← **特休週年 cutover idempotency 守門**

#### `OvertimeRecord`（`models/overtime.py`）

| Column | Type | 說明 |
|--------|------|------|
| `id` | Integer PK | — |
| `employee_id` | FK employees | — |
| `overtime_date` | Date | 月度／季度 cap 計算基準 |
| `overtime_type` | String(20) | `weekday` / `weekend` / `holiday` |
| `start_time` / `end_time` | DateTime | 不支援跨日加班；`start < end` |
| `hours` | Float default 0 | ≤ `MAX_OVERTIME_HOURS = 12` |
| `overtime_pay` | Float default 0 | 由 `calculate_overtime_pay` 計算 |
| `use_comp_leave` | Boolean default False | True ⇒ 不發加班費、改累積補休 |
| `comp_leave_granted` | Boolean default False | 防止重複發放補休配額 |
| `status` | String(20) default `pending` | — |
| `approved_by` | String(50) | — |
| `reason` | Text | — |
| `created_at` / `updated_at` | DateTime naive Taipei | — |

⚠ `OvertimeRecord` **無 `rejection_reason`** column；駁回原因落 `ApprovalLog.comment` 同 transaction。

Indexes：`(employee_id, overtime_date)`、`(employee_id, status)`、`(status, overtime_date)`。

#### `PunchCorrectionRequest`（`models/overtime.py`）

補打卡申請（屬考勤模組，SPEC-009 引用），欄位略；`status` 同 dual-write SoT。

#### `OvertimeCompLeaveGrant`（`models/overtime_comp_leave_grant.py`）

per-OT 補休帳本（grant ledger），由「核准補休模式加班」觸發。

| Column | Type | 說明 |
|--------|------|------|
| `id` | BigInteger PK | — |
| `overtime_record_id` | FK overtime_records ON DELETE CASCADE **UNIQUE** | 一筆 OT 對應一筆 grant |
| `employee_id` | FK employees ON DELETE CASCADE | — |
| `granted_hours` | Float | == `ot.hours` |
| `granted_at` | Date | == `ot.overtime_date` |
| `expires_at` | Date | `granted_at + 365 days` |
| `consumed_hours` | Float default 0 | FIFO 扣抵；CHECK `consumed_hours ≤ granted_hours` |
| `status` | String(20) default `active` | `active` / `expired` / `revoked` |
| `expired_at` | DateTime | scheduler 結算時記 |
| `reminder_sent_at` | DateTime | 7 天前 LINE 推播防重複 |
| `payout_salary_record_id` | FK salary_records ON DELETE SET NULL | Layer 1 直寫成功時 |
| `payout_log_id` | FK unused_leave_payout_log ON DELETE SET NULL | scheduler 結算建立的 log |
| `created_at` / `updated_at` | DateTime naive | — |

Indexes：`(employee_id, status, expires_at)`、partial `(expires_at) WHERE status='active'`。

---

### Pydantic schemas

#### `api/leaves.py`

```python
class LeaveCreate:
    employee_id: int
    leave_type: str  # 必在 LEAVE_DEDUCTION_RULES
    start_date: date; end_date: date  # 跨月禁止；end_date ≥ start_date
    start_time: Optional[str]; end_time: Optional[str]  # leave_hours<8 必填
    leave_hours: float = 8  # ≥0.5、≤480、0.5 倍數
    reason: Optional[str]
    is_hospitalized: bool = False  # sick 專用
    deduction_ratio: Optional[float]  # 0.0~1.0，覆蓋預設

class LeaveUpdate:  # 同上但全 Optional；含相同跨月／時數驗證
class ApproveRequest:
    approved: bool
    rejection_reason: Optional[str]
    force_without_substitute: bool = False
    force_overlap: bool = False
    force_overlap_reason: Optional[str]  # force_overlap=True 時 ≥10 字必填
class LeaveBatchApproveRequest:
    ids: List[int]; approved: bool; rejection_reason: Optional[str]
class LeaveImportRow(ExcelImportSchema):  # 中文 alias
    employee_code; employee_name; leave_type; start_date; end_date; leave_hours; reason
```

#### `api/leaves_quota.py`

```python
class QuotaUpdate:
    total_hours: float  # ≥0
    note: Optional[str]
```

#### `api/overtimes.py`

```python
class OvertimeCreate:
    employee_id: int
    overtime_date: date
    overtime_type: str  # 必在 OVERTIME_TYPE_LABELS
    start_time: Optional[str]; end_time: Optional[str]  # HH:MM；start<end
    hours: float  # >0、≤MAX_OVERTIME_HOURS(12)
    reason: Optional[str]
    use_comp_leave: bool = False

class OvertimeUpdate:
    # 同上但全 Optional；
    # @model_validator(mode='before') _reject_use_comp_leave_flip：
    #   傳入 use_comp_leave → ValueError「請改走 reject + recreate」
class OvertimeApproveRequest:
    approved: bool = True; rejection_reason: Optional[str]
class OvertimeBatchApproveRequest:
    ids: List[int]; approved: bool; rejection_reason: Optional[str]
class OvertimeImportRow(ExcelImportSchema):
    employee_code; employee_name; overtime_date; overtime_type; hours
    start_time; end_time; reason; use_comp_leave  # 中文 alias
```

---

### 業務常數

#### 假別扣薪規則（`LEAVE_DEDUCTION_RULES`）

| 假別 | code | 扣薪比例 | 備註 |
|------|------|---------|------|
| 事假 | `personal` | 1.0 | 全扣；portal 需提前 2 日 |
| 病假 | `sick` | 0.5 | 扣半薪；勞工請假規則第 4 條雙配額 |
| 生理假 | `menstrual` | 0.5 | 性工法 §14-1 |
| 特休 | `annual` | 0.0 | 週年制配額 |
| 產假 | `maternity` | 0.0 | 觸發跳獎金 |
| 陪產假（舊） | `paternity` | 0.0 | — |
| 公假 | `official` | 0.0 | — |
| 婚假 | `marriage` | 0.0 | 年累計 64h 上限 |
| 喪假 | `bereavement` | 0.0 | 單次 64h 上限 |
| 產檢假 | `prenatal` | 0.0 | — |
| 陪產檢及陪產假 | `paternity_new` | 0.0 | — |
| 流產假 | `miscarriage` | 0.0 | 觸發跳獎金 |
| 家庭照顧假 | `family_care` | 1.0 | 不給薪；勞基法 §20 |
| 育嬰留職停薪 | `parental_unpaid` | 0.0 | 留停期間無薪；觸發跳獎金 |
| 補休 | `compensatory` | 0.0 | 由 OT 動態累積 |
| 公傷病假 | `occupational_injury` | 0.0 | — |
| 安胎休養 | `pregnancy_rest` | 0.5 | 依病假規定 |
| 颱風假 | `typhoon` | 1.0 | 勞基法可不給薪 |

#### 法定配額（`STATUTORY_QUOTA_HOURS`）

`sick=240h(30天)`、`menstrual=24h(3天)`、`personal=112h(14天)`、`family_care=56h(7天)`。

#### 病假雙配額（勞工請假規則第 4 條）

`SICK_OUTPATIENT_CAP_HOURS=240`、`SICK_HOSPITALIZED_CAP_HOURS=2080`、`SICK_TOTAL_CAP_HOURS=2080`。

#### 其他上限

- `ANNUAL_MAX_HOURS = {"marriage": 64}` — 婚假年累計（勞工請假規則 §3）
- `SINGLE_REQUEST_MAX_HOURS = {"bereavement": 64}` — 喪假單次
- `MONTHLY_MAX_HOURS = {"menstrual": 8}` — 生理假每月（性工法 §14-1）

#### 加班常數（`utils/constants.py`）

| 常數 | 值 | 說明 |
|------|----|------|
| `MAX_OVERTIME_HOURS` | 12 | 單筆加班上限（8 + 4） |
| `MAX_MONTHLY_OVERTIME_HOURS` | 46 | §32 II 月度 |
| `MAX_QUARTERLY_OVERTIME_HOURS` | 138 | §32 II 季度 rolling 3 月 |
| `OVERTIME_QUARTERLY_WINDOW_MONTHS` | 3 | 窗口長度 |
| `DAILY_WORK_HOURS` | 8 | 每日法定 |
| `WEEKDAY_FIRST_2H_RATE` | 1.34 | 平日前 2h |
| `WEEKDAY_AFTER_2H_RATE` | 1.67 | 平日 3-4h |
| `WEEKDAY_THRESHOLD_HOURS` | 2 | 平日倍率分界 |
| `RESTDAY_FIRST_2H_RATE` | 1.34 | 休息日前 2h |
| `RESTDAY_MID_RATE` | 1.67 | 休息日 3-8h |
| `RESTDAY_AFTER_8H_RATE` | 2.67 | 休息日 >8h |
| `RESTDAY_FIRST_SEGMENT` | 2 | 第一分段 |
| `RESTDAY_SECOND_SEGMENT` | 8 | 第二分段 |
| `RESTDAY_MIN_HOURS` | 2 | 最低計費（休息日工作不足 2h 仍算 2h） |
| `HOLIDAY_RATE` | 2.0 | 例假日／國定假日全部 ×2.0 |

`OVERTIME_TYPE_LABELS`：`weekday`／`weekend`／`holiday`。

#### `services/leave_policy.py`

`SUPPORTING_DOCUMENT_THRESHOLD_DAYS = 2`、`PERSONAL_ADVANCE_NOTICE_DAYS = 2`、`SICK_LEAVE_INCREMENT_HOURS = 4.0`。

---

## Business Rules

### R1. 假別 ↔ 配額對應

| 配額類 | 假別 | 規則來源 |
|--------|------|---------|
| `QUOTA_LEAVE_TYPES` | `annual`、`sick`、`menstrual`、`personal`、`family_care` | 走 `_check_quota`；未初始化 quota 則略過（HR fallback） |
| 補休專用 | `compensatory` | 走 `_check_compensatory_quota`；quota 不存在視為 0（任何申請都會超限），quota 由 OT 核准動態累積 |
| 病假雙配額 | `sick` | 走 `_guard_leave_quota → assert_sick_leave_within_statutory_caps`；勞工請假規則第 4 條 |
| 年累計上限 | `marriage` | `_check_leave_limits` |
| 單次上限 | `bereavement` | `_check_leave_limits` |
| 月累計上限 | `menstrual` | `_check_leave_limits`（超過 3 天部分依性工法 §14-1 改填病假） |
| 不追蹤上限（事件型） | 其餘 | 略過 |

### R2. 配額自動計算

- **特休**（`_calc_annual_leave_hours(hire_date, year, reference_date)`）：依勞基法第 38 條算年資 → 配額。完整月數 < 6 → 0；6~12 月 → 24h；1~2y → 56h；2~3y → 80h；3~5y → 112h；5~10y → 120h；≥10y → `min(15 + complete_years - 10, 30) × 8`。
- **其餘配額** 走 `STATUTORY_QUOTA_HOURS` 固定值。
- `POST /api/leaves/quotas/init` 重算指定員工年度配額；**禁止對過去年份初始化**（HR 不得回溯改稽核紀錄）。
- 已存在配額 row 只 update `total_hours` / `note`，不刪除（保留手動 override 痕跡）。
- 學年優先讀（`_resolve_quota_row` 先按 `school_year` 查、找不到 fallback 西元年 legacy row）。

### R3. 跨月請假禁止

- `utils/leave_validators.validate_leave_date_order` 在 `LeaveCreate` / `LeaveUpdate` model_validator + `import` 路徑 + `update_leave` endpoint 全部 enforce。
- 訊息：「請假區間不可跨月，若需跨越月底請拆成兩張假單分別申請」。

### R4. 假單時數驗證

四層防線（任一觸發即 400/409）：

1. Pydantic：`leave_hours` ≥0.5、≤480、0.5 倍數；`leave_hours<8` 必填 `start_time/end_time`。
2. `validate_leave_hours_against_schedule`：不得超過該區間排班工時（扣午休 12-13）。
3. `_check_leave_limits`：婚假年累計、喪假單次、生理假月累計。
4. `_guard_leave_quota` ⇒ 分流：sick 雙配額／compensatory 專用／其他走 `_check_quota`。

approve 路徑會用 `include_pending=True + exclude_id=leave_id` 再驗一次，防止並發 approve 多張 pending 假單造成配額超支。

### R5. 重疊偵測

- **同員工同假**：`_check_overlap`（approved only）／`_find_overlapping_leave`（含 pending）。
- **同員工同日 leave vs OT**（修補 2026-05-11 P1-5）：`_check_employee_has_conflicting_overtime`（建立 leave）+ `_check_employee_has_conflicting_leave`（建立 OT）；雙方任一全日 ⇒ 同日衝突；雙方都半日 ⇒ 時段精比。
- **代理人衝突**（V14 + P1-9 + P1-10 + F-005）：`_check_substitute_leave_conflict` 同時擋 leave + OT；代理人離職／停用 → 400；detail 採 generic 訊息防探測。
- 重疊核准硬擋：approve 時若同員工同時段另有 approved 假單 → 409。可帶 `force_overlap=True + force_overlap_reason(≥10字) + ACTIVITY_PAYMENT_APPROVE 權限` 強制放行（三重守衛）。批次核准不支援 force_overlap。

### R6. 跨類抵扣（`ENABLE_LEAVE_OT_OFFSET`）

Feature flag 預設 false（讀 `settings.misc.enable_leave_ot_offset`，env 接受 `true`/`1`/`yes` 不分大小寫）。

**v1 metadata-only**（**不影響 salary engine**）：

- 觸發：僅在 `approve_leave` 且 `approval_changed=True` 路徑，呼叫 `resolve_cross_type_offset(session, leave)`。
- 短路條件（任一成立即回 None）：flag 關閉／補休假單（`source_overtime_id is not None`）／`start_date is None`。
- 比對條件：同員工 + `overtime_date == leave.start_date`（**跨日 leave 只看首日**）+ OT `status='approved'` + `use_comp_leave=False`。
- 偵測到時：
  1. 在 OT 的 ApprovalLog 寫一筆 `action='update'`，`comment='leave 跨類抵扣（auto, v1 metadata-only）'`，`metadata={'offset_by_leave_id': leave_id, 'offset_date': str(start_date)}`。
  2. `AuditLog.audit_changes` 加 `cross_offset_ot_id: <ot_id>`。
- 偵測失敗（任何 exception）不阻斷主流程，僅 warning。
- 已知限制（記於 RELEASE_NOTES）：不接 salary engine、跨日只看首日、單向觸發（approve OT 時不反向）、補休假單已排除、`use_comp_leave=True` 的 OT 已排除。

### R7. §32 II 雙重上限（月度 46h + 季度 138h）

**並排呼叫 unfailingly**：每一個 enforce 點都必須同時呼叫 `check_monthly_overtime_cap` + `check_quarterly_overtime_cap`。

季度窗口（曆月對齊 rolling 3 月，M = `target_date.month`）：

- W1: [M-2, M]
- W2: [M-1, M+1]
- W3: [M, M+2]

任一窗口違反即 raise；多窗口同時超標時回報「最先超過」（W1→W2→W3 順序）。已駁回的 OT 不計入；`exclude_id` 用於 update 排除自身。

訊息範例：「員工 #12 連續三個月（2026/04~2026/06）已申請加班 130.0 小時，加上此筆 10.0 小時合計 140.0 小時，超過勞基法第 32 條第 2 項每連續三個月延長工時上限 138 小時。」

### R8. 6 個 enforce 點

| # | 路徑 | 檔案行數 |
|---|------|---------|
| 1 | `POST /api/overtimes`（admin create） | `api/overtimes.py:634-639` |
| 2 | `PUT /api/overtimes/{id}`（admin update） | `api/overtimes.py:785-798` |
| 3 | `PUT /api/overtimes/{id}/approve`（admin approve，僅 `approved and not was_approved`） | `api/overtimes.py:1125-1138` |
| 4 | `POST /api/overtimes/batch-approve`（Pass 1） | `api/overtimes.py:1345-1358` |
| 5 | `POST /api/overtimes/import`（admin Excel） | `api/overtimes.py:1718-1719` |
| 6 | `POST /api/portal/my-overtimes`（teacher 自助 portal） | `api/portal/overtimes.py:171-172` |

⚠ 任一新增 OT 建立／核准入口必須補上這兩條檢查；否則為 §32 II 違規漏洞。

### R9. `overtime_type` ↔ 國定假日對齊（§37）

`check_overtime_type_calendar(session, target_date, overtime_type)` 查 `Holiday` 表後驗：

- `holiday` 但日期非國定假日 → 400（防溢付）
- `weekday`/`weekend` 但日期為國定假日 → 400（防短付違反 §37）

### R10. 加班費計算（`calculate_overtime_pay`）

時薪 = `base_salary / 30 / 8`；`base_salary <= 0` → 400。

- `weekday`：≤2h × 1.34；>2h 前 2h × 1.34 + 後段 × 1.67
- `weekend`（休息日）：`billable = max(hours, RESTDAY_MIN_HOURS=2)`；≤2h × 1.34；≤8h 前 2h × 1.34 + 中段 × 1.67；>8h 加 ×2.67
- `holiday`：全段 × 2.0
- `use_comp_leave=True` ⇒ 直接 `pay = 0.0`（改累積補休）

`hours = min(hours, MAX_OVERTIME_HOURS)` 防溢出；金額 `round_half_up` 至整數元。

### R11. 補休（comp leave）轉換規則

#### 核准補休模式 OT（`_grant_comp_leave_quota`）

只在 `use_comp_leave=True and comp_leave_granted=False` 觸發：

1. upsert `LeaveQuota(leave_type='compensatory', year=ot.overtime_date.year).total_hours += ot.hours`（`with_for_update`）。
2. INSERT `OvertimeCompLeaveGrant(overtime_record_id=ot.id, granted_hours=ot.hours, granted_at=ot.overtime_date, expires_at=ot.overtime_date + 365d, status='active')`。
3. 設 `ot.comp_leave_granted = True`。

#### 補休假單核准（FIFO 扣抵 `_consume_compensatory_grants_fifo`）

- 找該員工所有 `status='active'` 的 grant，按 `expires_at` 升冪。
- 從最早到期的扣 `consumed_hours += min(available, remaining)`。
- 不足 → `ValueError`（前端應已被 `_check_compensatory_quota` 擋；defense-in-depth）。

#### 補休假單駁回 / 刪除（`_release_compensatory_grants_fifo`）

對稱退回：按 `expires_at` 升冪、`consumed_hours > 0` 的 grant 退回。

#### 撤銷 OT 補休（`_revoke_comp_leave_grant`）

撤銷流程（已核准 OT 被修改／刪除／駁回）：

1. 找 `source_overtime_id=ot.id` 的補休假單：
   - 已核准 ⇒ 409「請先撤銷相關補休假單後再操作」（人工撤銷）
   - 待審核 ⇒ 自動駁回，寫 ApprovalLog `comment='auto_revoked_by_overtime_rollback (#{ot.id})'`
2. 全域 committed 檢查：`new_total = quota.total_hours - ot.hours` 必須 ≥ 該年度其他補休假單 committed（含 approved + pending）；不足 → 409。
3. mark `OvertimeCompLeaveGrant.status='revoked'`（不刪 row，留 audit）。
4. `quota.total_hours = max(0, new_total)`；`ot.comp_leave_granted = False`。

`current_user` 由 caller 傳入，無 caller 時 fallback `system_auto`（修補 P1-8）。

#### 翻轉 `use_comp_leave` 禁止（修補 P2-14）

`OvertimeUpdate` schema 在 `mode='before'` 拒絕 `use_comp_leave` 欄位；要切換補休/加班費模式須走 reject + recreate，避免狀態機脫節。

### R12. 補休到期 scheduler（`expire_comp_leave_grants`）

每日由 asyncio scheduler 呼叫；advisory lock `scheduler_name='leave_quota_expiry', run_key=today.isoformat()`。

對所有 `status='active' AND expires_at <= today AND Employee.is_active=True` 的 grant：

1. 依 `employee_id` 分組（order by `id` 確保 FIFO 可預期）。
2. `unexpired_hours = sum(granted - consumed)`；若 ≈ 0（`< 1e-9` 防 float underflow）⇒ 全消耗，mark expired，**不建 log**。
3. 否則：
   - `hourly_wage` 由 `_resolve_hourly_wage(emp, today)` 算（時薪：`hourly_rate`；月薪：`base_salary/30/8`）
   - `amount = round_half_up(calculate_unused_leave_compensation(unexpired_hours, hourly_wage))`
   - 寫 `UnusedLeavePayoutLog(source_type='comp_grant_expiry', salary_period_year/month=_next_month(today), meta={expired_grant_ids, hours_breakdown})`
   - **Layer 1**：若該員工目標月 `SalaryRecord` 存在且未 finalize ⇒ `unused_leave_payout += amount`，反向綁 `log.salary_record_id`
   - **Layer 2**：否則 `log.salary_record_id = None`，由 salary engine 之後 calculate 時撈 pending log 寫入（見 SPEC-005）
   - mark 所有 grant：`status='expired'`、`expired_at`、`payout_log_id`、`payout_salary_record_id`

每員工 `session.begin_nested()` savepoint 隔離；單員工失敗不影響其他。

### R13. 特休週年 cutover（`cutover_annual_leave_anniversaries`）

每日由同一 scheduler 呼叫；同 advisory lock。

對所有 `is_active=True AND hire_date 月日 == today 月日 AND hire_date <= today-180d`：

1. 找今日到期 period row（`period_start <= today AND period_end >= today AND leave_type='annual' AND period_start IS NOT NULL`）。
2. 若 current 存在：
   - `used = _approved_annual_used_in_period(emp, period_start, today)`
   - `unused = max(0, total_hours - used)`
   - 若 unused > 0 ⇒ 寫 `UnusedLeavePayoutLog(source_type='annual_anniversary', source_ref_id=current.id)`，Layer 1/2 直寫同 R12
3. 若 current 不存在（cold start）：`cold_start_count++`，不結算
4. **一律**建新 period row：`period_start=today, period_end=_add_one_year_with_feb29_handling(today)`，`total_hours=_calc_annual_leave_hours(hire_date, today.year, today)`

**Idempotency**：partial unique idx `uq_leave_quotas_emp_period_annual` 擋同日重跑；同日重複 INSERT → `IntegrityError` → savepoint rollback → 略過 log。

2/29 fallback：非閏年的 2/28 同時撈 hire_date=2/29 員工（`_is_anniversary_today_sql`）。

### R14. 7 天前 LINE 提醒（`remind_upcoming_comp_grants`）

撈 `status='active' AND reminder_sent_at IS NULL AND today <= expires_at <= today+7d AND Employee.is_active=True`。

- 依員工聚合 → 一則 Flex 訊息（總時數 + 最早 `expires_at`）。
- `_line_service.push_flex_to_user` 由 `init_comp_grant_reminder_line_service` 注入。
- 推播成功 → `reminder_sent_at = now_taipei`；全消耗也 stamp 防重撈。

### R15. 補休假單已綁定 OT 的雙重抵扣防護

補休假單 (`leave_type='compensatory' AND source_overtime_id is not None`) 在 `resolve_cross_type_offset` 內**短路回 None**（v1 行為），避免「補休假單 (= 已消耗 OT 的補休)」又再次去 offset 一筆 OT 的加班費。

進一步保險：核准 OT 時若使用者反悔切換模式，必須走 `reject + recreate` 流程（R11 已強制），確保 `comp_leave_granted` 旗標與 ledger row 同步。

### R16. 月薪封存（finalize）守衛

所有「approve / reject-of-approved / 修改／刪除已核准假單／加班」必經：

1. `assert_months_not_finalized(session, employee_id, months)` — 該月 `SalaryRecord.is_finalized=True` ⇒ 400。
2. `lock_and_premark_stale(session, employee_id, months)` — 取 per-emp advisory lock + `needs_recalc=True`。封住「caller commit 釋放鎖後 finalize 搶在 engine 重算前封舊薪資」的 race window。
3. 重算失敗降級：`mark_salary_stale(session, emp_id, year, month)` + commit；避免後續 finalize 在「狀態變更已落地但薪資未更新」的中間態誤封舊資料。

### R17. 考勤同步 hook（leave↔attendance sync）

`services.employee_leave_attendance_sync` 提供 `apply` / `revert` / `reapply` 三入口；本模組 5 個 hook 點（SPEC-009 內詳述）：

- Hook 1/2：approve 路徑（`approved=True and not was_approved` → `apply`；`approved=False and was_approved` → `revert`）
- Hook 3：update 退審（`old.status=approved → new.status=pending` → `revert`）
- Hook 4：update 改關鍵欄但仍 approved（罕見） → `reapply(old_snapshot=...)`
- Hook 5：delete 已核准 → `revert`

異常處理：`LeaveAttendanceConflict` / `LeavePartialTimeMissing` → 422；其他例外不 catch，讓 `session.close()` 觸發隱式 rollback，避免 `leave.status` 殘留。

### R18. 自我核准防護

`is_self_approval(current_user, leave.employee_id)` ⇒ 403「不可自我核准請假單／加班單」。leave 與 OT 兩條路徑都檢查。

### R19. 角色資格檢查

`assert_approver_eligible(session, doc_type, doc_label, submitter_employee_id, approver_role)` — 依 `approval_settings` 表（per `submitter_role` × `doc_type` 設可審角色清單）。403 訊息：「您的角色（X）無權審核此員工（Y）的請假申請」。批次路徑用 `_eligibility_cache` 避免 N 次查 DB。

### R20. 已核准 → 駁回 / 修改的稽核打標

業務允許「approver 改判」（既有設計），但會以 `risk_tags` 顯式打標：

- `reject_of_approved`：核准後又駁回
- `force_overlap`：重疊強制核准
- `force_without_substitute`：未取得代理人接受強制核准

`audit_summary` 帶「⚠ {tags}」字串，前端 AuditLogView 可篩出高風險事件。

### R21. 批次核准兩階段提交（atomicity 修補 P0-1）

leave + OT 批次都採 **Pass 1 純驗證 → Pass 2 套用 setattr/lock/grant/ApprovalLog → Phase 2 commit + 重算** 三段：

- Pass 1 只收集 validated metadata；不 touch ORM dirty state。
- Pass 2 任一條目套用失敗 ⇒ rollback 整批，把 applied 內條目全 mark failed。
- Phase 2 commit 成功才開始重算薪資；recalc 失敗 ⇒ mark stale + commit stale 標記。
- 批次速率限制：10/60s。

### R22. Datetime 寫入

所有 naive `Column(DateTime)` 視為 Asia/Taipei naive；`LeaveRecord`、`LeaveQuota`、`OvertimeRecord`、`OvertimeCompLeaveGrant` 之 `created_at`/`updated_at`/`expired_at`/`reminder_sent_at`/`substitute_responded_at` 全經 `now_taipei_naive`。scheduler 內 `datetime.now(_TAIPEI_TZ)` 為例外（顯式 aware）。

### R23. 安全規範

- 假單附件路徑穿越防護：`_safe_attach_path(leave_id, filename)` 確保解析後落在 upload base 內。
- 批次核准：rate limit + audit_summary 含「成功/失敗/請求」三筆數。
- approve 詳情寫 `ApprovalLog.comment`（永久稽核），不只是 logger。
- OT 駁回原因走 body 而非 query parameter（修補 P1-6），避免寫進 proxy/CDN/access log。

### R24. 學生請假（教師唯讀）

`GET /api/student-leaves` 僅列班級 scope 內的學生請假（預設 `status=approved`）；家長端提交即自動核准（路徑屬 `api/parent_portal/leaves.py`，本 SPEC scope 外）。教師端不再 approve/reject。`accessible_classroom_ids(session, current_user)` 控管 scope；`is_unrestricted` 角色（admin/principal/supervisor）放行全部班級。

---

## Rule 總計：24 條 Business Rules、26 個 HTTP 端點、≈ 45 個 public function。

## 標記

- `[unverified]` 0 處
- `[needs review]` 1 處（R22：scheduler 內 `datetime.now(_TAIPEI_TZ)` 直接呼叫，未走 `utils/taipei_time` 三入口；CI Ruff DTZ005 應已通過但與 CLAUDE.md「禁用 `datetime.now()`」契約字面衝突，疑似 scheduler-only 例外）

---

## Changelog

- v0.1 / 2026-05-28 / Initial draft（從 `api/leaves*.py` + `api/overtimes.py` + `services/leave_*` + `services/overtime_*` + `services/approval/cross_type_offset.py` + `models/leave.py` + `models/overtime.py` + `models/overtime_comp_leave_grant.py` + `utils/leave_*` + `RELEASE_NOTES.md` 擷取）
