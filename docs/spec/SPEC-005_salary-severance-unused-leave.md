# SPEC-005：離職結算（資遣費、未休假折現、任職比例）

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | services/salary/{severance,unused_leave_pay,proration}.py; services/offboarding/; api/offboarding.py |
| Related | SPEC-001（engine 主流程） |

## Overview

本 SPEC 涵蓋幼稚園管理系統的「離職結算」三大模組與其週邊 orchestration：

1. **資遣費 / 平均工資（`services/salary/severance.py`）**
   - 依勞基法第 2 條（平均工資）、第 17 條（舊制資遣費）、勞工退休金條例第 12 條（新制資遣費）提供純函式邏輯庫。
   - **目前狀態**：預留 API，無生產 caller；唯一呼叫方為 `tests/test_severance.py`（18 個測試等同法律邏輯的可執行文件）。
   - 接生產時必須補 spec、加入 money-rounding-gate paths、移除 `tests/test_severance_dead_code_guard.py` guard。

2. **未休特休折算工資（`services/salary/unused_leave_pay.py`）**
   - 依勞基法第 38 條第 4 項，於離職或年度終結時將未休特休「時數 × 時薪」折現。
   - 生產 caller：年度週年 cutover（`services/leave_quota_expiry/annual_cutover.py`）、補休到期（`services/leave_quota_expiry/comp_leave_expiry.py`）、離職薪資預覽（`api/employees.py`）、離職 snapshot step（`services/offboarding/steps/snapshot_leave.py`）。

3. **任職比例折算（`services/salary/proration.py`）**
   - 月中入職／離職者，按「在職自然日數 ÷ 當月日數」折算當月底薪。
   - 同步建立「應上班日集合」供考勤稽核使用（hire / resign 前後不視為應上班）。
   - 生產 caller：`services/salary/engine.py`（`_calculate_base_gross` / `_compute_absence` 兩處）、`services/finance/salary_field_breakdown.py`、`evals/targets/proration_target.py`。

4. **離職 orchestration（`services/offboarding/` + `api/offboarding.py`）**
   - 一鍵離職流程串接 5 個 step（mark_appraisal / snapshot_leave / prefill_leave_payout / revoke_user / generate_certificate）。
   - 含 magic-link（30 天 / 3 次上限）與離職 ZIP bundle（證明 PDF + 12 月薪資 PDF + 12 月考勤 CSV）。
   - 寫入 `unused_leave_payout_log`（`source_type='offboarding'`）形成稽核證據鏈。

---

## Interface Definitions

### 內部 Python 函式（`services/salary/severance.py`）

```python
def calculate_service_years(hire_date: date, end_date: date) -> float
```
- 年資（小數年）= `(end_date - hire_date).days / 365.25`
- 邊界：`end_date <= hire_date` 回傳 `0.0`

```python
def calculate_average_monthly_wage(records: list[tuple[float, int]]) -> float
```
- 平均月工資 = `Σ wage ÷ Σ days × 30`
- `records` 為事由發生當日「前 6 個月」每月 `(該月所得工資, 該月日數)`
- 邊界：空 list 或 `Σ days == 0` 回傳 `0.0`

```python
def calculate_severance_pay_new(service_years: float, avg_monthly_wage: float) -> float
```
- 新制（勞退條例 §12）：`avg_monthly_wage × min(service_years × 0.5, 6.0)`
- 上限 6 個月平均工資
- 邊界：`service_years <= 0` 或 `avg_monthly_wage <= 0` 回傳 `0.0`

```python
def calculate_severance_pay_old(service_years: float, avg_monthly_wage: float) -> float
```
- 舊制（勞基法 §17）：`avg_monthly_wage × service_years`
- 無上限
- 邊界：同新制

### 內部 Python 函式（`services/salary/unused_leave_pay.py`）

```python
def calculate_unused_annual_leave_hours(entitled_hours: float, used_hours: float) -> float
```
- 未休時數 = `max(0.0, entitled_hours − used_hours)`
- 容錯：`None` → 0

```python
def calculate_unused_leave_compensation(unused_hours: float, hourly_wage: float) -> float
```
- 折算金額 = `unused_hours × hourly_wage`
- 邊界：`unused_hours <= 0` 或 `hourly_wage <= 0`（含 `None`）回傳 `0.0`
- **不負責 rounding**；由呼叫端用 `utils.rounding.round_half_up` 守 .5 邊界

### 內部 Python 函式（`services/salary/proration.py`）

```python
def _prorate_base_salary(contracted_base: float, hire_date_raw, year: int, month: int) -> float
```
- 月中入職者底薪折算（不處理離職）
- 公式：`contracted_base × worked_days / month_days`，其中 `worked_days = month_days − hire_date.day + 1`
- 入職日 1 日或更早 / hire 月與計算月不同：回傳全額
- 入職日晚於計算月份：回傳 `0.0`（補算歷史月時避免吃全額）
- **守衛**：`month ∉ 1–12` raise `ValueError`；`contracted_base < 0` raise `ValueError`
- **僅** 影響顯示用底薪，**加班費時薪基準仍用完整 `emp.base_salary`**（避免「雙重縮水」違反勞基法）

```python
def _prorate_for_period(contracted_base: float, hire_date_raw, resign_date_raw, year: int, month: int) -> float
```
- 同月同時處理入職 + 離職
- `start_day = hire_date.day if hire 在本月且 ≥ 2 else 1`
- `end_day = resign_date.day if resign 在本月且 < month_days else month_days`
- `worked_days = end_day − start_day + 1`
- 守衛：同 `_prorate_base_salary`；同月內 `resign < hire` raise `ValueError`
- 非在職月份（hire 月份 > target 或 resign 月份 < target）回傳 `0.0`

```python
def _build_expected_workdays(
    year: int, month: int, holiday_set: set, daily_shift_map: dict,
    hire_date_raw=None, resign_date_raw=None,
    today: Optional[date] = None, makeup_set: Optional[Set[date]] = None,
) -> Set[date]
```
- 建立「指定月份的預期上班日」集合（供 SalaryEngine 計算缺勤）
- 優先順序：未來日不計 → holiday 不計 → 入職前/離職後不計 → 有排班記錄看 `shift_type_id` → 補班日視為應上班 → 否則平日（週一~週五）算上班
- 守衛：`month ∉ 1–12` raise `ValueError`

### 內部 Python 函式（`services/offboarding/`）

#### `orchestrator.py`
```python
def process_offboarding(
    session: Session,
    employee_id: int,
    resign_date: date,
    resign_reason: str | None,
    operator_user_id: int,
) -> OffboardingResult
```
- 一鍵離職主入口；單一 transaction 串接 5 個 step
- 驗證：員工存在、未重複建檔（one-to-one）、`resign_date >= hire_date`、`resign_date <= today + 90 天`
- 寫入 `EmployeeOffboardingRecord`、設 `Employee.resign_date` / `resign_reason`、`resign_date <= today` 時設 `is_active=False`、標所有未封存 SalaryRecord stale
- 串接 step 順序：`mark_appraisal` → `snapshot_leave` → `snapshot_leave.prefill_salary` → `revoke_user` → `generate_certificate`
- 任一 step raise `OffboardingError` 由 caller 負責 rollback

```python
class OffboardingError(Exception):
    def __init__(self, message: str, *, code: str)
```
- `code` 對應 HTTP detail：`EMPLOYEES_NOT_FOUND`、`ALREADY_OFFBOARDED`、`RESIGN_DATE_BEFORE_HIRE`、`RESIGN_DATE_TOO_FAR_FUTURE`、`LEAVE_BALANCE_NOT_FOUND`、`CERTIFICATE_GENERATION_FAILED`

#### `attendance_csv.py`
```python
def generate_attendance_csv(session: Session, employee_id: int, resign_date: date) -> bytes
```
- 過去 12 月（`max(resign_date - 365 days, hire_date)` ~ `resign_date`）AttendanceRecord 匯出 UTF-8 BOM CSV
- 員工不存在 raise `ValueError`

#### `download_bundle.py`
```python
def build_offboarding_zip(session: Session, record: EmployeeOffboardingRecord) -> bytes
```
- 產生 ZIP bundle（DEFLATED 壓縮）：`certificate.pdf` + `salary_<YYYY>_<MM>.pdf × 12` + `attendance.csv`
- 前置條件：`record.certificate_pdf_path is not None` 否則 raise `ValueError`
- 單月薪資 PDF 失敗不擋整 ZIP（try / except 跳過該月）

#### `magic_link.py`
```python
TOKEN_TTL_DAYS = 30
MAX_DOWNLOADS = 3

def hash_token(token: str) -> str                                    # SHA-256 hex
def generate_token(session, record) -> str                           # 回傳明文，DB 存 hash
def verify_token(session, token) -> Optional[EmployeeOffboardingRecord]
def revoke_token(session, record) -> None
def is_active(record) -> bool
def record_download(session, record) -> None

class MagicLinkError(Exception):
    def __init__(self, message: str, *, code: str)
```
- token：`secrets.token_urlsafe(32)`（256-bit base64url random）
- 明文只回一次；DB 只存 sha256 hash
- verify 失敗統一回 `None`（不暴露差異原因，防 enumeration）

#### `steps/mark_appraisal.py`
```python
def run(session, record) -> StepResult
```
- 純寫 `record.appraisal_marked_at = now`；不動 appraisal 資料

#### `steps/snapshot_leave.py`
```python
def _resolve_daily_wage(emp) -> float | None
def run(session, record) -> StepResult                               # snapshot 寫 JSONB
def prefill_salary(session, record) -> StepResult                    # 寫離職當月 SalaryRecord + UnusedLeavePayoutLog
```
- `run`：算特休餘額 + `daily_wage` 折現後寫 `record.leave_balance_snapshot` JSONB
- `daily_wage = emp.daily_wage / daily_salary` 或 `round_half_up(base_salary / 30, 2)` fallback
- 兩者皆 falsy 時 raise `OffboardingError(LEAVE_BALANCE_NOT_FOUND)`
- `prefill_salary`：離職當月 SalaryRecord 不存在 → SKIP；存在則覆寫 `unused_leave_payout`、寫 `UnusedLeavePayoutLog(source_type='offboarding')`、revoke 該員工所有 active `OvertimeCompLeaveGrant`

#### `steps/revoke_user.py`
```python
def run(session, record) -> StepResult
```
- `resign_date > today`：通知期 SKIP；`<= today` 設 `User.is_active=False` + `token_version += 1`（已簽發 cookie 立刻失效）

#### `steps/generate_certificate.py`
```python
def run(session, record) -> StepResult
```
- 呼叫 `services.employee_offboarding_certificate_pdf.generate_certificate_pdf` 產 PDF
- 寫檔至 `storage/offboarding_certificates/{employee_id}_{resign_date}.pdf`
- 磁碟 / 字型 / reportlab 失敗 raise `OffboardingError(CERTIFICATE_GENERATION_FAILED)`

### HTTP 端點（`api/offboarding.py`）

Router prefix：`/api/offboarding`（`main.py:833` 註冊）

| Method | Path | Permission | Request | Response | 說明 |
|--------|------|------------|---------|----------|------|
| `POST` | `/{employee_id}/preview` | `EMPLOYEES_WRITE` (`1<<20`) | `OffboardingPreviewRequest` | `OffboardingPreviewResponse` | 純讀預覽：特休餘額 / daily_wage / payout / 當月 SalaryRecord 是否存在 / User active / warnings |
| `POST` | `/{employee_id}/process` | `EMPLOYEES_WRITE` | `OffboardingProcessRequest` | `OffboardingProcessResponse` | 一鍵離職主處理；失敗依 `_ERROR_TO_STATUS` 映射 4xx/5xx |
| `GET` | `/download?token={token}` | **公開無 auth**（magic-link） | query `token` | StreamingResponse ZIP | 驗 magic-link → 串流 ZIP；失敗統一 410 Gone（防 enumeration） |
| `GET` | `/{employee_id}` | `EMPLOYEES_READ` (`1<<8`) | — | `OffboardingDetailResponse` | 取得離職 checklist 完整紀錄；record 不存在 404 `OFFBOARDING_RECORD_NOT_FOUND` |
| `GET` | `/{employee_id}/certificate.pdf` | `EMPLOYEES_READ` | — | `FileResponse application/pdf` | admin 取離職證明 PDF；record/PDF 不存在 404 |
| `POST` | `/{employee_id}/magic-link` | `EMPLOYEES_WRITE` | — | `MagicLinkResponse` | 產 magic-link（覆寫舊 hash） |
| `DELETE` | `/{employee_id}/magic-link` | `EMPLOYEES_WRITE` | — | `MagicLinkRevokeResponse` | 撤 magic-link |
| `PATCH` | `/{employee_id}/nhi-unenroll` | `EMPLOYEES_WRITE` | `NhiUnenrollRequest` | dict `{employee_id, nhi_unenroll_submitted_at}` | 手動標記健保退保申報；`submitted=true` 寫 now，`false` 清空 |

Error code → HTTP status 映射（`api/offboarding.py:52-59`）：
- `EMPLOYEE_NOT_FOUND` → 404
- `ALREADY_OFFBOARDED` → 409
- `RESIGN_DATE_BEFORE_HIRE` → 400
- `RESIGN_DATE_TOO_FAR_FUTURE` → 400
- `LEAVE_BALANCE_NOT_FOUND` → 422
- `CERTIFICATE_GENERATION_FAILED` → 500

Magic-link 下載 Response Headers：
- `Content-Disposition: attachment; filename="..."; filename*=UTF-8''...`（RFC 5987 ASCII fallback + UTF-8 percent-encoded）
- `X-Content-Type-Options: nosniff`
- `Cache-Control: no-store`

---

## DTO Definitions

### Table `employee_offboarding_records`（`models/offboarding.py`）

| 欄位 | 型別 | 約束 | 說明 |
|------|------|------|------|
| `employee_id` | Integer | PK, FK→employees.id (CASCADE) | one-to-one with Employee |
| `resign_date` | Date | NOT NULL | 離職日（可 > today 為通知期） |
| `resign_reason` | Text | nullable | 離職原因（不寫入證明 PDF） |
| `opened_at` | DateTime | NOT NULL | 紀錄建立時間（`now_taipei_naive()`） |
| `opened_by_user_id` | Integer | NOT NULL, FK→users.id | 操作 admin User.id |
| `user_revoked_at` | DateTime | nullable | revoke_user step audit 戳記 |
| `appraisal_marked_at` | DateTime | nullable | mark_appraisal step audit 戳記 |
| `leave_snapshot_at` | DateTime | nullable | snapshot_leave step 完成時間 |
| `certificate_generated_at` | DateTime | nullable | generate_certificate step 完成時間 |
| `leave_balance_snapshot` | JSONB | nullable | 特休 snapshot：`{snapshot_date, total_hours, used_hours, remaining_hours, remaining_days, daily_wage, payout_amount, calc_rule_version}` |
| `certificate_pdf_path` | Text | nullable | 證明 PDF 絕對路徑 |
| `nhi_unenroll_submitted_at` | DateTime | nullable | 健保退保申報時間 |
| `magic_link_token_hash` | Text | nullable | sha256(token) |
| `magic_link_expires_at` | DateTime | nullable | TTL 30 天 |
| `magic_link_revoked_at` | DateTime | nullable | 撤銷時間 |
| `magic_link_download_count` | Integer | NOT NULL default 0 | 上限 3 次 |
| `magic_link_last_used_at` | DateTime | nullable | 最後下載時間 |
| `closed_at` | DateTime | nullable | 結案時間 [unverified — 目前 codebase 無 caller] |
| `closed_by_user_id` | Integer | nullable, FK→users.id | 結案人 [unverified — 目前 codebase 無 caller] |

Indexes：`ix_offboarding_resign_date`、`ix_offboarding_open_status`（partial WHERE `closed_at IS NULL`）

### Table `unused_leave_payout_log`（`models/unused_leave_payout_log.py`）

| 欄位 | 型別 | 約束 | 說明 |
|------|------|------|------|
| `id` | BigInteger | PK, autoincrement | |
| `employee_id` | Integer | NOT NULL, FK→employees.id (RESTRICT) | |
| `source_type` | String(30) | NOT NULL | 三選一：`comp_grant_expiry` / `annual_anniversary` / `offboarding` |
| `source_ref_id` | Integer | nullable | annual_anniversary→LeaveQuota.id；offboarding→employee_id；comp_grant_expiry→None |
| `hours` | Float | NOT NULL | 未休時數（offboarding 寫 `snap.remaining_hours`） |
| `hourly_wage` | Numeric(10, 2) | NOT NULL | 時薪 |
| `amount` | Numeric(10, 2) | NOT NULL | 折算金額（round_half_up 至元） |
| `wage_basis_date` | Date | NOT NULL | 時薪基準日（offboarding 寫 `resign_date`） |
| `salary_record_id` | Integer | nullable, FK→salary_records.id (SET NULL) | Layer 1 直寫 set；Layer 2 由 engine pull 時 set |
| `salary_period_year` | Integer | NOT NULL | 目標寫入薪資月 |
| `salary_period_month` | Integer | NOT NULL | |
| `meta` | JSONB | NOT NULL default `{}` | offboarding 寫 `{offboarding_record_id, termination_date, snapshot_remaining_days}` |
| `created_at` | DateTime | NOT NULL, server_default `func.now()` | |

Indexes：`ix_payout_log_emp_period (employee_id, salary_period_year, salary_period_month)`、`ix_payout_log_salary_record`（partial WHERE NOT NULL）、`uq_payout_log_anniversary`（partial unique WHERE `source_type='annual_anniversary'`，含 `employee_id`、`source_type`、`source_ref_id`）

### SalaryRecord 相關欄位（`models/salary.py:257`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `unused_leave_payout` | Money | NOT NULL default 0；特休未休折現獨立 column，**不進 `gross_salary`**（仿 `appraisal_year_end_bonus`） |

### Pydantic Schema（`schemas/offboarding.py`）

- `OffboardingPreviewRequest` / `OffboardingProcessRequest` — `{resign_date, resign_reason?}`
- `LeaveSnapshotPreview` — `{special_leave_days, daily_wage, payout_amount}`
- `SalaryRecordTarget` — `{year, month, exists, will_be_marked_stale}`
- `AppraisalInFlightCycle` — `{cycle_id, cycle_name, current_score?}`
- `OffboardingPreview` — `{user_account_will_be_revoked, leave_snapshot, salary_record_target, appraisal_in_flight_cycles, certificate_pdf_ready_to_generate}`
- `OffboardingPreviewResponse` — `{employee_id, employee_name, resign_date, preview, warnings}`
- `StepResultModel` — `{step, status: completed|skipped|failed, completed_at?, payload?, error?}`
- `OffboardingProcessResponse` — `{employee_id, resign_date, is_active, user_account_revoked, steps, certificate_download_url?}`
- `OffboardingDetailResponse` — 完整 record + `magic_link_active` 派生 bool
- `NhiUnenrollRequest` — `{submitted: bool}`
- `MagicLinkResponse` — `{employee_id, token, expires_at, download_url}`
- `MagicLinkRevokeResponse` — `{employee_id, revoked_at}`

---

## Business Rules

### A. 資遣費計算（severance.py）

1. **平均工資（勞基法 §2 第 4 款）**：事由發生當日「前 6 個月」工資總額 ÷ 總日數 × 30。
2. **舊制資遣費（勞基法 §17）**：每滿 1 年發給 1 個月平均工資；剩餘月數按比例；**無上限**。
3. **新制資遣費（勞退條例 §12）**：每滿 1 年發給 0.5 個月平均工資；**上限 6 個月**。
4. **年資**：採 365.25 日／年（含閏年攤分）；`end_date <= hire_date` 回傳 0。
5. **零值守衛**：`service_years <= 0` 或 `avg_monthly_wage <= 0` 回傳 0；空 records 回 0；`Σ days == 0` 回 0。
6. **舊／新制適用條件、薪資基準來源、產品決策觸發點**：尚未定義；接生產前必須補 spec [needs review]。
7. **不負責 rounding**：本 module 純 float；呼叫端必須自行 `round_half_up` 守 .5 邊界（配合 政府/勞健保 ROUND_HALF_UP 標準）。
8. **Dead-code guard**：`tests/test_severance_dead_code_guard.py` 確保 production caller 不悄悄出現；新接生產時必須同步移除此 guard、補 spec。
   - **CI `money-rounding-gate` 涵蓋狀態（2026-05-28 DRIFT check 修正）**：✅ 已涵蓋。`.github/workflows/ci.yml:275` 的 glob `services/salary/**.py` 自然包含 `services/salary/severance.py`，**無需另行 patch CI gate paths**。先前 SPEC v0.1 草稿誤述「尚未列入」，已於 2026-05-28 修正。
   - **接生產 checklist（給另一位開發者）**：
     1. 找到第一個 production caller，把該 callsite 包進 `with try_scheduler_lock(...)` 或同等審計 wrapper（依使用場景）
     2. 刪除 `tests/test_severance_dead_code_guard.py`
     3. 補 unit test 至 `tests/test_severance.py` 涵蓋新 callsite 的整合行為
     4. 確認 CI `money-rounding-gate` job 仍綠（severance.py 內 0 處 builtin `round()`，已符合）
     5. 在本 SPEC 章節 A 末加 Changelog `vX.Y | YYYY-MM-DD | severance.py 接生產：callsite=<file:line>, dead-code guard 已移除`

### B. 未休假折現公式（unused_leave_pay.py）

1. **未休時數** = `max(0.0, entitled_hours − used_hours)`；`None` 容錯為 0。
2. **折算金額** = `unused_hours × hourly_wage`；負值 / 零值 / `None` 一律回 0。
3. **時薪定義**（呼叫端決定）：
   - 月薪制：`base_salary / 30 / 8`（與 `MONTHLY_BASE_DAYS = 30` 對齊 SPEC-001）
   - 時薪制：直接 `emp.hourly_rate`（避免 `base_salary=0` 算出 0 補償）
4. **Rounding 規則**：模組本身**不 round**；所有呼叫端必經 `utils.rounding.round_half_up(raw_amount)` 取整至元（CI `money-rounding-gate` enforce `services/salary/`、`services/finance/`、`services/offboarding/` 等路徑禁用 builtin `round()`）。
5. **三類觸發 source_type**：
   - `annual_anniversary`：特休週年 cutover（`services/leave_quota_expiry/annual_cutover.py`）；scheduler 每日跑；同員工同 `source_ref_id` 部分 unique（partial index）。
   - `comp_grant_expiry`：補休 +1 年到期（`services/leave_quota_expiry/comp_leave_expiry.py`）；scheduler 每日跑。
   - `offboarding`：離職 path（`services/offboarding/steps/snapshot_leave.prefill_salary`）；一鍵離職時寫入。
6. **離職薪資預覽**（`api/employees.py:879-899`）：`resign_date` 落在計算月時，由 `_calc_annual_leave_hours` + `_get_used_hours` 取得 entitled/used，傳入兩個函式算 `unused_annual_compensation`；不寫 DB，僅返回給 UI 預覽。

### C. 任職比例 proration 公式（proration.py）

1. **底薪折算公式**：`contracted_base × worked_days / month_days`，分母為當月自然日數（`calendar.monthrange(year, month)[1]`），分子計算如下：
   - **`_prorate_base_salary`（入職）**：`worked_days = month_days − hire_date.day + 1`（入職日當天計入）
   - **`_prorate_for_period`（入職 + 離職）**：`start_day = max(1, hire.day if hire 在本月且 ≥ 2 else 1)`；`end_day = min(month_days, resign.day if resign 在本月且 < month_days else month_days)`；`worked_days = end_day − start_day + 1`
2. **邊界**：
   - 入職日 ≤ 1 或入職月份 ≠ 計算月份：**全額**，不折算（避免月初到職還扣比例）
   - 入職月份 > 計算月份：回 **0.0**（補算歷史月時尚未到職不發薪）
   - 離職月份 < 計算月份：回 **0.0**（已離職）
   - 同月內 `resign < hire`：raise `ValueError`（防算出負薪）
   - `month ∉ 1–12`：raise `ValueError`
   - `contracted_base < 0`：raise `ValueError`（避免 truthy 守衛 silent 通過）
   - `contracted_base = 0` 或 falsy：回 0.0
   - `hire_date_raw = None`：回 `contracted_base` 全額
3. **「雙重縮水」禁忌**：折算僅影響 `breakdown.base_salary` 顯示；**加班費時薪基準必須用完整 `emp.base_salary / 30 / 8`**（CLAUDE.md 業務不變式同款）。範例（CLAUDE.md 已記）：
   - 錯：折算後底薪 15,000 / 30 / 8 = 62.5 NTD/hr
   - 對：契約月薪 30,000 / 30 / 8 = 125.0 NTD/hr
4. **應上班日集合（`_build_expected_workdays`）優先順序**：未來日不計 → holiday 不計 → `hire_date` 前 / `resign_date` 後不計 → `daily_shift_map` 有排班看 `shift_type_id` 是否非 None → 補班日（`makeup_set`，通常週六的官方補班日）視為應上班 → 否則平日（週一~週五）。
5. **整班漏點名假缺勤過濾**：若某天某班所有 active 學生皆「無紀錄」或皆「缺席」視為老師漏點名，不視為應上班 [unverified — 此規則在 CLAUDE.md analytics 段提及，proration.py 程式碼未實作該過濾，僅 expected_workdays 集合是 per-employee 視角]。

### D. 離職結算稽核流程

1. **觸發點**：`POST /api/offboarding/{employee_id}/process`（gated by `EMPLOYEES_WRITE`）。
2. **input 驗證**（orchestrator.py:68-94）：
   - 員工存在 → 否則 `EMPLOYEE_NOT_FOUND`
   - 無既有 `EmployeeOffboardingRecord`（one-to-one）→ 否則 `ALREADY_OFFBOARDED`
   - `resign_date >= emp.hire_date` → 否則 `RESIGN_DATE_BEFORE_HIRE`
   - `(resign_date − today).days <= 90` → 否則 `RESIGN_DATE_TOO_FAR_FUTURE`
3. **建立 `EmployeeOffboardingRecord`**：寫 `resign_date`、`resign_reason`、`opened_at = now_taipei_naive()`、`opened_by_user_id`。同時更新 `Employee.resign_date` / `resign_reason`；`resign_date <= today` 設 `is_active=False`。
4. **標 stale**：呼叫 `api.employees._mark_employee_salary_stale(session, employee_id)`，所有 unfinalized SalaryRecord 標 stale（proration / daily_wage 改動影響任何未封存月）。
5. **5-step 順序**（同一 transaction）：
   1. `mark_appraisal`：寫 `appraisal_marked_at`
   2. `snapshot_leave`：算特休餘額 + `daily_wage × remaining_days` 折現 → 寫 `leave_balance_snapshot` JSONB；`daily_wage` 為 0 或 None raise `LEAVE_BALANCE_NOT_FOUND`
   3. `snapshot_leave.prefill_salary`：離職當月 SalaryRecord 存在則覆寫 `unused_leave_payout` + 寫 `UnusedLeavePayoutLog(source_type='offboarding')` + revoke 該員工所有 active `OvertimeCompLeaveGrant`；不存在則 SKIP（薪資 calculate 時新建）
   4. `revoke_user`：`resign_date > today` SKIP；`<= today` 設 `User.is_active=False` + `token_version += 1`
   5. `generate_certificate`：產 PDF → 寫 `storage/offboarding_certificates/{employee_id}_{resign_date}.pdf` → 寫 `certificate_pdf_path` / `certificate_generated_at`；磁碟/字型失敗 raise `CERTIFICATE_GENERATION_FAILED`
6. **稽核日誌**：
   - `request.state.audit_entity_id = str(employee_id)` + `audit_summary` 寫入既有 middleware（不直接寫 audit log table）
   - `logger.warning("離職處理完成：employee_id=... resign_date=... operator=...")` 結構化日誌
7. **`UnusedLeavePayoutLog` 寫入時機**（snapshot_leave.prefill_salary:126-143）：當且僅當離職當月 SalaryRecord 存在時寫入；`hourly_wage = round_half_up(daily_wage / 8, 2)`；`amount = snap.payout_amount`；`wage_basis_date = resign_date`。
8. **Layer 2 補撈**（`services/salary/engine._pull_pending_payout_logs`）：月結 calculate 時撈所有 `salary_record_id IS NULL` 的 log，加總寫入 `SalaryRecord.unused_leave_payout` 並反向綁定；filter `IS NULL` 保證 idempotent，不雙重計入。
9. **錯誤處理**：任一 step raise `OffboardingError` → endpoint 層 `session.rollback()` + HTTPException 對應 status。
10. **magic-link 流程**：
    - `POST /magic-link` 產 token（覆寫舊 hash）→ 明文回一次，DB 存 sha256；30 天 TTL；3 次下載上限
    - `GET /download?token=...`（**公開無 auth**）→ verify → `build_offboarding_zip` 串流 ZIP → `record_download` 計數
    - 驗失敗統一 410 `LINK_NO_LONGER_VALID`（不暴露差異原因，防 enumeration）
    - `DELETE /magic-link` 撤銷（保留 hash 行 audit）
11. **健保退保標記**：`PATCH /nhi-unenroll` 手動標 `nhi_unenroll_submitted_at`；目前無自動對接政府 API [unverified — 僅手動 toggle]。

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft：涵蓋 severance / unused_leave_pay / proration 三 module + offboarding orchestration（含 5 step + magic-link + ZIP bundle + UnusedLeavePayoutLog 證據鏈） |
| v0.1 | 2026-05-28 | DRIFT delta：§A.8 修正「severance 尚未列入 CI money-rounding-gate」誤述（實際 `services/salary/**.py` glob 已涵蓋）；補上 severance 接生產時的 5 步 checklist 供另位開發者依循。 |
