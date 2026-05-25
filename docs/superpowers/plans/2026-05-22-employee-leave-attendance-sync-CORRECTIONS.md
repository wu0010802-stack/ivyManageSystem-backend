# Plan Corrections(對齊實際 codebase)

> **每個 implementer 開工前必讀**。`docs/superpowers/plans/2026-05-22-employee-leave-attendance-sync.md` 主 plan 是 spec 設計,但寫入時對既有 codebase 多處 mismatch。本 corrections 列出實際應對齊的 fact,**implementer 以本檔為準**。

## 1. Model 命名與表名

| Plan 寫的 | 實際 codebase | 影響範圍 |
|---|---|---|
| `class AttendanceRecord(Base)` | `class Attendance(Base)` | **全文** import / type hint / docstring |
| `__tablename__ = "attendance_records"` | `__tablename__ = "attendances"` | Migration / raw SQL |
| `from models.attendance import AttendanceRecord` | `from models.attendance import Attendance` | 所有檔 |

`LeaveRecord` / `leave_records` **對齊 plan**,不需改名。

## 2. AttendanceStatus enum

實際定義(`models/attendance.py:25-33`):

```python
class AttendanceStatus(enum.Enum):
    NORMAL = "normal"
    LATE = "late"
    EARLY_LEAVE = "early_leave"
    MISSING_PUNCH = "missing"     # ← plan 寫 MISSING 是錯的
    ABSENT = "absent"
    LEAVE = "leave"                # ← Task 2 已加
```

- 屬性名 `MISSING_PUNCH`(不是 `MISSING`)
- 值小寫(`"normal"`)
- **Migration `ALTER TYPE ... ADD VALUE 'leave'`** 值用**小寫 `'leave'`**

## 3. Attendance.status 是 String(20) 不是 SqlEnum

```python
status = Column(
    String(20), default=AttendanceStatus.NORMAL.value, comment="考勤狀態"
)
```

意味著:
- **Migration 不需要 `ALTER TYPE attendancestatus ADD VALUE`**(因為 Postgres 沒有 attendancestatus ENUM type;只是 VARCHAR(20) 存 enum.value 的字串)
- Task 21 migration 那段 `op.execute("ALTER TYPE ...")` **刪除**
- 不需要 `autocommit_block` 包(沒 enum DDL)
- 比對時用 `Attendance.status == AttendanceStatus.LEAVE.value`(即字串 `"leave"`),**不是** `== AttendanceStatus.LEAVE`(SqlEnum 才可以這樣)

## 4. punch_in_time / punch_out_time 是 DateTime 不是 Time

```python
punch_in_time = Column(DateTime, comment="上班打卡時間")
punch_out_time = Column(DateTime, comment="下班打卡時間")
```

影響:
- `compute_late_minutes_with_leave(punch_in: time, ...)` 簽章用 `time` 沒問題,但 caller 必須先 `att.punch_in_time.time()` 提取 time 再傳
- merge_attendance_with_leave 內呼叫 `compute_late_minutes_with_leave` 前要 `.time()` 轉換
- sync._apply_partial 同樣處理

## 5. confirmed_by 是 String(100) 不是 Integer FK

```python
confirmed_by = Column(String(100), nullable=True, comment="確認操作者")
```

Plan §2 寫 `confirmed_by = Column(Integer, ForeignKey("employees.id"), nullable=True)` 是錯的。**不要動這個欄位**(本 PR 不該動)。

## 6. Attendance 既有 boolean flag 欄(plan 沒提)

```python
is_late = Column(Boolean, default=False, comment="是否遲到")
is_early_leave = Column(Boolean, default=False, comment="是否早退")
is_missing_punch_in = Column(Boolean, default=False, comment="是否未打卡（上班）")
is_missing_punch_out = Column(Boolean, default=False, comment="是否未打卡（下班）")
```

這些 boolean flag 跟 `status` 是冗餘但**並存**。本 PR 不動既有 flag 邏輯,只新增 `leave_record_id` + `partial_leave_hours`。

但**注意**:既有寫入路徑(`api/attendance/records.py`、`api/attendance/upload.py`、`utils/attendance_calc.py`)會同時設 `status` + `is_late` 等 flag。**merge helper 不要動 is_late 等 boolean flag**,只動 `leave_record_id` / `partial_leave_hours` / `late_minutes` / `early_leave_minutes`。

## 7. LeaveRecord 全欄位確認(對齊 plan §3 §6 預期)

```python
class LeaveRecord(Base):
    __tablename__ = "leave_records"
    id, employee_id
    leave_type            # String(20)
    start_date, end_date  # Date
    start_time, end_time  # String(5) "HH:MM"
    leave_hours           # Float default 8
    is_deductible         # Boolean default True
    deduction_ratio       # Float default 1.0  ← salary engine 用這個
    is_hospitalized       # Boolean
    reason, attachment_paths
    is_approved           # Boolean nullable(None=pending)
    approved_by           # String(50)
    rejection_reason      # Text nullable
    source_overtime_id    # Integer FK overtime_records ON DELETE SET NULL
    substitute_*          # 代理人 4 欄(本 PR 不動)
    created_at, updated_at
```

對齊 plan。但 plan 提的 `LeaveType` enum(`models/leave.py:26-34`)在這個 codebase 內**有**(SICK/PERSONAL/MENSTRUAL/ANNUAL/MATERNITY/PATERNITY,**沒有 compensatory**;後者用字串 `"compensatory"`)。`leave_type` 欄位是 `String(20)` 不強制 enum,所以 spec 在 sync 內用字串比對 OK。

## 8. AuditLog 欄位(spec §5 已對齊)

實際欄位(`models/audit.py:12-36`):

| 欄位 | 型 | nullable |
|---|---|---|
| id | Integer PK | |
| user_id | Integer | ✓ |
| username | String(50) | ✓ |
| action | String(20) | ✗ |
| entity_type | String(50) | ✗ |
| entity_id | String(50) | ✓ |
| summary | Text | ✓ |
| changes | Text(JSON) | ✓ |
| ip_address | String(45) | ✓ |
| created_at | DateTime default now | ✗ |

Plan 早期版本寫 `target_type/target_id/detail` 是錯的;**對齊 spec §5 v1.1 的 `entity_type/entity_id/summary`**。
`action` 必填用 'CREATE'/'UPDATE'/'DELETE' 三選一(`entity_id` 是 `String(50)` 不是 Integer,傳 `str(lid)`)。

## 9. test_leaves.py 主檔不存在

實際既有 leaves 測試拆成多檔:`test_leaves_quota.py`、`test_leaves_approve_overlap_block.py`、`test_leaves_finalized_guard.py` 等。

**本 PR 新測試一律建新檔**,**不要嘗試 append 到 `tests/test_leaves.py`**(該檔不存在)。建議新檔命名:
- Task 1:`tests/test_leaves_partial_time_validator.py` ✓ 已建
- Task 12-14:`tests/test_leaves_attendance_sync.py`(plan 命名)
- Task 15-17:`tests/test_attendance_writes_leave_aware.py`(plan 命名)

## 10. update_leave 是 PATCH 還是 PUT?

實際 endpoint 是 **`PUT /api/leaves/{id}`**(不是 PATCH)。Plan 寫的 `client.patch(...)` 改用 `client.put(...)`。

## 11. Python 跑測試命令

主 repo 的 `venv_sec` 用 Python 3.14。**shebang 寫死壞掉的舊路徑**,必須走 `python -m pytest`:

```bash
/Users/yilunwu/Desktop/ivy-backend/venv_sec/bin/python -m pytest ...
```

**不要** `/Users/yilunwu/Desktop/ivy-backend/venv_sec/bin/pytest`(會 "bad interpreter")。

## 12. Pydantic version

Pydantic v2(`from pydantic import model_validator`)。`@model_validator(mode="after")` 收 `self` 並 `return self`,raise `ValueError` 自動轉 422。

## 13. Worktree 隔離

Implementer **必須在 worktree** 工作:
```
/Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/employee-leave-attendance-sync-be
```
Branch:`feat/employee-leave-attendance-sync-2026-05-22-backend`

**不要**碰主 worktree(`/Users/yilunwu/Desktop/ivy-backend` 直接路徑),那邊有 user 的並行 WIP。

## 14. Plan 範例 code import / class 名統一改

- `from models.attendance import AttendanceRecord` → `from models.attendance import Attendance`
- type hint `AttendanceRecord` → `Attendance`
- raw SQL `attendance_records` → `attendances`(migration / scripts)
- `AttendanceStatus.MISSING` → `AttendanceStatus.MISSING_PUNCH`(若有用到)
- spec / plan 用 `status=LEAVE` 概念上等同 `status == AttendanceStatus.LEAVE.value == "leave"`

## 15. 不在 scope 的事

不要去動既有的 `is_late` / `is_early_leave` / `is_missing_punch_in` / `is_missing_punch_out` 邏輯(那是另一支 spec 才會碰的);本 PR 只新增 `leave_record_id` + `partial_leave_hours`。

不要因為 Task N 的 import 註解或邏輯「順手」refactor 既有檔(例如 Task 2 動到 import 格式 / 加 docstring 空行是過頭,本來只該加 enum 一行)。**最小改動**原則。
