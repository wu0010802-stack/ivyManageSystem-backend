# SPEC-004：薪資扣繳與保費

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `services/salary/deduction.py`、`services/salary/insurance_salary.py`、`services/salary/supplementary_premium.py`；`services/insurance_service.py`（協作 / 級距與費率 SoT）；`api/insurance.py`（對外端點） |
| Related | SPEC-001（engine 主流程）、SPEC-003（時薪基準） |

## Overview

本 SPEC 描述「薪資扣繳體系」與「保費計算體系」兩條彼此緊密耦合的扣款路徑，最終匯總為 `SalaryRecord.total_deduction` 並驅動 `net_salary`。

整體分四大扣款族群：

1. **考勤扣款**（`services/salary/deduction.py`）
   - 遲到、早退按實際分鐘比例扣款；未打卡僅記次數不扣款。
   - 單筆遲到/早退設「不超過當日日薪」上限，避免打卡異常導致超額扣款。
   - `missing_punch_deduction` 永遠為 0（保留欄位但業務上不扣款，2026-04-25 業主決議）。
2. **請假與曠職扣款**（`services/salary/utils.py:_sum_leave_deduction` + `engine._compute_absence`）
   - 依 `LEAVE_DEDUCTION_RULES`（事假全扣、病假/生理半扣、特休/婚/喪/公假/產假等不扣）。
   - 病假 240 hr 年度半薪上限（勞基法 §43 + 勞工請假規則 §4），由 caller 傳入 YTD 已用病假時數。
   - 整日請假 8 hr、部分時數依 `Attendance.partial_leave_hours` 為準。
   - HR 在 `LeaveRecord.deduction_ratio` 顯式覆寫時改採覆寫值（病假需偏離 0.5 才視為人工覆寫）。
3. **三項法定保費**（`services/insurance_service.py` 配 `services/salary/insurance_salary.py`）
   - 勞保（含就保）、健保、勞退（雇主提撥 + 員工自提）皆查 `INSURANCE_TABLE_2026` 級距表，**員工/雇主負擔金額直接讀表，不重算**；僅政府補貼與勞退自提採 instance 費率即時計算。
   - 投保薪資不得低於實際工資（勞保條例 §14）：透過 `resolve_insurance_salary_raw()` 取 `max(insurance, base)` 法定下限。
   - 議題 B：三制度可獨立指定投保金額（`labor_insured` / `health_insured` / `pension_insured`），各自套各自上限。
4. **二代健保補充保費**（`services/salary/supplementary_premium.py` + `engine.py:1620-1636`）
   - **獎金路徑**：年累計獎金逾 4× 當月投保薪資部分扣 2.11%（健保法 §31 I 1）。
   - **兼職薪資路徑**：時薪制員工當月 `hourly_total ≥ 29500` 時整筆乘 2.11%。

### `total_deduction` 邊界（重要不變式）

`SalaryRecord.total_deduction` **不含** 下列項目：

- `meeting_absence_deduction`（園務會議缺席扣款）：**只**從 `festival_bonus` 扣，不進入 `total_deduction`，避免雙重扣繳。
- `labor_insurance_employer` / `health_insurance_employer` / `pension_employer`：雇主端負擔，員工不扣，僅落 `SalaryRecord` 供財報與勞保局匯出。
- `supplementary_health_employee`：「拆分顯示用」informational column，金額**已併入** `health_insurance_employee`（不可再次加總，否則 double-count；見 `services/salary/totals.py` line 33–38 警告註解）。

`total_deduction` 在系統中有兩條重算路徑：

| 路徑 | 位置 | 公式 |
|------|------|------|
| Engine 即時計算 | `engine.py:1685-1694` | `labor_insurance + health_insurance + pension_self + late + early + leave + absence + other` |
| Record 重算（manual_adjust / fill） | `services/salary/totals.py:39-49` | `labor_insurance_employee + health_insurance_employee + pension_employee + late + early + missing_punch + leave + absence + other` |

兩條公式皆**不含** `meeting_absence_deduction`；engine 路徑也未顯式列 `missing_punch_deduction`（在 engine 中固定為 0，加上去亦無作用）。

---

## Interface Definitions

### 內部 Python 函式

#### `services/salary/deduction.py`

| 函式 | 簽章 | 說明 |
|------|------|------|
| `calculate_attendance_deduction` | `(attendance: AttendanceResult, daily_salary: float = 0, base_salary: float = 0, late_details: list \| None = None) -> dict` | 計算考勤扣款；回傳 `late_deduction / early_leave_deduction / missing_punch_deduction(=0) / late_count / early_leave_count / missing_punch_count / total_late_minutes / total_early_minutes`。`base_salary <= 0` 時扣款歸零（時薪或未設定）。 |
| `calculate_bonus` | `(target: int, current: int, base_amount: float, overtime_per: float = 500) -> dict` | 舊版獎金計算（保留相容性）；回傳 `festival_bonus / overtime_bonus / ratio`。`target <= 0` 時記 warning 並 ratio=0。 |

#### `services/salary/insurance_salary.py`

| 函式 | 簽章 | 說明 |
|------|------|------|
| `resolve_insurance_salary_raw` | `(employee_type: str, base_salary: float, insurance_salary_level: float, hourly_rate: float) -> float` | 決定查投保級距用的 raw 薪資值，永遠取「合法下限」。月薪：`max(insurance, base)`；時薪：`max(insurance, hourly_rate × 176)`；其他類型回 `insurance`。 |
| `validate_insurance_salary` | `(employee_type: str, base_salary: float, insurance_salary_level: float, hourly_rate: float = 0) -> None` | 驗證投保薪資不低於實際工資。`insurance <= 0` 視為未設定直接放行；其餘違反勞保條例 §14 時 raise `HTTPException(400)`，error code = `INSURANCE_BELOW_BASE`，並回 `suggested`（建議值）供前端顯示。 |

模組常數：

- `ESTIMATED_MONTHLY_HOURS = 176`（22 工作日 × 8 hr，時薪制估算月工時基準）

#### `services/salary/supplementary_premium.py`

| 函式 | 簽章 | 說明 |
|------|------|------|
| `query_ytd_bonus_before` | `(session, employee_id: int, year: int, month: int) -> float` | 查該員工該年度 1 月 ~ (month-1) 月已落 `SalaryRecord` 的獎金累計（六項欄位 SUM）。 |
| `calculate_bonus_supplementary_fee` | `(session, employee_id: int, year: int, month: int, *, breakdown_bonus_total: float, health_insured_salary: float, rate: float = 0.0211) -> float` | 計算本月應扣的「獎金補充保費」；採 per-payment incremental 公式，避免重複扣繳。 |
| `_resolve_health_insured_salary` | `(emp_dict: dict, insurance_service) -> float` | 解出當月健保投保薪資（已 bracket 正規化）。優先序：`emp_dict["health_insured_salary"]` → `bracket(resolved_raw)`。 |
| `apply_bonus_supplementary_to_breakdown` | `(session, emp_dict: dict, breakdown: SalaryBreakdown, year: int, month: int, insurance_service, employee_pk: int) -> int` | 計算獎金補充保費並 mutate `breakdown` 四個欄位：`health_insurance` / `supplementary_health_employee` / `total_deduction` / `net_salary`，回傳本月應扣金額。 |

模組常數：

- `BONUS_FIELDS_FOR_YTD = ("festival_bonus", "overtime_bonus", "performance_bonus", "special_bonus", "supervisor_dividend", "appraisal_year_end_bonus")` — 列入年累計的六項欄位。

#### `services/insurance_service.py` — `class InsuranceService`

| Method | 簽章 | 說明 |
|--------|------|------|
| `__init__()` | `() -> None` | 預設用 hardcode `INSURANCE_TABLE_2026` + 模組常數 instance 費率/上限；level 表示年度 = `CURRENT_INSURANCE_YEAR = 2026`。 |
| `load_brackets_from_db` | `(year: int \| None = None, *, strict: bool = False) -> bool` | 從 DB `insurance_brackets` 載入級距表。指定年度查無資料時自動 fallback 到 ≤year 中最新年度。`strict=True` 真實 exception 時 raise；`strict=False` 沿用 hardcode + log。 |
| `update_rates_from_db` | `(rate_record) -> None` | 以 DB `InsuranceRate` 覆寫 instance 費率與三制度上限；`labor_government_ratio` 由守恆律 `1 - emp - er` 計算；補充保費率/門檻 NULL 時沿用 instance 預設。 |
| `get_bracket` | `(salary: float) -> dict` | 級距 lookup：取第一個 `salary <= entry["amount"]` 的列；超出表頂回最後一筆。 |
| `calculate` | `(salary: float, dependents: int = 0, pension_self_rate: float = 0, *, no_employment_insurance: bool = False, health_exempt: bool = False, labor_insured: float \| None = None, health_insured: float \| None = None, pension_insured: float \| None = None) -> InsuranceCalculation` | 主計算入口。對 NaN / 負值 raise `ValueError`；`salary=0` 短路（除非顯式指定分項投保）；眷屬人數要求為整數。 |

> **`api/insurance.py:152` 呼叫的 `_insurance_service.import_table(...)`**：在 `services/insurance_service.py` 內找不到此 method 定義 `[unverified]`，端點可能無法正常運作或在他處（例如 mixin）定義；本 SPEC 不重述行為。

#### `dataclass InsuranceCalculation`

| 欄位 | 型別 | 說明 |
|------|------|------|
| `insured_amount` | `float` | 向後相容單一投保金額（= salary 對應級距） |
| `salary_range` | `str` | 級距顯示用字串 |
| `labor_employee` | `float` | 勞保員工自付 |
| `labor_employer` | `float` | 勞保雇主負擔 |
| `labor_government` | `float` | 勞保政府補貼（採 instance rate 計算） |
| `health_employee` | `float` | 健保員工自付（含眷屬倍數） |
| `health_employer` | `float` | 健保雇主負擔 |
| `pension_employer` | `float` | 勞退雇主提撥 |
| `pension_employee` | `float` | 勞退員工自提（依 `pension_self_rate`） |
| `total_employee` | `float` | 員工三項合計 |
| `total_employer` | `float` | 雇主三項合計 |
| `labor_insured_amount` | `float` | 議題 B：勞保實際投保金額 |
| `health_insured_amount` | `float` | 議題 B：健保實際投保金額 |
| `pension_insured_amount` | `float` | 議題 B：勞退實際提繳工資 |

---

### HTTP 端點（`api/insurance.py`）

所有端點 prefix `/api`、tag `insurance`。

| 端點 | Method | Function | Permission | Request | Response |
|------|--------|----------|------------|---------|----------|
| `/api/insurance/import` | POST | `import_insurance_table` | `SALARY_WRITE` | `InsuranceTableImport { table_type: str = "labor", data: List[dict] }` | `{ message: str }`；失敗 400 |
| `/api/insurance/calculate` | GET | `calculate_insurance` | `SALARY_READ` | Query: `salary: float`、`dependents: int = 0` | `{ insured_amount, labor_employee, labor_employer, health_employee, health_employer, pension_employer, total_employee, total_employer }`；輸入錯誤回 422（ValueError → 422 字串明細） |
| `/api/insurance/brackets` | GET | `list_brackets` | `SALARY_READ` | Query: `year?: int` | `{ requested_year, effective_year, brackets: [{ id, amount, labor_employee, labor_employer, health_employee, health_employer, pension }] }`；無資料時 fallback 到 ≤year 最新年度 |
| `/api/insurance/brackets` | PUT | `upsert_brackets` | `SALARY_WRITE` + `has_finance_approve`（`ACTIVITY_PAYMENT_APPROVE`） | `InsuranceBracketsBulkUpsert { effective_year(2020-2100), brackets: [InsuranceBracketIn], replace_existing: bool=False, reason: str(10-500), acknowledge_finalized_months: bool=False }` | `{ message, effective_year, upserted, replaced_existing, stale_marked }`；該年已有封存月份且未 ack 時 409；reload 失敗 500 |
| `/api/insurance/brackets/{bracket_id}` | DELETE | `delete_bracket` | `SALARY_WRITE` + `has_finance_approve` | `InsuranceBracketDeleteRequest { reason, acknowledge_finalized_months }` | `{ message, effective_year, stale_marked }`；級距不存在 404；已封存月份未 ack 時 409；reload 失敗 500 |

額外安全 / 稽核守衛（PUT / DELETE）：

- 級距異動後自動呼叫 `_bulk_mark_salary_stale_for_year()`，把該年所有未封存 `SalaryRecord.needs_recalc=True`，避免「stale 未標 → finalize 用新級距落帳」攻擊（audit 2026-05-07 P0 #9）。
- `reason` 必填 ≥ 10 字、寫入 `audit_logs.changes`（含 `effective_year / upserted / replaced_existing / stale_marked / finalized_months_in_year / acknowledged_finalized / reason`）。
- 該年已有封存月份時，預設拒絕；需帶 `acknowledge_finalized_months=True` 二次審批才放行（audit 2026-05-07 P2）。
- 寫 DB 成功後立即 `load_brackets_from_db(strict=True)` 同步 in-memory 級距表，避免管理員見「儲存成功」但計算仍走舊表；reload 失敗回 500 並提示「目前計算端仍使用 reload 前的級距表」。

---

## DTO Definitions

### `SalaryRecord` 扣繳相關欄位（`models/salary.py:170-313`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `labor_insurance_employee` | `Money` | 勞保費（員工自付） |
| `labor_insurance_employer` | `Money` | 勞保費（雇主負擔） |
| `health_insurance_employee` | `Money` | 健保費（員工自付），已含補充保費合併 |
| `health_insurance_employer` | `Money` | 健保費（雇主負擔） |
| `supplementary_health_employee` | `Money` NOT NULL default 0 | 二代健保補充保費（員工自付；資訊用，已併入 `health_insurance_employee`） |
| `pension_employee` | `Money` | 勞退自提 |
| `pension_employer` | `Money` | 勞退雇提 |
| `late_deduction` | `Money` | 遲到扣款 |
| `early_leave_deduction` | `Money` | 早退扣款 |
| `missing_punch_deduction` | `Money` | 未打卡扣款（業務上恆為 0） |
| `leave_deduction` | `Money` | 請假扣款 |
| `absence_deduction` | `Money` | 曠職扣款 |
| `other_deduction` | `Money` | 其他扣款 |
| `meeting_absence_deduction` | `Money` | 園務會議缺席扣節慶獎金（**不計入** `total_deduction`） |
| `late_count` / `early_leave_count` / `missing_punch_count` / `absent_count` | `Integer` | 計數欄位 |
| `total_deduction` | `Money` | 扣款總額 |
| `gross_salary` | `Money` | 應發總額 |
| `net_salary` | `Money` | 實發 = `gross_salary - total_deduction` |
| `bonus_config_id` | `FK -> bonus_configs.id` | 計算當下使用的獎金設定版本（稽核） |
| `attendance_policy_id` | `FK -> attendance_policies.id` | 計算當下使用的考勤政策版本（稽核） |

`SalarySnapshot`（`models/salary.py:318+`）為 SalaryRecord 對應金額欄位的完整 mirror，作為不可變歷史快照。

### `InsuranceBracket` 模型（`models/config.py:215-246`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `Integer PK` | |
| `effective_year` | `Integer NOT NULL` | 適用年度（西元，與 `InsuranceRate.rate_year` 對齊） |
| `amount` | `Integer NOT NULL` | 投保金額 |
| `labor_employee` | `Integer NOT NULL` | 勞保員工自付（級距預算值） |
| `labor_employer` | `Integer NOT NULL` | 勞保雇主負擔 |
| `health_employee` | `Integer NOT NULL` | 健保員工自付（單口） |
| `health_employer` | `Integer NOT NULL` | 健保雇主負擔 |
| `pension` | `Integer NOT NULL` | 勞退雇主提繳（6%） |
| `created_at` / `updated_at` | `DateTime` | server default `func.now()` |

唯一索引：`(effective_year, amount)`；查詢索引：`ix_bracket_year_amount`。

### `InsuranceRate` 模型（`models/config.py:163-212`）

| 欄位 | 型別 / 預設 | 說明 |
|------|-------------|------|
| `rate_year` | `Integer NOT NULL` | 適用年度 |
| `version` | `Integer NOT NULL = 1` | 每次更新遞增 |
| `changed_by` | `String(50)` | 最後修改人 |
| `labor_rate` | `Float = 0.125` | 勞保總費率（含就保） |
| `labor_employee_ratio` | `Float = 0.20` | |
| `labor_employer_ratio` | `Float = 0.70` | |
| `labor_government_ratio` | `Float = 0.10` | |
| `health_rate` | `Float = 0.0517` | |
| `health_employee_ratio` | `Float = 0.30` | |
| `health_employer_ratio` | `Float = 0.60` | |
| `pension_employer_rate` | `Float = 0.06` | |
| `average_dependents` | `Float = 0.56` | |
| `labor_max_insured` | `Integer NULLABLE` | 勞保（含就保）最高月投保薪資 |
| `health_max_insured` | `Integer NULLABLE` | 健保最高月投保金額 |
| `pension_max_insured` | `Integer NULLABLE` | 勞退最高月提繳工資 |
| `supplementary_health_rate` | `Float NOT NULL = 0.0211` | 二代健保補充保費率 |
| `supplementary_health_threshold` | `Integer NOT NULL = 29500` | 兼職補充保費起扣門檻 |
| `is_active` | `Boolean = True` | |

### `dataclass SalaryBreakdown`（`services/salary/breakdown.py`）

扣繳相關欄位：

- `labor_insurance: float = 0`（員工自付）
- `health_insurance: float = 0`（員工自付；補充保費以 `+=` 累加）
- `pension_self: float = 0`（員工自提）
- `supplementary_health_employee: float = 0`（資訊用拆分顯示）
- `labor_insurance_employer: float = 0`
- `health_insurance_employer: float = 0`
- `pension_employer: float = 0`

### `dataclass InsuranceCalculation`

見上述 *Interface Definitions* 段落。

### Pydantic schema（`api/insurance.py`）

| Class | 欄位 |
|-------|------|
| `InsuranceTableImport` | `table_type: str = "labor"`、`data: List[dict]` |
| `InsuranceBracketIn` | `amount: int > 0`、`labor_employee/labor_employer/health_employee/health_employer/pension: int >= 0` |
| `InsuranceBracketsBulkUpsert` | `effective_year: 2020-2100`、`brackets: List[InsuranceBracketIn] min_length=1`、`replace_existing: bool=False`、`reason: str 10-500`、`acknowledge_finalized_months: bool=False` |
| `InsuranceBracketDeleteRequest` | `reason: str 10-500`、`acknowledge_finalized_months: bool=False` |

---

## Business Rules

### BR-001 `total_deduction` 邊界不變式

`total_deduction` **不含** `meeting_absence_deduction`：園務會議缺席扣款**只**從 `festival_bonus` 扣，且 `festival_bonus` 與 `meeting_absence_deduction` 差值不得為負（`max(0, festival - meeting_absence)`，`engine.py:1676-1678`）。理由：避免「同一行為雙重扣繳」。

### BR-002 `total_deduction` 重算 SoT 一致性

`engine.py:1685-1694` 與 `services/salary/totals.py:39-49` 為**兩條** `total_deduction` 重算路徑，兩條公式不得 drift；新增扣款欄位需同步更新。`totals.py` 的版本含 `missing_punch_deduction`（engine 版本省略，因 engine 中固定為 0）。

### BR-003 `supplementary_health_employee` 不可重複加總

`supplementary_health_employee` 為「拆分顯示用」informational column，金額**已併入** `health_insurance_employee`（hourly 路徑於 `engine.py:1635-1636`、獎金路徑於 `supplementary_premium.apply_bonus_supplementary_to_breakdown` line 185-187 皆對 `breakdown.health_insurance` `+=`）。`totals.py:33-38` 註解明確警告不可加進 `total_deduction`，否則 double-count。

### BR-004 考勤扣款公式

- **每分鐘扣款率**：`base_salary / (MONTHLY_BASE_DAYS × 8 × 60) = base_salary / 14400`（`MONTHLY_BASE_DAYS = 30`，勞基法基準）。
- **遲到扣款**：每筆 `min(minutes × per_minute_rate, daily_salary)`，加總；`late_details` 為日級列表，若 None 則僅以 `total_late_minutes` 算一筆。
- **早退扣款**：`min(total_early_minutes × per_minute_rate, early_count × daily_salary)`，整月共用同一「總和上限」（不分日）。
- **未打卡**：永遠 `missing_punch_deduction = 0`，僅 `missing_punch_count = missing_punch_in_count + missing_punch_out_count`。
- **`base_salary <= 0`**（時薪制或未設定）：所有扣款歸零（時薪制不適用此公式，由其他路徑處理）。

> AttendancePolicy 的 `late_deduction / early_leave_deduction / missing_punch_deduction` 欄位 **已 deprecated**，DB 欄位保留但 `AttendancePolicyUpdate` API 不再接受。

### BR-005 請假扣款規則（`services/salary/utils.py:_sum_leave_deduction`）

- 扣款比例查 `LEAVE_DEDUCTION_RULES`（`services/salary/constants.py:21-37`）：
  - **全扣（1.0）**：事假 `personal`、家庭照顧假 `family_care`
  - **半扣（0.5）**：病假 `sick`、生理假 `menstrual`
  - **不扣（0.0）**：特休 `annual`、婚假 `marriage`、喪假 `bereavement`、公假 `official`、產假 `maternity`、陪產假 `paternity` / `paternity_new`、產檢假 `prenatal`、流產假 `miscarriage`、育嬰留職停薪 `parental_unpaid`、補休 `compensatory`
- **病假 240 hr 年度半薪上限**（勞基法 §43 + 勞工請假規則 §4）：
  - `ytd_sick_hours_before_month` 之內 → 扣半薪；超過 → 扣全薪（雇主得不給薪）。
  - 病假依 `start_date` 由早到晚處理，「先請的先享半薪額度」。
- **HR `LeaveRecord.deduction_ratio` 覆寫**：
  - 病假：偏離標準 `0.5` 才視為**人工覆寫**（避免誤把標準值當成覆寫）。
  - 其他假別：`deduction_ratio is not None` 即覆寫，否則 fallback 至 `LEAVE_DEDUCTION_RULES`。
- **小時換算**：`status == LEAVE` 視為 8 hr；`partial_leave_hours > 0` 用該值；其餘 0 hr 不計扣。
- **扣款金額**：`(hours / 8) × daily_salary × ratio`。

### BR-006 投保薪資合法下限（勞保條例 §14）

`resolve_insurance_salary_raw()` 永遠取「合法下限」，即使 DB 短報亦自動 max 回實際工資（高報則保留以維護員工權益）：

| `employee_type` | raw 公式 |
|-----------------|---------|
| `regular`（月薪） | `max(insurance_salary_level, base_salary)` |
| `hourly`（時薪） | `max(insurance_salary_level, hourly_rate × ESTIMATED_MONTHLY_HOURS)`，其中 `ESTIMATED_MONTHLY_HOURS = 176`（22 工作日 × 8 hr） |
| 其他 | `insurance_salary_level` |

`insurance_salary_level <= 0` 視為「未設定」，由 caller 自行 fallback。

### BR-007 投保薪資驗證 API 守衛

`validate_insurance_salary()`：當 `insurance > 0` 時：

- `insurance < MINIMUM_MONTHLY_WAGE`（29500，2026/115 年）→ 400 `INSURANCE_BELOW_BASE`、`context.kind=below_minimum_wage`
- 月薪制且 `insurance < base_salary` → 400、`kind=below_monthly_wage`
- 時薪制且 `insurance < hourly_rate × 176` → 400、`kind=below_hourly_estimated`

違規處罰提示：「勞保局查核可處短繳差額 2-4 倍罰鍰」（勞保條例 §72）。

### BR-008 級距 lookup 與三制度上限 clamp

- **級距 lookup**：`get_bracket(salary)` 取第一個 `salary <= entry["amount"]` 的列（介於兩級距時取較高級數），超出表頂回最後一筆。
- **三制度上限不同**（2026 年）：
  - 勞保（含就保）`LABOR_MAX_INSURED_SALARY = 45800`
  - 健保 `HEALTH_MAX_INSURED_SALARY = 219500`
  - 勞退 `PENSION_MAX_INSURED_SALARY = 150000`
- 三制度先 `min(salary, max_insured)` clamp 再 lookup，互不影響。
- DB `InsuranceRate.{labor,health,pension}_max_insured` 可覆寫（NULL 沿用模組常數）。

### BR-009 級距金額 vs 費率計算的覆蓋策略

- 員工/雇主負擔金額（`labor_employee / labor_employer / health_employee / health_employer / pension`）**永遠**由 `INSURANCE_TABLE_2026` 級距表決定，**不可** 由 `amount × rate × ratio` 重算（這些值為政府公告經特殊鋪陳，重算會引入捨入誤差）。
- 僅「政府補貼 `labor_government`」與「勞退自提 `pension_employee`」採 DB `InsuranceRate` 的 instance 費率即時計算：
  - `labor_government = round_half_up(amount × labor_rate × labor_government_ratio)`
  - `pension_employee = round_half_up(amount × pension_self_rate)`
- `labor_government_ratio` 採守恆律 `1 - employee_ratio - employer_ratio`，避免三比例和不為 1。

### BR-010 免就保（退休再聘等）扣率調整

`no_employment_insurance=True` 時：勞保扣款從 12.5%（含就保 1%）下調至 11.5%，三邊（員工 / 雇主 / 政府）皆按 `(labor_rate - 0.01) / labor_rate` ≈ 0.92 比例縮放。實作用比例縮放避免重算引入捨入誤差。`EMPLOYMENT_INSURANCE_RATE = 0.01` 為就保固定費率。

### BR-011 健保眷屬計算與豁免

- 健保員工自付額隨眷屬人數倍增：`health_employee_base × (1 + min(max(0, dependents), 3))`（最多 3 人；負值以 0 計，防 DB 舊資料或直接寫入產生負保費）。
- `health_exempt=True`（公保 / 老人健保等其他管道）：員工本人 + 眷屬皆不扣（含 `health_employee` 與 `health_employer` 都歸零）。
- **季扣眷屬**（業主實務 `engine.py:1592-1612`）：本人 + 第 1 名「月扣」；第 2 名以上「季扣」每 3 個月一次，僅在 1/4/7/10 月份額外扣 `health_employee_base × extra_dependents_quarterly × 3`（以「未含眷屬」單口健保金額為基數）。`health_exempt=True` 時不扣季扣。

### BR-012 議題 B：三制度分項投保

- `labor_insured / health_insured / pension_insured` 三個參數可獨立指定某制度的投保金額；`None` 或 `0` 沿用 `salary`（防前端 `el-input-number` 清欄 emit 0 而被 clamp 到最低級距 1500）。
- 各自仍套自己制度的上限 clamp 與級距 lookup。
- `salary=0` 短路：若**未**顯式指定任何分項投保則回全 0 結果；若**有**顯式分項則不短路（自願免薪仍要投保的場景）。

### BR-013 二代健保補充保費 — 獎金路徑（健保法 §31 I 1）

法規定義：「所領取之全年累計逾當月投保金額四倍部分之獎金」扣 2.11%。

- **列入年累計（六項，2026-05-26 業主確認）**：
  - `festival_bonus`（三節獎金）
  - `overtime_bonus`（超額獎金）
  - `performance_bonus`（績效獎金）
  - `special_bonus`（特別獎金/紅利）
  - `supervisor_dividend`（主管紅利）
  - `appraisal_year_end_bonus`（考核年終，2 月發放）
- **不入累計**：
  - `birthday_bonus`（生日禮金，福利金性質）
  - `overtime_pay` / `meeting_overtime_pay`（加班費，經常性給予）
  - `base_salary` 與各 deduction
- **per-payment incremental 公式**（`supplementary_premium.py:74-120`）：
  ```
  appraisal           = query_appraisal_year_end_bonus(...)   # 僅 2 月有
  current_month_total = breakdown_bonus_total + appraisal
  ytd_before          = ∑ SalaryRecord(bonus_fields) WHERE year==year AND month<this_month
  ytd_after           = ytd_before + current_month_total
  threshold           = 4 × health_insured_salary
  basis               = max(ytd_before, threshold)
      # 第一次破門檻：ytd_before < threshold → basis=threshold（只扣超門檻部分）
      # 累計已破門檻：ytd_before ≥ threshold → basis=ytd_before（本月全額扣）
  excess              = max(0, ytd_after - basis)
  fee                 = round_half_up(excess × rate)
  ```
- **重算情境**：例如 3 月重算 1 月，`month=1` → `ytd_before=0`（1 月之前無紀錄）。
- `breakdown_bonus_total` 由 caller 傳入（不含 `appraisal_year_end_bonus`，函式內部 query 加上）。

### BR-014 二代健保補充保費 — 兼職薪資路徑（`engine.py:1620-1636`）

- 適用條件：`employee_type == "hourly"` 且當月 `breakdown.hourly_total ≥ supplementary_health_threshold`（預設 29500）。
- 公式：`round_half_up(hourly_total × supplementary_health_rate)`（預設 2.11%）。
- Excel 註記：「115.01 月起未達 29500 元，兼職所得無需扣除二代健保」。
- 與獎金路徑共存：時薪員工同時拿獎金時兩條路徑皆會 `+=` 到 `supplementary_health_employee` 與 `health_insurance`（`supplementary_premium.py:155-157` 註解明確說明）。

### BR-015 獎金補充保費 mutation 範圍

`apply_bonus_supplementary_to_breakdown()` 計算後 mutate 四個欄位（`supplementary_premium.py:185-196`）：

- `breakdown.health_insurance += fee`（重算 round_half_up）
- `breakdown.supplementary_health_employee += fee`（拆分顯示用，不 round）
- `breakdown.total_deduction += fee`（重算 round_half_up）
- `breakdown.net_salary = gross_salary - total_deduction`

`health_insured_salary <= 0` 或 `rate <= 0` 時直接 return 0 不 mutate。

### BR-016 級距表異動安全守衛（PUT / DELETE `/api/insurance/brackets`）

- 寫入後立即 `load_brackets_from_db(strict=True)` 同步 in-memory 級距表。
- 自動 `_bulk_mark_salary_stale_for_year(year)`：把該年所有未封存 `SalaryRecord.needs_recalc=True`，強制 finalize 前重算（防「stale 未標 → finalize 用新級距落帳」攻擊）。
- 該年已有封存月份時預設 409；需 `acknowledge_finalized_months=True` 二次審批才放行。
- 需 `has_finance_approve` 額外權限（`ACTIVITY_PAYMENT_APPROVE`）；`reason ≥ 10 字`；`audit_logs` 落完整 changes。

### BR-017 級距表載入 fallback

`load_brackets_from_db()`：

- 指定年度查無資料時，自動 fallback 到 `≤ year` 中最新年度（同 `GET /api/insurance/brackets` 行為）。
- 真實 exception（DB 連線 / import 失敗）時，`strict=True` raise；`strict=False` log warning 沿用 hardcode（startup 路徑用）。
- `current_year > CURRENT_INSURANCE_YEAR` 時 `__init__` log warning 提示級距表已過期。

### BR-018 `InsuranceService.calculate` 輸入驗證

- `salary` 為 `float` 且 `NaN` → `ValueError`（防 NaN bypass：`NaN < 0` 為 False 會繞過負值檢查）。
- `salary < 0` → `ValueError`。
- `dependents` 必須為整數（允許 `bool` 子類與整數值的 float 如 `2.0`，後者自動 cast；其餘 `ValueError`）。`dependents=1.5` 會讓 `health_emp` 乘子變非整數，業務無意義。
- `pension_self_rate` 為 `NaN` 或不在 `[0, 0.06]` → `ValueError`（勞退自提勞基法上限 6%）。
- 分項投保（`labor_insured / health_insured / pension_insured`）為 `NaN` 或負值 → `ValueError`。

### BR-019 2026 年（民國 115 年）費率與常數

| 項目 | 值 | 法源 / 出處 |
|------|----|-------------|
| 勞保總費率（含就保） | 12.5% | 普通事故 11.5% + 就業保險 1% |
| 勞保員工 / 雇主 / 政府比例 | 20% / 70% / 10% | |
| 健保費率 | 5.17% | |
| 健保員工 / 雇主比例 | 30% / 60%（其餘 10% 為政府） | |
| 勞退雇主提撥率 | 6% | 勞基法 |
| 勞退員工自提上限 | 6% | 勞工退休金條例 §14 |
| 平均眷屬人數 | 0.56 | |
| 補充保費率 | 2.11% | 健保法 §31 |
| 兼職補充保費門檻 | 29,500 | 基本工資 |
| 月薪計算基準日數 | 30 | `MONTHLY_BASE_DAYS`（勞基法） |
| 時薪估算月工時 | 176 | `ESTIMATED_MONTHLY_HOURS`（22 × 8） |
| 基本月薪 | 29,500 | `MINIMUM_MONTHLY_WAGE`（2026/115 年） |
| 勞保最高月投保薪資 | 45,800 | |
| 健保最高月投保金額 | 219,500 | |
| 勞退最高月提繳工資 | 150,000 | |

DB `InsuranceRate` 可逐欄覆寫上述常數；NULL 沿用模組預設。

### BR-020 `bonus_config_id` / `attendance_policy_id` 稽核追蹤

`SalaryRecord` 必須記錄當下使用的 `bonus_config_id` 與 `attendance_policy_id`（FK），確保歷史月份可回溯當時使用的版本。級距表異動有 `audit_logs` 對應紀錄（`entity_type=insurance_bracket`、`action=UPDATE/DELETE`），可依 `effective_year / amount / reason / stale_marked` 追蹤完整異動軌跡。

### BR-021 級距表 `current_year` 過期警告

`InsuranceService.__init__()` 比對 `today_taipei().year > CURRENT_INSURANCE_YEAR` 時 log warning 提示行政新增 `insurance_brackets` 對應 `effective_year=current_year` 的列；DB 無新年度資料時沿用 `INSURANCE_TABLE_2026` fallback。

---

## Known Defects

### KD-1：`POST /api/insurance/import` 為 dead endpoint（BREAKING） 🔴

**現況**：`api/insurance.py:152` 呼叫 `_insurance_service.import_table(data, table_type)`，但 `services/insurance_service.py` 全檔案 **無** `def import_table` 定義；`grep -rn "def import_table"` 全 repo 0 命中。任何 client 呼叫此端點即 runtime `AttributeError: 'InsuranceService' object has no attribute 'import_table'`。

**驗證紀錄**：2026-05-28 DRIFT check（SPEC-004 v0.1 對 code）— BREAKING 級漂移。

**建議修法（給另一位開發者，二選一）**：

**選項 A：刪除端點（推薦）**
- 前端已改走 `PUT /api/insurance/brackets` 走 audited 流程（`api/insurance.py` 後段），此 endpoint 為早期 import 邏輯遺跡
- 修改範圍：
  1. 刪除 `api/insurance.py:152` 整個 `@router.post("/import")` 函式（含 route decorator 與 body）
  2. 同步刪除任何相關 `InsuranceImportRequest` Pydantic schema（若僅此端點使用）
  3. 確認 OpenAPI drift CI gate（`.github/workflows/ci.yml` `openapi-drift` job）通過：前端 `npm run gen:api:check` 不會因端點消失而 fail（推測前端早已不用）
- **接受標準**：`grep -rn "import_table\|POST.*insurance/import" .` 全 repo 命中 0；`pytest tests/test_insurance*.py` 全綠

**選項 B：補實作 `InsuranceService.import_table()`**
- 僅在確認此 endpoint 仍有 user-facing 用途時採用（不推薦，因前端已有替代）
- 修改範圍：
  1. 於 `services/insurance_service.py` 補 `def import_table(self, data: List[dict], table_type: str) -> dict:` 實作
  2. 對應補 unit test 於 `tests/test_insurance_service.py`
  3. 加 audit log（敏感操作，依 CLAUDE.md 安全規範）

**SPEC 同步**：上述任一選項實施後，把本 KD-1 移到 Changelog Bugfix Variant 紀錄修法日期 + 採用選項。

---

## Open Items

- 雇主端負擔（`labor_insurance_employer / health_insurance_employer / pension_employer`）落入 `SalaryRecord` 後是否被任何匯出 / 報表消費，本 SPEC 未追蹤 `[needs review]`。
- 補充保費「兼職薪資路徑」目前僅檢查 `hourly_total ≥ threshold`，**未** 考慮多家投保人 / 加退保跨月情境（健保法另有規定）`[needs review]`。
- `LEAVE_DEDUCTION_RULES` 中 `family_care` 標 `1.0`（全扣），註解卻寫「不給薪」`[needs review]`（兩者語意一致但註解措辭可能誤導讀者）。

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft；涵蓋 deduction / insurance_salary / supplementary_premium 三個 salary 子模組 + `InsuranceService` 協作 + `api/insurance.py` 對外端點。 |
| v0.1 | 2026-05-28 | DRIFT delta：`api/insurance.py:152` `POST /api/insurance/import` 經驗證為 dead endpoint（呼叫不存在的 `import_table` method）；升級為 Known Defects 章節 KD-1 並提供兩種建議修法（推薦刪除端點）供另位開發者實作。 |
