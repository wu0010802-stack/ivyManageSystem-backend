# SPEC-001：薪資 Engine 主流程

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `services/salary/{__init__,engine,totals,breakdown,breakdown_enrollment,finalize_guard,bulk_preload,utils,constants}.py`；`api/salary/{__init__,calculate,simulate,manual_adjust,records,detail}.py` |
| Related | SPEC-002（節慶獎金 `services/salary/festival.py`）；SPEC-003（時薪／最低工資 `services/salary/{hourly,minimum_wage}.py`）；SPEC-004（扣繳 `services/salary/{deduction,insurance_salary,supplementary_premium}.py`）；SPEC-005（離職結算與比例 `services/salary/{severance,unused_leave_pay,proration}.py`）；SPEC-006（年終考核 `services/salary/appraisal_year_end.py`） |

## Overview

`SalaryEngine`（`services/salary/engine.py`）為幼稚園後端薪資模組的核心服務，於 `main.py` 啟動時建立為 singleton，並透過 `api/salary/__init__.py` 的 `init_salary_services()` 注入給薪資相關 router 使用（薪資、保險、加班、請假、獎金預覽、設定等）。

### 主要職責

1. **設定載入與歷史月份切換**：自 DB（`BonusConfig`、`AttendancePolicy`、`InsuranceRate`、`GradeTarget`、`JobTitle.bonus_grade`、`PositionSalaryConfig`）載入計算所需參數，並提供 `config_for_month(session, year, month)` context manager，於重算歷史月份時臨時 swap 成「該月當下有效」的版本，離開時還原（含 in-memory snapshot cache 加速）。
2. **單員工計算流程**：
   - `preview_salary_calculation()` — 只算不寫（GET 端點）。
   - `process_salary_calculation()` — 計算 + upsert SalaryRecord（含 IntegrityError retry）。
3. **批次計算流程**：`process_bulk_salary_calculation()` — 以 ~13 次批次預載查詢取代 N×13，per-employee 以 SAVEPOINT 包覆失敗隔離，整批單次 commit。
4. **聚合欄位重算與 manual_override 保留**：`_fill_salary_record()` + `services/salary/totals.py` 的 `recompute_record_totals()` 為單一公式來源。
5. **封存／併發守衛**：`_check_not_finalized()` 與 `services/salary/finalize_guard.py` 的 `assert_months_not_finalized()` 共用攔截邏輯；advisory lock + needs_recalc 旗標 + version 樂觀鎖協同。
6. **發放月期間累積**：節慶 / 超額獎金規則為「發放月（2/6/9/12）= 期間每月各自比例的合計」，由 `_compute_period_accrual_totals()` + `calculate_period_accrual_row()` 提供。

### 不涵蓋

| 模組 | 改由哪份 SPEC 描述 |
|------|----------------------|
| `services/salary/festival.py`（純函式：等級對應、節慶／超額獎金公式） | SPEC-002 |
| `services/salary/hourly.py`、`services/salary/minimum_wage.py`（時薪分段計費、最低工資） | SPEC-003 |
| `services/salary/deduction.py`、`services/salary/insurance_salary.py`、`services/salary/supplementary_premium.py`（考勤扣款、投保薪資、二代健保補充保費） | SPEC-004 |
| `services/salary/severance.py`、`services/salary/unused_leave_pay.py`、`services/salary/proration.py`（資遣費、未休假折算、月中入／離職比例） | SPEC-005 |
| `services/salary/appraisal_year_end.py`（2 月年終考核獎金） | SPEC-006 |

本 SPEC 仍會在主流程中提及上述模組的呼叫點與介面契約（例如 `_calculate_deductions` 呼叫 `services.salary.supplementary_premium.apply_bonus_supplementary_to_breakdown`），但詳細公式留給對應 SPEC。

---

## Interface Definitions

### 內部 Python 函式 — `SalaryEngine`（`services/salary/engine.py`）

下列為 caller 直接使用、或被 router/外部 service 依賴的方法。私有方法（`_xxx`）若被 `api/salary/`、`api/portal*`、`tests/` 直接呼叫，亦列入。

#### 生命週期 / 設定

- `SalaryEngine.__init__(load_from_db: bool = False, insurance_service: Optional[InsuranceService] = None)` — 建立 engine 實例。`load_from_db=True` 時於建構末段呼叫 `load_config_from_db()`；`insurance_service` 為 None 時自建一個（向下相容 unit test），production 由 `main.py` 注入 singleton。
- `SalaryEngine.load_config_from_db()` — 從 DB 重新載入所有設定（`BonusConfig` / `AttendancePolicy` / `InsuranceRate` / `GradeTarget` / `JobTitle.bonus_grade` / `PositionSalaryConfig`），並清空 `_month_config_cache`。受 `_config_swap_lock` 保護。
- `SalaryEngine.invalidate_month_config_cache()` — 外部主動清空 `(year, month) → snapshot` 快取，用於設定變更但未走 `load_config_from_db` 的路徑。
- `SalaryEngine.config_for_month(session, year: int, month: int)` — context manager，臨時把 engine 屬性 swap 成 `(year, month)` 對應歷史版本，離開時還原（含 `insurance_service` rate）。同 thread 重入安全（`threading.RLock`），含 in-memory snapshot cache。
- `SalaryEngine.set_bonus_config(bonus_config: dict)` — 由前端 dict 覆寫獎金設定（受同一把 `_config_swap_lock` 保護）。
- `SalaryEngine.set_deduction_rules(rules: dict)` — 更新 `deduction_rules`（歷史相容 stub；實際扣款由 `services/salary/deduction.py` 直接以勞基法基準計算）。

#### 節慶獎金 / 紅利 thin wrapper（委派至 `services/salary/festival.py`）

- `get_position_grade(position: str) -> Optional[str]`
- `get_festival_bonus_base(position: str, role: str) -> float`
- `get_target_enrollment(grade_name: str, has_assistant: bool, is_shared_assistant: bool = False) -> int`
- `get_supervisor_dividend(title: str, position: str = "", supervisor_role: str = "") -> float`
- `get_supervisor_festival_bonus(title: str, position: str = "", supervisor_role: str = "") -> Optional[float]`
- `get_office_festival_bonus_base(position: str, title: str = "") -> Optional[float]`
- `get_overtime_target(grade_name: str, has_assistant: bool, is_shared_assistant: bool = False) -> int`
- `get_overtime_per_person(role: str, grade_name: str) -> float`
- `is_eligible_for_festival_bonus(hire_date, reference_date=None) -> bool` — 依 `_attendance_policy["festival_bonus_months"]`（預設 3）判斷年資門檻。

#### 公開計算入口

- `calculate_salary(employee: dict, year: int, month: int, attendance: AttendanceResult = None, bonus_settings: dict = None, leave_deduction: float = 0, classroom_context: dict = None, office_staff_context: dict = None, meeting_context: dict = None, working_days: int = 22, overtime_work_pay: float = 0, personal_sick_leave_hours: float = 0, period_festival_override: Optional[float] = None, period_overtime_override: Optional[float] = None) -> SalaryBreakdown` — 純計算入口（不寫 DB）。回傳 `SalaryBreakdown` dataclass。時薪／月薪兩種分流，最後一次性 round + 負值守衛。
- `calculate_festival_bonus_breakdown(employee_id: int, year: int, month: int, *, _ctx: dict | None = None) -> dict` — UI 「節慶獎金明細」用，回傳 `{name, category, bonusBase, targetEnrollment, currentEnrollment, ratio, festivalBonus, overtimeBonus, remark}`。`_ctx` 可傳預先批次查詢結果避免 N+1。
- `calculate_period_accrual_row(employee_id: int, year: int, month: int, *, _ctx: dict | None = None) -> dict` — 單員工×單月「本期累積」列資料，回傳 `{festival_bonus, overtime_bonus, meeting_absence_deduction, category}`；**不**套用「事病假 > 40h 全清零」規則（該規則僅限發放月當月）。
- `calculate_attendance_deduction(attendance, daily_salary: float = 0, base_salary: float = 0, late_details: list = None) -> dict` — 委派至 `services/salary/deduction.calculate_attendance_deduction`。
- `calculate_bonus(target: int, current: int, base_amount: float, overtime_per: float = 500) -> dict` — 委派至 `services/salary/deduction.calculate_bonus`（舊版相容）。
- `calculate_overtime_bonus(role, grade_name, current_enrollment, has_assistant, is_shared_assistant=False) -> dict` — 委派至 `festival.calculate_overtime_bonus`。
- `calculate_festival_bonus_v2(position, role, grade_name, current_enrollment, has_assistant, is_shared_assistant=False) -> dict` — 委派至 `festival.calculate_festival_bonus_v2`。

#### 主流程入口（含 DB 寫入）

- `preview_salary_calculation(employee_id: int, year: int, month: int) -> SalaryBreakdown` — 只算不寫，保證無副作用（最後 `session.rollback() + close`）。
- `process_salary_calculation(employee_id: int, year: int, month: int) -> SalaryBreakdown` — 單員工計算 + upsert `SalaryRecord`。流程：先 `acquire_salary_lock` → `_build_breakdown_for_month` → 查 existing record → `_check_not_finalized` → `_fill_salary_record` → flush → `_mark_discipline_applied` → commit；`IntegrityError` 時 rollback + retry 一次。
- `process_bulk_salary_calculation(employee_ids: list, year: int, month: int, progress_callback=None) -> tuple[list, list]` — 批次計算入口；progress_callback 簽名 `callable(done: int, total: int, emp_name: str)`。回傳 `(results: list[tuple[Employee, SalaryBreakdown]], errors: list[dict])`。內部 SAVEPOINT 包覆每位員工，失敗自動補上 `needs_recalc=True`。

#### 主流程私有 helper（被 router / unit test 直接呼叫）

| 方法 | 用途 |
|------|------|
| `_check_not_finalized(salary_record, emp_name, year, month)` | classmethod 守衛；既有 record `is_finalized=True` 時 raise `ValueError` |
| `_get_bonus_reference_date(year, month) -> date` | 取月底 date，供節慶獎金年資門檻判斷（避免月首口徑誤判） |
| `_get_effective_bonus_title(title, bonus_grade_override) -> str` | 以 `bonus_grade`（A/B/C）覆寫節慶獎金職稱 |
| `_resolve_standard_base(emp) -> float` | 依 `_position_salary_standards` 決定員工底薪；`bypass_standard_base=True` 短路取個人值 |
| `_load_emp_dict(emp) -> dict` | 從 `Employee` ORM 物件建構計算用 dict |
| `_load_attendance_result(session, emp, start_date, end_date, emp_dict) -> tuple[AttendanceResult, list[Attendance]]` | 查考勤、彙總統計；時薪制計算 `hourly_calculated_pay`（會 mutate `emp_dict`） |
| `_load_period_records(session, emp, start_date, end_date, year, month, daily_salary) -> dict` | 查請假、加班、園務會議；回傳 `{leave_deduction, personal_sick_leave_hours, overtime_work_pay, meeting_context, approved_leaves}` |
| `_detect_absences(session, emp, attendances, approved_leaves, start_date, end_date, year, month) -> tuple[int, float]` | 曠職偵測（查 holiday/shift/makeup 後委託 `_compute_absence`） |
| `_compute_absence(emp_id, attendances, approved_leaves, expected_workdays, daily_salary, start_date, end_date, year, month) -> tuple[int, float]` | staticmethod 純邏輯：累計每日請假時數 ≥ 8h 才視為整日覆蓋 |
| `_build_contexts(session, emp, end_date) -> tuple[Optional[dict], Optional[dict]]` | 建構 `(classroom_context, office_staff_context)`；不再依賴 `emp.classroom_id` |
| `_build_classroom_context_from_db(session, classroom, employee_id, reference_date, classroom_count_map=None, assistant_to_classes_map=None, art_to_classes_map=None) -> Optional[dict]` | 從 DB 建班級上下文（單筆路徑） |
| `_build_classroom_context_from_batch(emp, classroom, db_count_map, assistant_to_classes, art_to_classes) -> Optional[dict]` | 從預載 dict 建班級上下文（批次路徑） |
| `_build_office_staff_context(emp, total_students, classroom_context) -> Optional[dict]` | 主管 / 辦公室人員以全校在籍人數為基數 |
| `_calculate_base_gross(breakdown, employee, year, month) -> float` | 計算底薪折算（呼叫 `_prorate_for_period`），回傳 contracted_base |
| `_calculate_bonuses(breakdown, employee, year, month, classroom_context, office_staff_context, bonus_settings, personal_sick_leave_hours, *, period_festival_override=None, period_overtime_override=None) -> None` | 主管／辦公室／帶班三分流節慶獎金、超額獎金、紅利、生日禮金，並處理 `skip_payroll_bonuses` 與事病假 > 40h 短路歸零 |
| `_calculate_deductions(breakdown, employee, attendance, leave_deduction, meeting_context, overtime_work_pay, month) -> None` | 勞健保／勞退（呼叫 `insurance_service.calculate`）、季扣眷屬、二代健保補充保費（時薪路徑）、考勤扣款、請假扣款、園務會議；最終彙總 `total_deduction` |
| `_pick_primary_classroom(classrooms, employee_id) -> Optional[Classroom]` | staticmethod；班導 > 副班導 > 美師優先序 |
| `_resolve_classroom_for_employee_in_term(session, employee_id, school_year, semester) -> Optional[Classroom]` | 依學期反查；找不到 fallback 跨期 active 並 log warning |
| `_resolve_classroom_for_employee_in_month(session, employee_id, year, month) -> Optional[Classroom]` | 包裝上一個方法，先 `resolve_current_academic_term` |
| `_calculate_classroom_bonus_result(bonus_title, classroom_context) -> dict` | 帶班獎金；共用副班導 / 跨班美師依在籍人數加權平均（含本班與 `shared_other_classes`） |
| `_compute_period_accrual_totals(session, emp, year, month, *, monthly_ctx_cache=None) -> tuple[Optional[float], Optional[float]]` | 發放月累積期間每月節慶／超額；逐月 `config_for_month` swap；產假／育嬰 / 流產假月跳過（`should_skip_bonuses_for_month`） |
| `_adjust_period_totals_for_discipline(session, emp, year, month, festival_total, overtime_total) -> tuple` | 從累積總額扣減 pending 懲處（節慶優先扣完才動超額） |
| `_mark_discipline_applied(session, employee_id, year, month, salary_record_id) -> None` | 發放月寫入 record 後標記 pending 懲處已抵扣 |
| `_load_manual_salary_fields(session, employee_id, year, month) -> dict` | 重算前撈出 HR 手動加的 `performance_bonus` / `special_bonus`，避免重算歸 0 |
| `_build_breakdown_for_month(session, emp, year, month) -> SalaryBreakdown` | 純計算主幹；於 `config_for_month` 內串接：load emp / load attendance / load period records / detect absences / period accrual / calculate_salary / supplementary_premium / 含曠職的 total_deduction 重算 + 負值守衛 |
| `_bulk_preload_for_salary_month(session, employee_ids, year, month, start_date, end_date) -> _BulkSalaryPreload` | 批次預載 ~13 個查詢結果打包 |
| `_acquire_locks_and_load_existing_records(session, employee_ids, emp_map, year, month) -> dict[int, SalaryRecord]` | Phase 3：取 advisory lock + 載既有 SalaryRecord + 取鎖後二次 finalize TOCTOU 檢查（403 → 409） |
| `_build_period_monthly_context(session, year, month, classroom_map) -> dict` | Phase 2：預載期間每月學生快照 |
| `_compute_and_persist_single_employee(session, emp, preload, monthly_ctx_cache, salary_record_by_emp, year, month, start_date, end_date) -> tuple[Employee, SalaryBreakdown]` | Phase 4：單一員工計算 + SalaryRecord upsert（不 commit） |

#### Module-level helper（`services/salary/engine.py` 頂層）

- `_fill_salary_record(salary_record, breakdown, engine, session=None)` — 將 `SalaryBreakdown` 寫入 `SalaryRecord`；遵守 `manual_overrides` 跳過清單；無 override 時直接用 breakdown 的 totals，有 override 時呼叫 `recompute_record_totals` 重算；session 不為 None 時呼叫 `query_appraisal_year_end_bonus` 與 `_pull_pending_payout_logs`；最後 `needs_recalc=False`、`version += 1`。
- `_pull_pending_payout_logs(session, salary_record)` — Layer 2：把 `UnusedLeavePayoutLog.salary_record_id IS NULL` 且 period 對齊本月的 log 加總入 `salary_record.unused_leave_payout` 並反向綁定 id。
- `_get_db_session()` — 包裝 `models.database.get_session`。
- `_BulkSalaryPreload`（dataclass，從 `bulk_preload.py` re-export）— 批次預載結果容器。
- `_get_ytd_sick_hours_before(session, employee_id, year, month) -> float`、`_get_ytd_sick_hours_bulk(session, employee_ids, year, month) -> dict`（re-export）— 年度累計病假時數（勞基法 §43 240h 半薪上限）。

### 內部 Python 函式 — 其他模組

#### `services/salary/__init__.py`

純 re-export，公開 surface 包含：`MONTHLY_BASE_DAYS`、`MAX_DAILY_WORK_HOURS`、`HOURLY_OT1_RATE`、`HOURLY_OT2_RATE`、`HOURLY_REGULAR_HOURS`、`HOURLY_OT1_CAP_HOURS`、`LEAVE_DEDUCTION_RULES`、`FESTIVAL_BONUS_BASE`、`TARGET_ENROLLMENT`、`OVERTIME_TARGET`、`OVERTIME_BONUS_PER_PERSON`、`SUPERVISOR_DIVIDEND`、`SUPERVISOR_FESTIVAL_BONUS`、`OFFICE_FESTIVAL_BONUS_BASE`、`POSITION_GRADE_MAP`、`SalaryBreakdown`、`SalaryEngine`、festival/hourly/proration/utils 的若干函式。

#### `services/salary/totals.py`

- `recompute_record_totals(record)` — 從 `SalaryRecord` 各欄位重算 `gross_salary` / `total_deduction` / `bonus_amount` / `bonus_separate` / `net_salary`；engine 與 api 共用單一公式 source of truth。

#### `services/salary/breakdown.py`

- `class SalaryBreakdown(dataclass)` — 薪資明細容器（30+ 欄位，見下 DTO 章節）。

#### `services/salary/breakdown_enrollment.py`

- `compute_enrollment_breakdown(session, employee_id: int, target_date: date) -> Optional[dict]` — 給薪資頁列表用：依員工解析班導／副班導／美師班級與在籍人數，回傳 `{enrollment: {...} | None, assistant: {by_classroom: [...]} | None}`；多頭班級加 `multi_head=True` 旗標並 log warning。

#### `services/salary/finalize_guard.py`

- `collect_months_from_range(start_date, end_date) -> set[tuple[int, int]]` — 收集 leave 跨月假單影響的 (year, month)。
- `collect_months_from_dates(dates) -> set[tuple[int, int]]` — 收集多個單日所屬月份（用於 overtime / punch_correction）。
- `assert_months_not_finalized(session, *, employee_id: int, months: set[tuple[int, int]]) -> None` — 任一月份已封存即 raise `HTTPException(409)`。

#### `services/salary/bulk_preload.py`

- `class _BulkSalaryPreload(dataclass)` — 批次預載結果（17 個欄位）。
- `_get_ytd_sick_hours_before(session, employee_id, year, month) -> float`
- `_get_ytd_sick_hours_bulk(session, employee_ids, year, month) -> dict[int, float]`

#### `services/salary/utils.py`

- `is_attendance_waived(att) -> bool` — `admin_waive` 標記視為已豁免（薪資端不扣）。
- `_sum_leave_deduction_legacy(leaves, daily_salary, ytd_sick_hours_before_month=0.0) -> float` — 舊版（LeaveRecord 列表為 SoT），保留供 parity 測試。
- `_sum_leave_deduction(att_leave_pairs, daily_salary, ytd_sick_hours_before_month=0.0) -> float` — 新版（`(Attendance, LeaveRecord)` tuples 為 SoT）；病假按 `start_date` 先扣 240h 半薪額度後 unpaid。
- `get_working_days(year, month, session=None) -> int` — 含補班、排除國定假日。
- `get_bonus_distribution_month(month) -> bool` — 2/6/9/12 為 True。
- `get_current_period_passed_months(year, month) -> list[tuple[int, int]]` — 該月所屬發放期起點至查詢月（含）；發放月輸入回 `[]`。
- `get_distribution_period_months(year, month) -> list[tuple[int, int]]` — 發放月所結算的月份清單（不含發放月本身）。
- `calc_daily_salary(base_salary) -> float` — `base_salary / MONTHLY_BASE_DAYS`。
- `mark_salary_stale(session, employee_id, year, month) -> bool` — 標 `needs_recalc=True`；排除 finalized。
- `lock_and_premark_stale(session, employee_id, months) -> None` — 同時取 advisory lock + 預標 stale（同 transaction）。
- `get_meeting_deduction_period_start(year, month) -> Optional[date]` — 發放月會議缺席扣款起算日（2→1/1、6→3/1、9→7/1、12→10/1）。

#### `services/salary/constants.py`

列為模組級常數的清單：

| 常數 | 值 | 用途 |
|------|----|----|
| `MONTHLY_BASE_DAYS` | 30 | 勞基法時薪計算基準日數（月薪 / 30 / 8） |
| `MAX_DAILY_WORK_HOURS` | 12.0 | 時薪制每日工時上限 |
| `HOURLY_OT1_RATE` | 1.34 | 時薪制第 9–10 小時倍率（§24） |
| `HOURLY_OT2_RATE` | 1.67 | 時薪制第 11 小時起倍率（§24） |
| `HOURLY_REGULAR_HOURS` | 8 | 正常日工時上限 |
| `HOURLY_OT1_CAP_HOURS` | 10 | 第一分段上限 |
| `SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS` | 240.0 | 普通傷病假年度半薪上限（§43） |
| `LEAVE_DEDUCTION_RULES` | dict | 各假別 deduction_ratio fallback（personal=1.0、sick=0.5、annual=0、…） |
| `DEFAULT_LATE_PER_MINUTE` / `DEFAULT_EARLY_PER_MINUTE` / `DEFAULT_MISSING_PUNCH` | 1 / 1 / 0 | 預設扣款參數（按比例覆蓋） |
| `DEFAULT_MEETING_ABSENCE_PENALTY` | 100 | 園務會議缺席扣節慶獎金 fallback |
| `DEFAULT_MEETING_HOURS` | 2 | 園務會議每次時數 fallback |
| `POSITION_GRADE_MAP` | dict | 「幼兒園教師→A、教保員→B、助理教保員→C」對應 |
| `FESTIVAL_BONUS_BASE`、`TARGET_ENROLLMENT`、`OVERTIME_TARGET`、`OVERTIME_BONUS_PER_PERSON`、`SUPERVISOR_DIVIDEND`、`SUPERVISOR_FESTIVAL_BONUS`、`OFFICE_FESTIVAL_BONUS_BASE` | dict | 節慶／超額／主管／辦公室人員獎金預設值，可由 DB `BonusConfig` 覆寫 |

---

### HTTP 端點（`api/salary/`）

> 統一 prefix：`/api`（router 於 `api/salary/__init__.py` 設定）。權限以 `utils/permissions.py` 的 `Permission` IntFlag 為單位。

#### `POST /api/salaries/calculate`（`api/salary/calculate.py`）

| 項目 | 描述 |
|------|------|
| Function | 同步批次計算某月所有當月在職員工薪資 |
| Permission | `Permission.SALARY_WRITE`（`require_staff_permission`） |
| Request | Query：`year` (2000–2100)、`month` (1–12)；Header：JWT；Rate limit：每 IP 每小時 20 次（`_salary_calc_limiter`） |
| Response | `{results: list[dict], errors: list[dict]}`；員工數 > `MAX_BULK_EMPLOYEES_SYNC=300` 拒 413；當月有任何 finalized record 拒 409 |

#### `POST /api/salaries/calculate-async`（`api/salary/calculate.py`）

| 項目 | 描述 |
|------|------|
| Function | 啟動非同步批次計算 job，立即回傳 job_id |
| Permission | `Permission.SALARY_WRITE`；Rate limit 同步端點 |
| Request | Query：`year`、`month` |
| Response | 202；`{job_id, status, total}`；同 (year, month) 已有 active job 回 409；該月已有 finalized 回 409 |

#### `GET /api/salaries/calculate-jobs/{job_id}`（`api/salary/calculate.py`）

| 項目 | 描述 |
|------|------|
| Function | 查詢 async job 狀態與進度 |
| Permission | `Permission.SALARY_WRITE` |
| Request | Path：`job_id`；Query：`include_results=False` |
| Response | `{job_id, status, total, done, current_employee, ...}`；`include_results=True` 且 `status=completed` 時附 `results` |

#### `POST /api/salaries/simulate`（`api/salary/simulate.py`）

| 項目 | 描述 |
|------|------|
| Function | 薪資試算（沙盒模式，不寫入 DB），套用 override 後與實際 record 對比 |
| Permission | `Permission.SALARY_READ` + `enforce_self_or_full_salary`（非 admin/hr 僅能查自己） |
| Request | Body：`SalarySimulateRequest`（含 `employee_id`、`year`、`month`、`overrides`：`SalarySimulateOverride`） |
| Response | `{employee, period, overrides_active, simulated, actual, diff}`；時薪制員工不支援，回 422 |

#### `PUT /api/salaries/{record_id}/manual-adjust`（`api/salary/manual_adjust.py`）

| 項目 | 描述 |
|------|------|
| Function | 手動調整單筆薪資金額欄位；寫入 `manual_overrides` 避免後續重算覆蓋；累進樂觀鎖 `version` |
| Permission | `Permission.SALARY_WRITE` + `require_not_self_salary_record`（不得調整自己） + `require_finance_approve`（單次總變動 |delta| 合計超門檻時） |
| Request | Path：`record_id`；Header：`If-Match: "<version>"`（樂觀鎖，可選）；Body：`SalaryManualAdjustRequest`（必填 `adjustment_reason` 5–200 字，其餘金額欄位皆可選，單欄位上限 `_MANUAL_ADJUST_FIELD_MAX=500_000`） |
| Response | `{message, record: {...}}`；Response header：`ETag` 與 `X-Record-Version`；finalized → 409；If-Match 不符 → 409；無實際變更 → 400；調整後 net_salary < 0 → 400 |

#### `POST /api/salaries/finalize-month`（`api/salary/__init__.py`）

| 項目 | 描述 |
|------|------|
| Function | 封存整月薪資（封存後禁止修改，需手動解封） |
| Permission | `Permission.SALARY_WRITE`；`force=True` 額外需 `has_finance_approve` |
| Request | Body：`FinalizeMonthRequest`（`year`、`month`、`force=False`、`force_reason`：force=True 時 ≥ 10 字） |
| Response | `{message, count, finalized_by, finalized_at, force, skipped_missing, skipped_stale}`；無可封存 → 404；missing/stale 阻擋 → 409；取鎖後全已封存 → 409 |

#### `DELETE /api/salaries/{record_id}/finalize`（`api/salary/__init__.py`）

| 項目 | 描述 |
|------|------|
| Function | 解除單筆薪資封存（高風險：等同重開結帳鎖定） |
| Permission | `Permission.SALARY_WRITE` + role ∈ {admin, hr} + `has_finance_approve` + `require_not_self_salary_record` |
| Request | Path：`record_id`；Body：`UnfinalizeSalaryRequest`（`reason` 10–500 字） |
| Response | `{message}`；尚未封存 → 409；操作記錄寫入 `record.remark` 與 `request.state.audit_summary` |

#### `GET /api/salaries/logic`（`api/salary/__init__.py`）

| 項目 | 描述 |
|------|------|
| Function | 傾印目前薪資計算邏輯與所有參數設定 |
| Permission | `Permission.SALARY_READ` |
| Request | 無 |
| Response | `build_salary_logic_info(session, _salary_engine)` 結果（純設定／常數） |

#### `GET /api/salaries/employee-salary-debug`（`api/salary/__init__.py`）

| 項目 | 描述 |
|------|------|
| Function | 模擬計算單一員工指定月份完整明細（不寫 DB），供薪資試算頁右側顯示 |
| Permission | `Permission.SALARY_READ` + `enforce_self_or_full_salary` |
| Request | Query：`employee_id`、`year`、`month` |
| Response | `build_salary_debug_snapshot(session, _salary_engine, emp, year, month)`；時薪制 → 422 |

#### `GET /api/salaries/records`（`api/salary/records.py`）

| 項目 | 描述 |
|------|------|
| Function | 查詢某月薪資列表（支援分頁與 viewer 過濾） |
| Permission | `Permission.SALARY_READ`（`require_permission`，非 staff 路徑） |
| Request | Query：`year`、`month`、`skip=0`、`limit=500` (1–1000) |
| Response | `list[dict]`（含 `breakdown` enrollment 子物件、`breakdown_stale=needs_recalc`、`manual_overrides` 名單）；觸發過期月 lazy snapshot |

#### `GET /api/salaries/export-all`（`api/salary/records.py`）

| 項目 | 描述 |
|------|------|
| Function | 匯出整月薪資 xlsx / pdf |
| Permission | `Permission.SALARY_READ` + role ∈ `FULL_SALARY_ROLES`（admin/hr） |
| Request | Query：`year`、`month`、`format`=xlsx|pdf、`include_pending=False`（True 時含未封存 / needs_recalc） |
| Response | `StreamingResponse`；無記錄 → 404；寫 explicit audit log |

#### `GET /api/salaries/history`（`api/salary/records.py`）

| 項目 | 描述 |
|------|------|
| Function | 查詢員工歷史薪資 |
| Permission | `Permission.SALARY_READ` + `enforce_self_or_full_salary` |
| Request | Query：`employee_id`、`months=12` (1–60) |
| Response | `list[dict]`（最近 N 月） |

#### `GET /api/salaries/history-all`（`api/salary/records.py`）

| 項目 | 描述 |
|------|------|
| Function | 查詢全部員工年度薪資概覽（依員工分組分頁） |
| Permission | `Permission.SALARY_READ` |
| Request | Query：`year`、`skip=0`、`limit=100` (1–500) |
| Response | `{items, total, skip, limit}` |

#### `GET /api/salaries/{record_id}/audit-log`（`api/salary/detail.py`）

| 項目 | 描述 |
|------|------|
| Function | 查單筆薪資操作歷史（來源 `AuditLog` 表） |
| Permission | `Permission.SALARY_READ` + `enforce_self_or_full_salary` |
| Request | Path：`record_id`；Query：`limit=50` (1–200) |
| Response | `{record_id, items: [...]}` |

#### `GET /api/salaries/{record_id}/breakdown`（`api/salary/detail.py`）

| 項目 | 描述 |
|------|------|
| Function | 查單筆薪資聚合明細（earnings / bonuses / deductions / summary） |
| Permission | `Permission.SALARY_READ` + `enforce_self_or_full_salary` |
| Request | Path：`record_id` |
| Response | `{employee, earnings, bonuses, deductions, summary, manual_overrides}` |

#### `GET /api/salaries/{record_id}/field-breakdown`（`api/salary/detail.py`）

| 項目 | 描述 |
|------|------|
| Function | 查單筆薪資指定欄位明細；以 `(record_id, version)` 為 key、TTL 60s 快取 snapshot |
| Permission | `Permission.SALARY_READ` + `enforce_self_or_full_salary` |
| Request | Path：`record_id`；Query：`field`（必須在 `FIELD_LABELS` 內） |
| Response | `build_field_breakdown(record, emp, snapshot, field)` |

#### `GET /api/salaries/{record_id}/unused-leave-payout-detail`（`api/salary/detail.py`）

| 項目 | 描述 |
|------|------|
| Function | 查單筆薪資未休假折算明細（多筆 `UnusedLeavePayoutLog` source_type 列表） |
| Permission | `Permission.SALARY_READ` + `enforce_self_or_full_salary` |
| Request | Path：`record_id` |
| Response | `{salary_record_id, employee_id, total_amount, logs: [...]}`；詳細欄位語意屬 SPEC-005 範疇 |

#### `GET /api/salaries/{record_id}/export`（`api/salary/detail.py`）

| 項目 | 描述 |
|------|------|
| Function | 匯出單人薪資單 PDF |
| Permission | `Permission.SALARY_READ` + `enforce_self_or_full_salary` |
| Request | Path：`record_id`；Query：`format=pdf` |
| Response | PDF `StreamingResponse` |

---

## DTO Definitions

### `SalaryBreakdown`（`services/salary/breakdown.py`）

純 dataclass，作為 engine 內計算流通的中介資料，最後由 `_fill_salary_record()` 寫入 `SalaryRecord`。

```python
@dataclass
class SalaryBreakdown:
    employee_name: str
    employee_id: str
    year: int
    month: int

    # 應領項目
    base_salary: float = 0

    # 獎金
    festival_bonus: float = 0
    overtime_bonus: float = 0
    performance_bonus: float = 0
    special_bonus: float = 0
    supervisor_dividend: float = 0      # 主管紅利
    overtime_work_pay: float = 0        # 加班費
    meeting_overtime_pay: float = 0     # 園務會議加班費
    birthday_bonus: float = 0           # 生日禮金

    # 時薪制
    work_hours: float = 0
    hourly_rate: float = 0
    hourly_total: float = 0

    # 法定代扣（員工自付）
    labor_insurance: float = 0
    health_insurance: float = 0
    pension_self: float = 0
    supplementary_health_employee: float = 0  # 二代健保補充保費（已併入 health_insurance，本欄位供稽核明細）

    # 雇主端負擔（不從薪水扣，但落到 SalaryRecord 供財報）
    labor_insurance_employer: float = 0
    health_insurance_employer: float = 0
    pension_employer: float = 0

    # 考勤扣款
    late_deduction: float = 0
    early_leave_deduction: float = 0
    missing_punch_deduction: float = 0       # 保留欄位但不再扣款
    leave_deduction: float = 0
    absence_deduction: float = 0             # 曠職扣款
    meeting_absence_deduction: float = 0     # 園務會議未出席扣節慶獎金
    other_deduction: float = 0

    # 考勤統計
    late_count: int = 0
    early_leave_count: int = 0
    missing_punch_count: int = 0
    absent_count: int = 0
    total_late_minutes: int = 0
    total_early_minutes: int = 0
    meeting_attended: int = 0
    meeting_absent: int = 0
    personal_sick_leave_hours: float = 0     # 事假+病假累計時數（超過 40h 取消節慶獎金）

    # 合計
    gross_salary: float = 0
    total_deduction: float = 0
    net_salary: float = 0

    # 獎金獨立轉帳
    bonus_separate: bool = False
    bonus_amount: float = 0
```

### `SalaryRecord`（`models/salary.py`）主要欄位

| 欄位 | 型別 | 預設 | 註 |
|------|------|------|----|
| `id` | Integer PK | autoincrement | |
| `employee_id` | FK `employees.id` | — | NOT NULL |
| `bonus_config_id` | FK `bonus_configs.id` | NULL | 計算時使用的獎金設定版本（稽核追蹤） |
| `attendance_policy_id` | FK `attendance_policies.id` | NULL | 計算時使用的考勤政策版本（稽核追蹤） |
| `salary_year` / `salary_month` | Integer | — | NOT NULL；與 `employee_id` 構成唯一鍵 `uq_salary_emp_ym` |
| `base_salary` | Money | 0 | 底薪 |
| `festival_bonus` / `overtime_bonus` / `performance_bonus` / `special_bonus` | Money | 0 | 節慶／超額／績效／特別獎金 |
| `overtime_pay` | Money | 0 | 加班費 |
| `meeting_overtime_pay` / `meeting_absence_deduction` | Money | 0 | 園務會議加班費／缺席扣節慶獎金 |
| `birthday_bonus` | Money | 0 | 生日禮金 |
| `work_hours` / `hourly_rate` / `hourly_total` | Float / Money / Money | 0 | 時薪制 |
| `labor_insurance_employee` / `labor_insurance_employer` | Money | 0 | 勞保（員工自付／雇主負擔） |
| `health_insurance_employee` / `health_insurance_employer` | Money | 0 | 健保（員工自付／雇主負擔） |
| `supplementary_health_employee` | Money | 0 (NOT NULL, server_default="0") | 二代健保補充保費（hourly 月累計 + 獎金年累計逾 4× 投保薪資；已併入 `health_insurance_employee`，本欄位供拆分顯示） |
| `pension_employee` / `pension_employer` | Money | 0 | 勞退自提／雇提 |
| `late_deduction` / `early_leave_deduction` / `missing_punch_deduction` | Money | 0 | 考勤扣款；`missing_punch_deduction` 為保留欄位，不再扣款 |
| `leave_deduction` / `absence_deduction` / `other_deduction` | Money | 0 | 請假／曠職／其他扣款 |
| `late_count` / `early_leave_count` / `missing_punch_count` / `absent_count` | Integer | 0 | 考勤統計 |
| `gross_salary` / `total_deduction` / `net_salary` | Money | 0 | 應發／扣款／實發 |
| `bonus_separate` | Boolean | False | 獎金是否獨立轉帳 |
| `bonus_amount` | Money | 0 | 獨立轉帳獎金（festival + overtime + supervisor_dividend） |
| `supervisor_dividend` | Money | 0 | 主管紅利 |
| `appraisal_year_end_bonus` | Money | 0 (NOT NULL, server_default="0") | 考核年終獎金（2/5 月隨月薪同發；source = `special_bonus_items` 兩筆 `APPRAISAL_HALF_BONUS_*` SUM；不進 `gross_salary`）。詳見 SPEC-006 |
| `unused_leave_payout` | Money | 0 (NOT NULL, server_default="0") | 特休未休折現（§38；獨立欄位不進 `gross_salary`）。詳見 SPEC-005 |
| `remark` | Text | NULL | 備註（手動調整／封存／解封皆會 append） |
| `is_finalized` | Boolean | False | 是否已結算（封存） |
| `finalized_at` | DateTime | NULL | 結算時間 |
| `finalized_by` | String(50) | NULL | 結算人 username |
| `needs_recalc` | Boolean | False (NOT NULL, server_default="false") | True 表示最後一次重算失敗或上游異動後未成功重算；封存時必須為 False |
| `manual_overrides` | JSON (list[str]) | `[]` | 被 `manual_adjust_salary` 寫過的欄位名稱清單；重算時 `_fill_salary_record` 會跳過此清單內欄位 |
| `version` | Integer | 1 (NOT NULL, server_default="1") | 樂觀鎖版本號 |
| `created_at` / `updated_at` | DateTime | `now_taipei_naive` | 時區契約：Asia/Taipei naive |

### Pydantic Schemas（`api/salary/`）

#### `FinalizeMonthRequest`（`api/salary/__init__.py`）

```python
class FinalizeMonthRequest(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    force: bool = Field(False, description="略過完整性檢查；仍會封存現有記錄")
    force_reason: Optional[str] = Field(None, max_length=500)

    @model_validator(mode="after")
    def _force_requires_reason(self):
        # force=True 時必填 force_reason，至少 10 字
```

#### `UnfinalizeSalaryRequest`（`api/salary/__init__.py`）

```python
class UnfinalizeSalaryRequest(BaseModel):
    reason: str = Field(..., min_length=10, max_length=500)
```

#### `SalaryManualAdjustRequest`（`api/salary/manual_adjust.py`）

`_MANUAL_ADJUST_FIELD_MAX = 500_000.0`（單欄位合理上限，從舊版 10,000,000 降下）。

```python
class SalaryManualAdjustRequest(BaseModel):
    adjustment_reason: str = Field(..., min_length=5, max_length=200)
    base_salary: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    performance_bonus: Optional[float] = Field(...)
    special_bonus: Optional[float] = Field(...)
    festival_bonus: Optional[float] = Field(...)
    overtime_bonus: Optional[float] = Field(...)
    overtime_pay: Optional[float] = Field(...)
    supervisor_dividend: Optional[float] = Field(...)
    meeting_overtime_pay: Optional[float] = Field(...)
    birthday_bonus: Optional[float] = Field(...)
    labor_insurance_employee: Optional[float] = Field(...)
    health_insurance_employee: Optional[float] = Field(...)
    pension_employee: Optional[float] = Field(...)
    leave_deduction: Optional[float] = Field(...)
    late_deduction: Optional[float] = Field(...)
    early_leave_deduction: Optional[float] = Field(...)
    missing_punch_deduction: Optional[float] = Field(...)
    meeting_absence_deduction: Optional[float] = Field(...)
    absence_deduction: Optional[float] = Field(...)
    other_deduction: Optional[float] = Field(...)
```

`EDITABLE_SALARY_FIELDS`（dict[field_name → 中文 label]）為可被 manual_adjust 寫入的白名單；任何其他欄位都會被忽略。

#### `SalarySimulateOverride` / `SalarySimulateRequest`（`api/salary/simulate.py`）

```python
class SalarySimulateOverride(BaseModel):
    late_count: Optional[int] = Field(None, ge=0)
    early_leave_count: Optional[int] = Field(None, ge=0)
    missing_punch_count: Optional[int] = Field(None, ge=0)
    total_late_minutes: Optional[float] = Field(None, ge=0)
    total_early_minutes: Optional[float] = Field(None, ge=0)
    work_days: Optional[int] = Field(None, ge=0, le=31)
    extra_personal_leave_hours: float = Field(0, ge=0)
    extra_sick_leave_hours: float = Field(0, ge=0)
    enrollment_override: Optional[int] = Field(None, ge=0)
    extra_overtime_pay: float = Field(0, ge=0)

class SalarySimulateRequest(BaseModel):
    employee_id: int = Field(..., ge=1)
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    overrides: SalarySimulateOverride = SalarySimulateOverride()
```

---

## Business Rules

### 1. 薪資聚合公式（single source of truth：`services/salary/totals.py:recompute_record_totals`）

**規則 1.1**：`gross_salary` 在 `calculate_salary()` 內由 `_calculate_base_gross` + `_calculate_bonuses` 累加，最後於 `_calculate_deductions` 末段再 += `meeting_overtime_pay + overtime_work_pay`。完整組成：

```
gross_salary = base_salary
             + performance_bonus
             + special_bonus
             + supervisor_dividend
             + birthday_bonus
             + meeting_overtime_pay
             + overtime_work_pay
             + hourly_total          # 時薪制
```

**規則 1.2（不進 `gross_salary` 的金額）**：
- `festival_bonus` — 僅在發放月（2/6/9/12）計算，另行轉帳。
- `overtime_bonus` — 與 `festival_bonus` 同，另行轉帳。
- `appraisal_year_end_bonus` — 2 月與月薪同發但金額獨立欄位、不進 `gross_salary`（SPEC-006）。
- `unused_leave_payout` — 特休未休折現，獨立欄位（SPEC-005）。

**規則 1.3**：`recompute_record_totals(record)` 由 `SalaryRecord` 欄位重算：

```
gross_salary    = base_salary + hourly_total + performance_bonus + special_bonus
                + supervisor_dividend + meeting_overtime_pay + birthday_bonus
                + overtime_pay
total_deduction = labor_insurance_employee + health_insurance_employee + pension_employee
                + late_deduction + early_leave_deduction + missing_punch_deduction
                + leave_deduction + absence_deduction + other_deduction
bonus_amount    = festival_bonus + overtime_bonus + supervisor_dividend
bonus_separate  = (bonus_amount > 0)
net_salary      = gross_salary - total_deduction
```

**規則 1.4**：`supplementary_health_employee` 已於 hourly 路徑（`engine.py:1635`）與獎金路徑（`supplementary_premium.apply_bonus_supplementary_to_breakdown`）`+=` 到 `breakdown.health_insurance`／`record.health_insurance_employee`；此 column 為「拆分顯示用」informational，**不可**再加進 `total_deduction`，否則 double-count。

**規則 1.5**：`meeting_absence_deduction` 只從 `festival_bonus` 扣（`max(0, festival - meeting_absence_deduction)`），**不**進入 `total_deduction`。manual_adjust 連動：若只改 `meeting_absence_deduction` 未同時改 `festival_bonus`，festival 會以 `inferred_raw = old_festival + old_meeting_absence` 自動重算。

### 2. 獎金結構

**規則 2.1**：節慶獎金 / 超額獎金僅在發放月 `get_bonus_distribution_month(month) in {2,6,9,12}` 計算，非發放月強制歸 0（`_calculate_bonuses` line 1499–1501）。

**規則 2.2（發放月期間累積，業主 2026-04-25 確認）**：發放月計算須使用「期間每月各自比例的合計」覆蓋當月單月值。對照表：

| 發放月 | 結算月份 |
|--------|----------|
| 2 月 | 去年 12 月 + 當年 1 月 |
| 6 月 | 當年 2、3、4、5 月 |
| 9 月 | 當年 6、7、8 月 |
| 12 月 | 當年 9、10、11 月 |

由 `_compute_period_accrual_totals(session, emp, year, month)` 逐月 swap `config_for_month` 後 `calculate_period_accrual_row` 計算合計，並透過 `period_festival_override` / `period_overtime_override` 傳入 `calculate_salary`。產假／育嬰／流產假月份自動跳過（`should_skip_bonuses_for_month`）。

**規則 2.3**：`bonus_separate` 與 `bonus_amount` 為「顯示用聚合」`= festival + overtime + supervisor_dividend > 0`。**注意**：`supervisor_dividend` 已包進 `gross_salary`，因此 `bonus_amount` 不可被當作「另行轉帳金額」使用（否則主管紅利會雙付）。實際另行轉帳金額為 `festival + overtime`（見 `services/salary/salary_slip.py`）。

**規則 2.4**：節慶／超額獎金的「事假 + 病假累計 > 40h 全清零」規則僅在發放月當月檢查（`_calculate_bonuses` line 1513–1515），**不**套用在 `_compute_period_accrual_totals` 的逐月累積階段（`calculate_period_accrual_row` docstring 明示）。

**規則 2.5**：主管紅利（`supervisor_dividend`）與職務掛鉤、不與出勤掛鉤，**不**受「事假 + 病假 > 40h」影響。

**規則 2.6**：`skip_payroll_bonuses=True`（業主指示「不發紅利／節慶／超額／生日禮金」的特殊個案）會在 `_calculate_bonuses` 末段短路歸零 `festival_bonus / overtime_bonus / supervisor_dividend / birthday_bonus / performance_bonus / special_bonus`；基本薪 + 勞健保仍正常計算。

**規則 2.7**：生日禮金固定 $500，月份 = `employee.birthday.month` 時發放（`_calculate_bonuses` line 1517–1526）。

**規則 2.8**：節慶獎金資格基準日 = 薪資月份的**月底**（`_get_bonus_reference_date`）；改寫過去用月首判斷的 bug，使「該月才剛滿 3 個月」員工不被誤排除。

### 3. 設定版本切換（歷史月份重算）

**規則 3.1**：所有 SalaryRecord 必須記錄當下使用的 `bonus_config_id` 與 `attendance_policy_id`（由 `_fill_salary_record` 寫入），確保可回溯。

**規則 3.2**：歷史月份重算必須走 `config_for_month(session, year, month)` context manager，於該月 last day 23:59:59 為 cutoff 取 `created_at <= cutoff` 最新版本，若無則 fallback 最舊版本。`_select_active_at` 故意**不**過濾 `is_active`，否則歷史月份會被強迫使用目前最新設定，破壞對帳（缓解措施由 `BonusConfig/InsuranceRate/AttendancePolicy` 的 INSERT/UPDATE 守衛 + needs_recalc 全標守衛承擔）。

**規則 3.3**：`load_config_from_db()` 與 `config_for_month` 必須共用同一把 `_config_swap_lock`（RLock，重入安全），否則 reload 可能被 restore 蓋掉。

### 4. 封存（finalize）守衛

**規則 4.1**：`SalaryRecord.is_finalized=True` 後禁止任何重算與修改：
- `_check_not_finalized()` 在單筆與批次 upsert 流程中守衛（raise `ValueError`）。
- `manual_adjust` 端點明確檢查（409）。
- 上游事件（leave / overtime / punch_correction）改寫前必須先呼叫 `finalize_guard.assert_months_not_finalized()`（409）。

**規則 4.2**：封存前必須通過完整性檢查：
- `_find_missing_salary_employees(session, year, month)` — 當月在職員工是否都有 SalaryRecord。
- `_find_stale_salary_employees(session, year, month)` — 該月是否有 `needs_recalc=True` 且 `is_finalized != True` 的記錄。
- 任一檢查失敗 → 409；`force=True` 可繞過但需 `ACTIVITY_PAYMENT_APPROVE` 權限與 ≥ 10 字原因，並寫入每筆 `record.remark` 與 `request.state.audit_summary`。

**規則 4.3**：封存／解封必須取 advisory lock（整月 + 每位員工）後再 refresh 才執行寫入，避免 TOCTOU。`_acquire_locks_and_load_existing_records` 在批次路徑取鎖後再做一次 finalize 檢查，仍有 finalized 則 raise `HTTPException(409)`。

**規則 4.4**：解封（`DELETE /salaries/{id}/finalize`）需：role ∈ {admin, hr} + `has_finance_approve` + 不得解除自己 + `reason` ≥ 10 字；操作記錄寫入 `record.remark`。

### 5. 樂觀鎖（version）與 manual_overrides

**規則 5.1**：`_fill_salary_record` 每次成功寫入後 `record.version += 1`，使 ETag/If-Match 能偵測「前端拿到的是重算前版本」、snapshot cache 自動失效。

**規則 5.2**：`manual_adjust` 端點若帶 `If-Match` header，必須與目前 `record.version` 相符才允許寫入（不帶 If-Match 仍允許，但會累加版本）。

**規則 5.3**：`manual_adjust` 寫入的欄位名稱合併進 `record.manual_overrides`；後續 `_fill_salary_record` 重算時跳過清單內欄位（保留人工調整）。manual_adjust 之後從 `record` 各欄位重算 totals（不能用 `breakdown` 的 totals，否則與人工值脫節）。

**規則 5.4**：manual_adjust 單次請求所有欄位 `|delta|` 合計（含 festival_bonus 連動部分）超過 `FINANCE_APPROVAL_THRESHOLD` 需 `require_finance_approve`，封死「拆欄位繞過」路徑。

### 6. 並發與 `needs_recalc` 旗標

**規則 6.1**：所有薪資計算入口（單筆 `process_salary_calculation` / 批次 `process_bulk_salary_calculation` / 封存 / 解封 / manual_adjust）皆需 `utils.advisory_lock.acquire_salary_lock(session, employee_id, year, month)`。

**規則 6.2**：上游事件（請假 / 加班 / 考勤 / 會議 / 排班核准）必須在同一 session 內呼叫 `utils.salary.lock_and_premark_stale(session, employee_id, months)`，把對應月份預標 `needs_recalc=True`，封死 commit 與重算之間的 race window（封存搶先封存舊薪資）。

**規則 6.3**：批次計算 SAVEPOINT 內失敗的員工，外層補上 `needs_recalc=True`（已封存者跳過）。

**規則 6.4**：成功重算後 `_fill_salary_record` 清除 `needs_recalc=False`。

### 7. 比例計算（proration）與時薪基準

**規則 7.1**：`MONTHLY_BASE_DAYS = 30`（勞基法時薪計算基準日數）。時薪 = `base_salary / 30 / 8`；日薪 = `base_salary / 30`（`calc_daily_salary`）。

**規則 7.2**：月中入／離職者底薪自動折算（`_calculate_base_gross` → `_prorate_for_period`）。**加班費時薪**基準為 `emp.base_salary`（僅底薪，**不**含加給或獎金）— 詳細邏輯屬 SPEC-005。

### 8. 曠職偵測（`_compute_absence`）

**規則 8.1**：時薪制（`emp.employee_type == "hourly"`）跳過曠職判定（`_detect_absences` line 2566–2567）。

**規則 8.2**：曠職定義 = `expected_workdays - attendance_dates - leave_covered`；`leave_covered` 只在「當日累計請假時數 ≥ 8h」才視為整日覆蓋，避免短時數假單規避整日曠職扣款。

**規則 8.3**：曠職扣款 = `absent_count × daily_salary`（daily_salary 以 `_resolve_standard_base(emp)` 計算）。

### 9. 二代健保補充保費（聚合契約；公式屬 SPEC-004）

**規則 9.1**：兼職薪資路徑 — `_calculate_deductions` 內 hourly 員工當月 `hourly_total ≥ supplementary_health_threshold`（預設 29500）時扣 `hourly_total × supplementary_health_rate`（預設 2.11%），並 `+=` 到 `breakdown.health_insurance`，獨立記入 `supplementary_health_employee` 欄位。

**規則 9.2**：獎金路徑 — `_build_breakdown_for_month` 與 `_compute_and_persist_single_employee` 末段呼叫 `services.salary.supplementary_premium.apply_bonus_supplementary_to_breakdown(session, emp_dict, breakdown, year, month, insurance_service, emp_id)`，將年累計獎金逾 4× 投保薪資部分扣 2.11% 加進 health。

### 10. 季扣眷屬健保

**規則 10.1**：`extra_dependents_quarterly > 0` 且 `health_exempt=False` 且 `month ∈ {1, 4, 7, 10}` 時，額外扣 `health_bracket_emp_base × extra_dependents_quarterly × 3` 個月。

### 11. 帶班獎金 / 跨班加權

**規則 11.1**：班級反查不再讀 `emp.classroom_id`，改以 `_resolve_classroom_for_employee_in_month` 依「該月份對應學期」反查 `Classroom.head/assistant/art_teacher_id`，避免班級頁面更新未同步 `Employee.classroom_id` 的 silent zero bug。

**規則 11.2**：副班導 / 美師跨多班（`len(shared_classes) >= 2`）時，其他班資訊塞進 `classroom_context.shared_other_classes`；下游 `_calculate_classroom_bonus_result` 以「在籍人數加權平均」計算節慶與超額獎金。

**規則 11.3**：mixed-role 邊界（同員工於 A 班為 head_teacher、於 B 班為 art_teacher）目前 `_pick_primary_classroom` 取 head_teacher，B 班暫不會被合併進來 [needs review]。

### 12. `request.state.audit_summary` 寫入契約

**規則 12.1**：以下端點必須在同一 session 內設定 `request.state.audit_summary`，供 AuditMiddleware / 下游觀察者消費：
- `PUT /salaries/{id}/manual-adjust` — 設 `audit_entity_id` + `audit_summary`，並呼叫 `utils.audit.write_audit_in_session` 同交易寫 AuditLog（避免 fire-and-forget 在 CI / threadpool 故障時丟稽核）。
- `POST /salaries/finalize-month`（`force=True` 時） — 設 `audit_summary` 描述 FORCE 詳情。
- `DELETE /salaries/{id}/finalize` — 設 `audit_summary` 描述解封原因與原封存資訊。

**規則 12.2**：GET 端點（不會經 AuditMiddleware）使用 `utils.audit.write_explicit_audit` 顯式寫稽核，特別是 `export-all`、`records`、`history`、`audit-log`、`breakdown`、`field-breakdown`。

### 13. 訪問控制（self-or-full）

**規則 13.1**：薪資讀取端點對非 admin/hr role 套用 `enforce_self_or_full_salary(current_user, employee_id)`，僅能查自己；admin/hr 跨員工查詢。

**規則 13.2**：「不得調整自己的薪資」由 `require_not_self_salary_record(current_user, record.employee_id, action=...)` 在 manual_adjust 與 unfinalize 強制；純管理員帳號（無 employee 綁定）不受限。

### 14. 數值守衛

**規則 14.1**：`calculate_salary` 最後一次 round（`round_half_up`）後，若 `gross_salary < 0` / `total_deduction < 0` / `net_salary < 0` 任一為負，raise `ValueError`。

**規則 14.2**：含曠職的 `_build_breakdown_for_month` 末段重算 `total_deduction` 與 `net_salary` 後再做一次負值守衛。

**規則 14.3**：`manual_adjust` 寫入後 `record.net_salary < 0` → 400「調整後淨薪資為負數」。

### 15. `MAX_BULK_EMPLOYEES_SYNC` 與限流

**規則 15.1**：`MAX_BULK_EMPLOYEES_SYNC = 300`（`calculate.py`）— 超過時拒 413，要求改用 async 端點。

**規則 15.2**：`_salary_calc_limiter`：每 IP 每小時 20 次（`SlidingWindowLimiter`，window=3600s）。注意：limiter 為 in-process 記憶體版，多 worker 部署時各 worker 額度獨立 [needs review]（CLAUDE.md 已標註 `utils/rate_limit.py` 預設為 in-process）。

### 16. 待重算下傳（Layer 2 unused leave payout）

**規則 16.1**：`_fill_salary_record(session=...)` 結尾呼叫 `_pull_pending_payout_logs` 撈取 `UnusedLeavePayoutLog.salary_record_id IS NULL` 且 period 對齊本 record 的 log，加總入 `salary_record.unused_leave_payout` 並反向綁定 id（具幂等性，filter `salary_record_id IS NULL`）。

### 17. 月底快照 lazy trigger

**規則 17.1**：`GET /salaries/records` 與 `POST /salaries/calculate` 呼叫 `_trigger_past_month_snapshot_if_missing(bg)`，若上個月有 record 但缺任何 `month_end` 快照則排背景補拍；單 worker 用 `_snapshot_lazy_guard` 去重，DB 端 partial unique index 提供二次防護。

### 18. 待確認 / 邊界條件 [needs review]

- 規則 11.3 mixed-role 邊界（多角色跨班）尚未實作合併。
- 規則 15.2 in-process rate limiter 在多 worker 部署的有效性需確認（CLAUDE.md 註記可改 PG-backed）。
- `appraisal_year_end_bonus` 在 `_fill_salary_record` 透過 `query_appraisal_year_end_bonus` 寫入，session=None 時跳過（向下相容 unit test）— 此向下相容路徑會讓沒帶 session 的呼叫者拿不到 `appraisal_year_end_bonus`，需 caller 自行確保正式路徑都帶 session [needs review]。
- `_select_active_at` 不過濾 `is_active=True`，依賴上線一筆 needs_recalc 全標守衛 + INSERT/UPDATE finance_approve（目前 `api/config.py` / `api/insurance.py` 缺此守衛，待補）[needs review]（程式碼註解第 405–411 行明示）。
- `MAX_MONTHLY_OVERTIME_HOURS` 由 `utils/constants.py` 提供，時薪制當月加班觸及上限後續加班以 1.0 倍率計薪（`_load_attendance_result` line 2322–2327 與批次路徑 line 3535–3540）；具體數值 [unverified]（未讀 utils/constants.py）。
- `services/salary/__init__.py` 的 `MeetingRecord` 與 `BonusConfig` 在 `startup/seed.py` 以 `DBBonusConfig` 別名匯入（CLAUDE.md 提及），engine 也以 `DBBonusConfig` 引用 — 設計上避免與 `api/salary.py` 同名 Pydantic schema 衝突。

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft（涵蓋 9 個服務檔 + 6 個 api 檔；engine.py 3855 行讀完並萃取公開／私有 API） |
