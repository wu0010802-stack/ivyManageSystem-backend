# SPEC-009：考勤與打卡

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | api/attendance/、api/punch_corrections.py、services/attendance_parser.py、utils/attendance_{calc,guards,leave_merge}.py、models/attendance.py、models/shift.py |
| Related | SPEC-001（薪資 engine 透過 Attendance 派生欄位扣款）、SPEC-003（時薪基準）、SPEC-004（遲到/早退/缺勤扣款換算）、SPEC-007（ATTENDANCE_READ/WRITE、APPROVALS 權限位）、SPEC-008（補打卡審核推播） |

---

## Overview

考勤模組負責：

1. **打卡上傳**：管理員上傳打卡機 Excel／CSV → 解析 → 比對員工排班（週排班 `ShiftAssignment` / 日排班 `DailyShift` / 員工預設 `work_start_time`/`work_end_time`）→ 寫入 `attendances`。
2. **即時派生欄位**：依排班計算 `is_late` / `is_early_leave` / `is_missing_punch_in` / `is_missing_punch_out` / `late_minutes` / `early_leave_minutes` / `status`，供薪資 engine 扣款讀取。
3. **leave-aware 合併**：寫入 attendance 時即合進當日有效請假單（`merge_attendance_with_leave`），請假涵蓋遲到/早退時段時把 `late_minutes` / `early_leave_minutes` 歸零。
4. **異常確認流程**：員工自助確認（接受扣款／特休抵銷／申訴）或管理員批次代確認（admin_accept／admin_waive）。
5. **補打卡審核**：員工提交補打卡申請 → 管理員核准 → 寫回 `attendances` → 依新打卡時間 `apply_attendance_status` 重算派生欄位。
6. **跨夜班縫合**：解析器自動把次日清晨打卡歸入前一個工作日。
7. **保存期保護**：勞基法第 30 條第 5 項要求出勤紀錄保存 5 年，刪除受 `_assert_attendance_within_retention` 阻擋。
8. **封存連動**：所有改動都會與 `SalaryRecord.is_finalized` 結合 — 已封存月份不可寫入；未封存但已計算過的月份 `lock_and_premark_stale` 標 `needs_recalc=True`。

---

## Interface Definitions

### HTTP 端點 — `api/attendance/`

#### upload.py

| Function | Permission | Request | Response |
|----------|------------|---------|----------|
| `POST /api/attendance/upload` | `ATTENDANCE_WRITE` | `multipart/form-data` 上傳 `.xlsx` 或 `.xls`；新格式需含「上班時間 / 下班時間」欄；舊格式 fallback 至 `parse_attendance_file()` | `{message, summary[], anomaly_count, anomalies[]}` |
| `POST /api/attendance/upload-csv` | `ATTENDANCE_WRITE` | `AttendanceUploadRequest` JSON | `{message, results: {total, success, failed, errors, summary}}` |

#### records.py

| Function | Permission | Request | Response |
|----------|------------|---------|----------|
| `GET /api/attendance/records` | `ATTENDANCE_READ` | Query: `year`, `month`, `employee_id?` | `list[{id, employee_id, employee_name, employee_number, date, weekday, punch_in, punch_out, status, is_late, is_early_leave, is_missing_punch_in, is_missing_punch_out, late_minutes, early_leave_minutes, remark}]` |
| `POST /api/attendance/record` | `ATTENDANCE_WRITE` | `AttendanceRecordUpdate` | 201；`{message, status, is_late, late_minutes, is_early_leave, early_leave_minutes}` |
| `DELETE /api/attendance/record/{employee_id}/{date}` | `ATTENDANCE_WRITE` | path | `{message}` |
| `DELETE /api/attendance/records/{employee_id}/{date_str}` | `ATTENDANCE_WRITE` | path（支援 `YYYY-MM-DD` 與 `YYYY/MM/DD`） | `{message}` |
| `DELETE /api/attendance/records/{year}/{month}` | `ATTENDANCE_WRITE` | path | `{message: "已刪除 N 筆..."}` |

#### reports.py

| Function | Permission | Request | Response |
|----------|------------|---------|----------|
| `GET /api/attendance/today` | `ATTENDANCE_READ` | — | `{date, total_employees, present_count, absent_count, late_count, missing_count}` |
| `GET /api/attendance/summary` | `ATTENDANCE_READ` | Query: `year`, `month` | `list[{employee_id, employee_name, employee_number, total_days, normal_days, late_count, early_leave_count, missing_punch_in, missing_punch_out, total_late_minutes, total_early_minutes}]` |
| `GET /api/attendance/today-anomalies` | `ATTENDANCE_READ` | — | `{date, anomalies: [{employee_id, employee_name, anomaly_type, late_minutes}]}` `anomaly_type ∈ {absent, late, missing_punch}` |
| `GET /api/attendance/anomaly-report` | `ATTENDANCE_READ` | Query: `year`, `month` | `StreamingResponse` Excel；7 欄（員工姓名 / 日期 / 上班打卡 / 下班打卡 / 狀態 / 遲到分鐘 / 早退分鐘） |
| `GET /api/attendance/calendar` | `ATTENDANCE_READ` | Query: `employee_id`, `year`, `month` | `{employee_name, employee_id, year, month, days[], summary: {work_days, late_count, leave_days, overtime_hours}}` |

#### anomalies.py

| Function | Permission | Request | Response |
|----------|------------|---------|----------|
| `GET /api/attendance/anomalies` | `ATTENDANCE_READ` | Query: `year`, `month`, `status ∈ {all, pending, confirmed}` | `{total, pending, confirmed, items[]}` 每 item：`id, employee_name, employee_number, date, weekday, confirmed_action, confirmed_by, confirmed_at, type, type_label, detail, estimated_deduction` |
| `POST /api/attendance/anomalies/batch-confirm` | `ATTENDANCE_WRITE` | `BatchConfirmRequest` | `{processed}` |
| `GET /api/attendance/anomalies/export` | `ATTENDANCE_READ` | Query 同 GET anomalies | `StreamingResponse` Excel；11 欄（員工編號 / 姓名 / 日期 / 星期 / 異常類型 / 明細 / 預估扣款 / 確認狀態 / 確認動作 / 確認人員 / 確認時間） |

### HTTP 端點 — `api/punch_corrections.py`

| Function | Permission | Request | Response |
|----------|------------|---------|----------|
| `GET /api/punch-corrections` | `APPROVALS` | Query: `status?`, `year?`, `month?`, `employee_id?` | `list[{id, employee_id, employee_name, attendance_date, correction_type, correction_type_label, requested_punch_in, requested_punch_out, reason, approval_status, approved_by, rejection_reason, created_at}]` |
| `PUT /api/punch-corrections/{correction_id}/approve` | `APPROVALS` | `ApproveRequest{approved, rejection_reason?}` | `{message}`；副作用：核准時寫 `attendances`（含 `apply_attendance_status` 重算） + `mark_salary_stale` + 推 `punch_correction.approved/rejected` 通知 |

### HTTP 端點 — `api/portal/` 相關（教師自助）

| Function | Auth | Path | Notes |
|----------|------|------|-------|
| `get_attendance_sheet` | `get_current_user` | `GET /portal/attendance-sheet` | 個人月考勤表（含請假/加班/節日標記） |
| `print_attendance_sheet_pdf` | `get_current_user` | `GET /portal/attendance-sheet.pdf` | A4 橫向 PDF |
| `get_anomalies` | `get_current_user` | `GET /portal/anomalies` | 個人月異常清單，含預估扣款 |
| `confirm_anomaly` | `get_current_user` | `POST /portal/anomalies/{attendance_id}/confirm` | `action ∈ {accept, use_pto, dispute}` |
| `get_my_punch_corrections` | `get_current_user` | `GET /portal/my-punch-corrections` | 個人補打卡申請紀錄 |
| `create_my_punch_correction` | `get_current_user` | `POST /portal/my-punch-corrections` | 提交補打卡申請；201 |

### 內部 Python 函式

#### `services/attendance_parser.py`

```python
@dataclass
class AttendanceResult:
    employee_name: str
    total_days: int
    normal_days: int
    late_count: int
    early_leave_count: int
    missing_punch_in_count: int
    missing_punch_out_count: int
    total_late_minutes: int
    total_early_minutes: int
    details: list[dict]

class AttendanceParser:
    DEFAULT_WORK_START = "08:00"
    DEFAULT_WORK_END = "17:00"
    LATE_GRACE_MINUTES = 5
    OVERNIGHT_START = time(16, 0)
    OVERNIGHT_END = time(6, 0)

    def __init__(self, employee_schedules: dict[str, dict] = None): ...
    def parse_attendance_excel(self, file_path, name_column="姓名",
                                datetime_column="時間",
                                date_column=None, time_column=None
                                ) -> dict[str, AttendanceResult]: ...
    def _stitch_overnight_punches(self, employee_df) -> tuple[pd.DataFrame, set]: ...
    def _analyze_employee_attendance(self, employee_name, employee_df
                                      ) -> AttendanceResult: ...
    def generate_anomaly_report(self, results) -> pd.DataFrame: ...
    def generate_summary_report(self, results) -> pd.DataFrame: ...

def parse_attendance_file(file_path, employee_schedules=None,
                          name_column="姓名", datetime_column="時間"
                          ) -> tuple[dict[str, AttendanceResult],
                                      pd.DataFrame, pd.DataFrame]:
    """便捷函式：解析 + 異常表 + 摘要表"""
```

#### `utils/attendance_calc.py`

```python
DEFAULT_WORK_START = "08:00"
DEFAULT_WORK_END = "17:00"

class AttendanceStatusFields(TypedDict):
    is_late: bool
    is_early_leave: bool
    is_missing_punch_in: bool
    is_missing_punch_out: bool
    late_minutes: int
    early_leave_minutes: int
    status: str

def recompute_attendance_status(
    *, attendance_date: date,
    punch_in_time: datetime | None,
    punch_out_time: datetime | None,
    work_start_str: str | None,
    work_end_str: str | None,
) -> AttendanceStatusFields:
    """純函式：依 punch + 排班時間重算派生欄位（不含 grace minutes）。"""

def apply_attendance_status(
    attendance,
    *, work_start_str: str | None,
    work_end_str: str | None,
    session: Session | None = None,
) -> AttendanceStatusFields:
    """讀寫一體：重算並寫回 ORM 物件；傳 session 則同時 merge_attendance_with_leave。"""

def compute_late_minutes_with_leave(
    punch_in: time, scheduled_start: time,
    leave_start: time | None, leave_end: time | None,
) -> int:
    """leave-aware 遲到分鐘：請假涵蓋 scheduled_start 時 effective_start = leave_end。"""

def compute_early_leave_minutes_with_leave(
    punch_out: time, scheduled_end: time,
    leave_start: time | None, leave_end: time | None,
) -> int:
    """leave-aware 早退分鐘：請假涵蓋 scheduled_end 時 effective_end = leave_start。"""
```

#### `utils/attendance_guards.py`

```python
def require_not_self_attendance(
    current_user: dict, target_employee_id: int,
    *, detail: str = "不可修改／刪除自己的考勤紀錄",
) -> None:
    """單筆寫入自我守衛（F-041）；None-safe，純管理帳號放行。"""

def assert_no_self_in_batch(
    current_user: dict, employee_ids,
    *, detail: str = "批次操作不可包含自己的考勤紀錄",
) -> None:
    """批次寫入自我守衛（F-046）；含 caller 自己整批 403。"""
```

#### `utils/attendance_leave_merge.py`

```python
DEFAULT_SCHEDULED_START = time(9, 0)
DEFAULT_SCHEDULED_END = time(18, 0)

def merge_attendance_with_leave(att: Attendance, session: Session) -> None:
    """In-place 合併當日有效 leave 至 attendance。
    決策表 6 case：無 leave / 全天無打卡 / 全天有打卡 / 部分有打卡 / 部分無打卡 / 同日多筆取最早 id。
    純讀 session，不把 att 加入 session。"""

def _is_full_day(leave: LeaveRecord) -> bool: ...
def _parse_hhmm(s: str | None) -> time | None: ...
def _get_employee_schedule(session, employee_id: int) -> tuple[time, time]: ...
```

#### `api/attendance/records.py` 私有 helper

```python
ATTENDANCE_RETENTION_YEARS = 5

def _retention_cutoff(today: date) -> date: ...
def _assert_attendance_within_retention(attendance_date: date,
                                         today: date | None = None) -> None: ...
def _assert_attendance_not_finalized(session, employee_id: int,
                                      attendance_date: date) -> None: ...
def _assert_month_no_finalized_salary(session, year: int, month: int) -> None: ...
def _assert_upload_months_not_finalized(session, emp_ids: set, dates: set) -> None: ...
```

#### `api/attendance/anomalies.py` 私有 helper

```python
ACTION_LABELS = {
    "accept": "接受扣款", "admin_accept": "接受扣款",
    "use_pto": "特休抵銷", "dispute": "申訴中",
    "admin_waive": "管理員豁免",
}
WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]

def _build_anomaly_rows(session, year: int, month: int,
                         status_filter: str) -> list[dict]: ...
```

#### `api/punch_corrections.py` 私有 helper

```python
CORRECTION_TYPE_LABELS = {
    "punch_in": "補上班打卡",
    "punch_out": "補下班打卡",
    "both": "補全天打卡",
}

def _format_correction(c: PunchCorrectionRequest,
                        employee_name: str = "") -> dict: ...
```

---

## DTO Definitions

### `Attendance`（DB 模型 `models/attendance.py`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `Integer PK` | — |
| `employee_id` | `Integer FK employees.id NOT NULL` | — |
| `attendance_date` | `Date NOT NULL` | 考勤日期 |
| `punch_in_time` | `DateTime` | 上班打卡時間（跨夜班時可能與 date 不同） |
| `punch_out_time` | `DateTime` | 下班打卡時間（跨夜班可為次日） |
| `status` | `String(20)` default `"normal"` | `AttendanceStatus` 之一（含 `+missing` 等複合字串） |
| `is_late` | `Boolean` default `False` | 派生：遲到旗標 |
| `is_early_leave` | `Boolean` default `False` | 派生：早退旗標 |
| `is_missing_punch_in` | `Boolean` default `False` | 派生：上班未打卡 |
| `is_missing_punch_out` | `Boolean` default `False` | 派生：下班未打卡 |
| `late_minutes` | `Integer` default `0` | 派生：遲到分鐘數（leave-aware 後可歸零） |
| `early_leave_minutes` | `Integer` default `0` | 派生：早退分鐘數 |
| `remark` | `Text` | 備註（部門、Legacy Upload、確認標記等） |
| `confirmed_action` | `String(20)` | `accept/use_pto/dispute/admin_accept/admin_waive` |
| `confirmed_by` | `String(100)` | 確認操作者 username |
| `confirmed_at` | `DateTime` | 確認時間（Taipei naive） |
| `leave_record_id` | `Integer FK leave_records.id ON DELETE SET NULL` | leave-aware 合併寫入 |
| `partial_leave_hours` | `Numeric(4,2)` | 部分假時數（null = 全天或無假） |
| `created_at` | `DateTime` default `now_taipei_naive` | — |
| `updated_at` | `DateTime` default `now_taipei_naive` onupdate `now_taipei_naive` | — |

索引與限制：
- `Index ix_attendance_emp_date (employee_id, attendance_date)`
- `Index ix_attendance_date (attendance_date)`
- `Index ix_attendance_anomaly (attendance_date, is_late, is_early_leave, is_missing_punch_in, is_missing_punch_out)`
- `UniqueConstraint uq_attendance_employee_date (employee_id, attendance_date)`

### `AttendanceStatus`（Enum）

| 值 | 說明 |
|----|------|
| `normal` | 正常出勤 |
| `late` | 遲到 |
| `early_leave` | 早退 |
| `missing` | 缺打卡 |
| `absent` | 未出勤（部分請假但無打卡時） |
| `leave` | 全天請假（請假同步寫入考勤） |

**注意**：實際 `status` 欄位可出現複合字串如 `"late+early_leave"`、`"missing+missing_in"`、`"late+missing_out"`，由 upload 端與 `recompute_attendance_status` 用 `+` 串接，不全屬枚舉值。

### `PunchCorrectionRequest`（DB 模型 `models/overtime.py:81`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `Integer PK` | — |
| `employee_id` | `Integer FK NOT NULL` | — |
| `attendance_date` | `Date NOT NULL` | 欲補打的日期 |
| `correction_type` | `String(20) NOT NULL` | `punch_in / punch_out / both` |
| `requested_punch_in` | `DateTime` | 申請的上班時間 |
| `requested_punch_out` | `DateTime` | 申請的下班時間 |
| `reason` | `Text` | 說明原因 |
| `status` | `String(20) NOT NULL` server_default `"pending"` | `pending / approved / rejected`（P1 dual-write SoT） |
| `approved_by` | `String(50)` | 核准人 username |
| `rejection_reason` | `Text` | 駁回原因 |
| `created_at` / `updated_at` | `DateTime` | Taipei naive |

額外屬性：
- `approval_status` (property) → 直接回傳 `status`（內部 SoT 切換相容層）

索引：
- `Index ix_punch_correction_emp_date (employee_id, attendance_date)`
- `Index ix_punch_correction_status (status, attendance_date)`

### `ShiftType` / `ShiftAssignment` / `DailyShift`（`models/shift.py`）

排班來源優先順序（高至低）：

1. **`DailyShift`** — 單日明確排班（亦可 NULL `shift_type_id` 表「明確排休，不繼承週排班」）
2. **`ShiftAssignment`** — 週排班（key = `employee_id` + `week_start_date`，週一為 key）
3. **`Employee.work_start_time` / `work_end_time`** — 員工預設排班
4. fallback `"08:00"` / `"17:00"`（`attendance_calc.DEFAULT_WORK_*`）或 `"09:00"` / `"18:00"`（`attendance_leave_merge.DEFAULT_SCHEDULED_*`） `[needs review: 兩個預設值不一致]`

排班僅在「主班教師（`Classroom.head_teacher_id`）/ 助理教師（`Classroom.assistant_teacher_id`）」時被應用；其他職務（含「司機」）走員工預設與「最小總工時」判定。

### Pydantic Schemas

#### `_shared.py`

| Schema | 欄位 |
|--------|------|
| `AttendanceCSVRow` | `department: str`, `employee_number: str`, `name: str`, `date: str`, `weekday: str`, `punch_in: str?`, `punch_out: str?` |
| `AttendanceUploadRequest` | `records: list[AttendanceCSVRow]`, `year: int`, `month: int` |
| `AttendanceRecordUpdate` | `employee_id: int`, `date: str`, `punch_in: str?`, `punch_out: str?` |

#### `anomalies.py`

| Schema | 欄位 |
|--------|------|
| `BatchConfirmRequest` | `attendance_ids: list[int]`, `action: str ∈ {admin_accept, admin_waive}`, `remark: str?` |

#### `punch_corrections.py`（管理員）

| Schema | 欄位 |
|--------|------|
| `ApproveRequest` | `approved: bool`, `rejection_reason: str?` |

#### `portal/punch_corrections.py`（員工自助）

| Schema | 欄位 / 驗證 |
|--------|-----------|
| `PunchCorrectionCreate` | `attendance_date: date`、`correction_type ∈ {punch_in, punch_out, both}`、`requested_punch_in: datetime?`、`requested_punch_out: datetime?`、`reason: str?`；`@model_validator`：日期不可未來、依 type 強制對應 punch 時間必填 |

#### `portal/_shared.py`

| Schema | 欄位 |
|--------|------|
| `AnomalyConfirm`（推測自 `confirm_anomaly` 使用）`[unverified]` | `action: str ∈ {accept, use_pto, dispute}`、`remark: str?` |

---

## Business Rules

### R-001 打卡解析格式

兩種上傳路徑：

1. **新格式 Excel**（`/api/attendance/upload`，columns 含「上班時間」與「下班時間」）：
   - 必要欄位：`部門`、`編號`、`姓名`、`日期`、`星期`、`上班時間`、`下班時間`
   - 員工 lookup：先 `編號` → 再 `姓名`（兩者皆無 → 「找不到員工」失敗）
   - `編號` 字尾 `.0` 自動移除（pandas 浮點型轉字串副作用）
   - `日期` 支援 `YYYY/MM/DD` 與 `YYYY-MM-DD`
   - `上班時間` / `下班時間` 解析 `HH:MM`（支援小數秒，會被截）；解析失敗 logger.warning 而非整列失敗

2. **舊格式 Excel**（fallback）：`services/attendance_parser.AttendanceParser`，欄位 `姓名`、`時間`（單一 datetime 欄）；遲到判定使用 5 分鐘寬限。

3. **CSV (JSON 包裝)**（`/api/attendance/upload-csv`）：透過 `AttendanceUploadRequest` 提交，欄位對齊新格式 Excel。

### R-002 遲到 / 早退 / 缺打卡 / 曠職判定

| 判定 | 規則 |
|------|------|
| `is_late` | 新格式 upload / records.py：`punch_in_time > work_start_dt`（**無寬限**）；解析器 `AttendanceParser`：`punch_in > grace_time = work_start + 5 min` |
| `late_minutes` | `(punch_in_time - work_start_dt).total_seconds() / 60`，`max(0, ...)` |
| `is_early_leave` | `punch_out_time < work_end_dt`（跨夜班則 `work_end_dt += 1 day`） |
| `early_leave_minutes` | `(work_end_dt - punch_out_time).total_seconds() / 60`，`max(0, ...)` |
| `is_missing_punch_in` | `punch_in_time is None` |
| `is_missing_punch_out` | `punch_out_time is None` |
| `status` 複合字串 | 由初始 `normal` 依序疊加 `late` / `early_leave` / `missing` / `missing_in` / `missing_out`，用 `+` 串接 |

**寬限期不一致**：`AttendanceParser` 含 5 分鐘寬限；新 upload 路徑與 `recompute_attendance_status` 無寬限。`[needs review: 業務應確認哪條為 canonical]`

**異常確認預估扣款**（`_build_anomaly_rows` / portal anomalies）：
- 遲到：`round_half_up(daily_salary / 8 / 60 * late_minutes)`；`daily_salary = base_salary / MONTHLY_BASE_DAYS`（admin 端，見 `calc_daily_salary`）或 `base_salary / get_working_days(year, month)`（portal 端）`[needs review: admin/portal 換算基準不同]`
- 早退：固定 `50` 元
- 未打卡：`0`（僅記錄）

### R-003 跨夜班處理

**入庫時**（records.py、upload.py、upload-csv 三處共用）：

```
if punch_in_time and punch_out_time and punch_out_time < punch_in_time:
    punch_out_time += timedelta(days=1)
elif punch_in_time and punch_out_time and punch_out_time == punch_in_time:
    raise 400 "上下班時間相同"
```

**排班比對時**：若 `shift_end_dt <= shift_start_dt`（如 `02:00 ≤ 18:00`），`shift_end_dt += 1 day`。

**解析器層（`AttendanceParser._stitch_overnight_punches`）**：跨夜縫合規則
- Day N 最早一筆打卡 `< 06:00` 且 Day N-1 只有 1 筆 `≥ 16:00` 的打卡 → 將 Day N 該筆 punch_date 改為 Day N-1
- Day N 移走後若無剩餘 → Day N 加入 `absorbed_dates`，不獨立計算為一個工作日

### R-004 排班套用與工時門檻

採用順序（upload.py、portal/attendance.py）：

1. `daily_shift_map.get((employee_id, date))` — 日排班優先
2. **僅當員工是主班/助理教師**：`shift_schedule_map.get((employee_id, week_monday))` — 週排班
3. 否則：`employee.work_start_time` / `work_end_time`，預設 `08:00` / `17:00`

**最小總工時 fallback**（無排班 + 有打卡）：

| 角色 | 最小總工時（分鐘） |
|------|------------------|
| 司機（`"司機" in employee.title_name`） | `480`（8 小時） |
| 其他 | `540`（9 小時） |

當 `duration_minutes >= required_duration` → `status = "normal"` 並把 `is_late / is_early_leave / late_minutes / early_leave_minutes` 全部歸零。

### R-005 leave-aware 合併（`merge_attendance_with_leave`）

寫入 attendance 端負責 leave-awareness。決策表 6 case：

| Case | 條件 | 行為 |
|------|------|------|
| 1 | 無 leave | 清 `leave_record_id` / `partial_leave_hours`；保留 caller 算好的 status/late_minutes |
| 2 | 全天 + 無打卡 | `status = LEAVE`，清 leave_*，`late/early_leave_minutes = 0`，寫 `leave_record_id` |
| 3 | 全天 + 有打卡 | `partial_leave_hours = 0`，寫 `leave_record_id`，保留 caller 算好的 status/late_minutes |
| 4 | 部分 + 有打卡 | 寫 `partial_leave_hours = leave.leave_hours`，以 `compute_*_with_leave` 重算 late/early；若兩者皆 0 且 status 是 `LATE`/`EARLY_LEAVE` → 退回 `NORMAL` |
| 5 | 部分 + 無打卡 | `status = ABSENT`，寫 `leave_record_id` + `partial_leave_hours`，late/early = 0 |
| 6 | 同日多筆 leave | `order_by(LeaveRecord.id)` 取最早 |

**全天定義** `_is_full_day`：`start_time IS NULL AND end_time IS NULL AND (leave_hours IS NULL OR leave_hours >= 8)`。

**leave 過濾**：僅 `ApprovalStatus.APPROVED` 的請假單會合併。

### R-006 補打卡審核 state machine

```
                提交（員工自助 POST /portal/my-punch-corrections）
                       │ 防重複：同日 status != REJECTED 已存在 → 400
                       ▼
                  status = PENDING
                       │
        ┌──────────────┴──────────────┐
   APPROVE (PUT /api/punch-corrections/{id}/approve, approved=true)
                       │
                       │ 守衛：
                       │   - with_for_update() 列鎖
                       │   - 必須 PENDING；否則 400 "已核准/已駁回"
                       │   - F-015：approver.employee_id == correction.employee_id → 403
                       │   - 角色資格 (_check_approval_eligibility)
                       │   - acquire_salary_lock(emp, year, month)
                       │   - assert_months_not_finalized
                       │
                       ▼
       upsert Attendance.punch_in_time / punch_out_time
       apply_attendance_status (含 leave-aware merge)
       mark_salary_stale
       enqueue notification: punch_correction.approved
                       ▼
                  status = APPROVED   approved_by = current_user.username
                       │
                       └──── 終態

   REJECT (PUT /api/punch-corrections/{id}/approve, approved=false)
                       │
                       │ 422 if not rejection_reason.strip()
                       ▼
       enqueue notification: punch_correction.rejected
       status = REJECTED   approved_by = username
       rejection_reason = ...
                       │
                       └──── 終態
```

`correction_type` 對應寫入欄位：

| correction_type | 寫入 |
|----------------|------|
| `punch_in` | `att.punch_in_time = requested_punch_in` |
| `punch_out` | `att.punch_out_time = requested_punch_out` |
| `both` | 同時寫入 |

通知 dispatch 必須在 `session.commit()` **之前** enqueue，否則 `after_commit` hook 不會 fan-out（PR-D bug fix 註解明示）。

### R-007 異常確認 state machine

員工側（`POST /portal/anomalies/{attendance_id}/confirm`）：

| action | 行為 |
|--------|------|
| `accept` | `remark += " [已確認接受扣款]"`；`confirmed_action = "accept"` |
| `use_pto` | 建立 `LeaveRecord(annual, leave_hours=8, status=REJECTED)` `[needs review: 註解寫「待主管核准」但 status 寫死 REJECTED，疑似 bug]`；`remark += " [已申請特休抵銷]"` |
| `dispute` | `remark += " [申訴: ...]"` |
| 其他 | 400 |

`confirmed_at = now_taipei_naive()`，`confirmed_by = emp.name`。

管理員側（`POST /api/attendance/anomalies/batch-confirm`）：

| action | 行為 |
|--------|------|
| `admin_accept` | 僅標旗，不重算薪資 |
| `admin_waive` | 標旗 + 收集 `(employee_id, year, month)` 走 `lock_and_premark_stale` 觸發薪資重算 |
| 其他 | 400 |

守衛：
- `assert_no_self_in_batch`（F-042）— 整批含 caller 自己整批 403
- 封存月份（任一 target 已封存）→ 整批 409
- `lock_and_premark_stale` 在 commit 前執行，避免 finalize race

### R-008 自我守衛矩陣（IDOR audit Phase 2）

| Finding | 場景 | 守衛 |
|---------|------|------|
| F-015 | 補打卡核准 | `approver.employee_id == correction.employee_id` → 403 |
| F-041 | 單筆 attendance 寫入／刪除 | `require_not_self_attendance` |
| F-042 | 批次 anomaly confirm | `assert_no_self_in_batch` |
| F-046 | bulk upload（Excel/CSV 兩路徑皆套用） | `assert_no_self_in_batch` |

純管理帳號（`employee_id is None`）一律放行。

### R-009 出勤紀錄保存期（勞基法第 30 條第 5 項）

```python
ATTENDANCE_RETENTION_YEARS = 5
```

`_assert_attendance_within_retention`：`attendance_date >= today - 5 years` → 400「不得刪除」。閏年 2/29 退回 2/28。三個刪除端點皆呼叫：
- `DELETE /api/attendance/record/{employee_id}/{date}`
- `DELETE /api/attendance/records/{employee_id}/{date_str}`
- `DELETE /api/attendance/records/{year}/{month}`（以 `end_date` 做最嚴格保護）

### R-010 薪資封存連動

任何 attendance 寫入／刪除（單筆 / 批次 confirm / bulk upload / 補打卡核准）：

1. 寫入前：`_assert_attendance_not_finalized` / `_assert_upload_months_not_finalized` / `_assert_month_no_finalized_salary` / `assert_months_not_finalized` — 已封存月份 409 阻擋
2. 寫入時：`lock_and_premark_stale(session, emp_id, {(year, month)})` — 同時取 advisory lock + 標 `needs_recalc=True`，封住「來源檢查通過 → mark_stale → caller commit」之間 finalize 搶先封存的 race window
3. 補打卡核准額外於核准前 `acquire_salary_lock`，讓「封存守衛 → 改 attendance → mark_stale → commit」在同一鎖窗

### R-011 整班漏點名假缺勤過濾

CLAUDE.md 提及：若某天某班所有 active 學生皆無紀錄或皆「缺席」，視為老師當日漏點名。
**注意**：此規則出現在 `services/analytics/` 對學生出勤的處理脈絡，並非本 SPEC 涵蓋的員工考勤模組。`[unverified: 本 SPEC scope 內未發現對應實作]`

### R-012 跨夜班 punch_out 顯示策略

`portal/attendance.py:307` 對 DB 中異常資料（punch_out ≤ punch_in）也補一天 `effective_out += 1 day`，並從工時扣除 `_calc_lunch_overlap_hours`（呼叫 `services.salary_engine`）。

### R-013 上傳檔案大小與簽章驗證

- 副檔名白名單：`{".xlsx", ".xls"}`
- `read_upload_with_size_check`：上限 `MAX_UPLOAD_SIZE = 10 MB`
- `validate_file_signature`：magic byte 驗證
- 暫存：`storage.get_backend().save(...)` → 解析完 `backend.delete(...)`（無論成功與否；finally 區塊）

### R-014 批次查詢 LIMIT

所有 GET 查詢套 `.limit(5000)` 保護記憶體：
- `/api/attendance/records`
- `/api/attendance/anomalies` 內部 query
- `/api/attendance/anomaly-report`
- `/api/punch-corrections`

### R-015 時區契約

所有 `now_*_naive()` 寫入時間（`confirmed_at`、`created_at`、`updated_at`）一律走 `utils.taipei_time.now_taipei_naive()`；查詢 today 走 `today_taipei()`。對齊全域 datetime 寫入契約（CLAUDE.md）。

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft |
