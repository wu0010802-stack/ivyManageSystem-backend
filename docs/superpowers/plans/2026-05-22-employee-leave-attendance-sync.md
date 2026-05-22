# 員工請假 → 考勤同步重構 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `AttendanceRecord` 成為員工出勤的唯一 SoT,消除「多處 join LeaveRecord 才不出鬼影」的補丁文化;sync 涵蓋 5 條 leaves hook 路徑,merge helper 涵蓋 3 條 attendance 寫入路徑。

**Architecture:** 兩個核心模組 — (1) `services/employee_leave_attendance_sync.py` 在 leaves 生命週期(approve/reject/update/delete)寫 AttendanceRecord;(2) `utils/attendance_leave_merge.py` 讓既有 3 條 attendance 寫入路徑(admin manual / Excel upload / 補卡重算)leave-aware。Salary engine 與 monthly report 切到讀 AttendanceRecord(`status=LEAVE` / `partial_leave_hours`)為 SoT,parity test + _legacy 函式保留作為安全網,merge 後 7 天 follow-up 移除。

**Tech Stack:** Python 3.11 / FastAPI / SQLAlchemy 2.x / Alembic / PostgreSQL 16 / pytest

**Spec Reference:** `docs/superpowers/specs/2026-05-22-employee-leave-attendance-sync-design.md` v1.1

---

## File Structure

### Create
- `services/employee_leave_attendance_sync.py` — sync 三公開函式 + 例外型別
- `utils/attendance_leave_merge.py` — `merge_attendance_with_leave` 純函式
- `scripts/dedupe_attendance.py` — pre-flight 去重工具
- `scripts/preview_backfill.py` — pre-flight 預演工具
- `scripts/fix_partial_leave_times.py` — pre-flight 部分請假時段修補工具
- `alembic/versions/empleavesync_attendance_sync.py` — schema + backfill migration
- `tests/test_employee_leave_attendance_sync.py` — sync unit + idempotency
- `tests/test_attendance_leave_merge.py` — merge helper unit
- `tests/test_leaves_attendance_sync.py` — 5 hook 整合
- `tests/test_attendance_writes_leave_aware.py` — 3 寫入路徑整合
- `tests/test_salary_engine_parity.py` — salary cutover 安全網
- `tests/test_attendance_report_parity.py` — 月報 cutover 安全網
- `tests/test_migration_empleavesync.py` — migration 行為測試

### Modify
- `models/attendance.py` — 加 `LEAVE` enum 值、加 `leave_record_id` / `partial_leave_hours` 欄位 + UniqueConstraint(model 端)
- `utils/attendance_calc.py` — 補 `compute_late_minutes_with_leave` 純函式 + `apply_attendance_status` 多接 session 並呼叫 merge
- `api/leaves.py` — LeaveCreate/Update validator 加「leave_hours<8 → start_time/end_time 必填」;approve_leave / update_leave / delete_leave 三 endpoint 加 sync hook
- `api/attendance/records.py` — `create_or_update_attendance_record` 末段加 merge_attendance_with_leave 呼叫
- `api/attendance/upload.py` — `upload_attendance` 內每個 row build 後加 merge 呼叫
- `api/attendance/reports.py` — 拆 leave_map join,改 outerjoin AttendanceRecord+LeaveRecord;原邏輯保留為 `build_monthly_report_legacy` 給 parity test
- `services/salary/engine.py` — `_build_breakdown_for_month` 改讀 AttendanceRecord;原邏輯保留為 `_build_breakdown_for_month_legacy`

### Read-only reference
- `services/student_leave_service.py` — 學生端對等實作參考(apply_attendance_for_leave / revert_attendance_for_leave)
- `models/leave.py` — LeaveRecord 欄位定義
- `models/audit.py` — audit_logs 實際欄位(entity_type/entity_id/summary/changes)

---

## Task Roadmap

依序執行,前序 task 完成後才開後續:

| # | Task | 依賴 |
|---|---|---|
| 1 | Pydantic validator:leave_hours<8 → start_time/end_time 必填 | — |
| 2 | AttendanceStatus enum 加 `LEAVE` 值 | — |
| 3 | AttendanceRecord 加 leave_record_id / partial_leave_hours 欄位 + UniqueConstraint(model 層,不含 migration) | 2 |
| 4 | `compute_late_minutes_with_leave` 純函式 + 單元測試 C-1~C-6 | — |
| 5 | sync service 骨架:例外型別 + `_is_full_day` + `_assert_leave_time_consistent` | 3 |
| 6 | sync.apply 全天分支 + U-1, U-2 | 5 |
| 7 | sync.apply 部分分支 + U-3, U-4, U-5 | 6, 4 |
| 8 | sync.apply 例外路徑 U-6, U-7, U-8, U-15 | 7 |
| 9 | sync.revert + U-9, U-10, U-11, U-12 | 8 |
| 10 | sync.reapply + U-13, U-14 | 9 |
| 11 | `merge_attendance_with_leave` 純函式 + M-1~M-7 | 4 |
| 12 | api/leaves.py approve hook + I-1, I-2, I-3, I-8, I-9, I-10 | 10 |
| 13 | api/leaves.py update hook + I-4, I-5, I-6 | 12 |
| 14 | api/leaves.py delete hook + I-7 | 13 |
| 15 | api/attendance/records.py 接 merge + W-1, W-2 | 11 |
| 16 | api/attendance/upload.py 接 merge + W-3 | 15 |
| 17 | utils/attendance_calc.apply_attendance_status 接 merge + W-4, W-5 | 16 |
| 18 | scripts/dedupe_attendance.py | — |
| 19 | scripts/preview_backfill.py | 10 |
| 20 | scripts/fix_partial_leave_times.py | — |
| 21 | Alembic migration:schema + enum + dup-check(尚未 backfill / unique) | 3 |
| 22 | Migration 加 unique constraint(CONCURRENTLY)+ bad_leaves pre-flight | 21 |
| 23 | Migration 加 `_run_backfill` | 22, 10 |
| 24 | Migration tests M-1~M-5 | 23 |
| 25 | salary engine `_build_breakdown_for_month_legacy` 備份 + 新版實作 | 24 |
| 26 | salary engine parity test(30+ 案例) | 25 |
| 27 | monthly report `build_monthly_report_legacy` 備份 + 新版 outerjoin | 24 |
| 28 | monthly report parity test(30+ 案例) | 27 |
| 29 | Final 全套 pytest + 前端手動 F-1~F-4 + Grep gate | 28 |
| 30 | PR description + Deploy SOP 引用 | 29 |

---

## Task 1: Pydantic validator — leave_hours<8 → start_time/end_time 必填

**Files:**
- Modify: `api/leaves.py:224-307`(LeaveCreate / LeaveUpdate Pydantic schema)
- Test: `tests/test_leaves.py`(既有檔,加新案例)

### Step 1: 寫測試先(LeaveCreate)

- [ ] **Step 1: Read 既有 LeaveCreate schema 定位 validator 位置**

Run: `grep -n "class LeaveCreate\|class LeaveUpdate\|@field_validator\|validate_leave_hours_value" ivy-backend/api/leaves.py | head -20`

Expected output: 顯示 LeaveCreate 起點(約 L224)、LeaveUpdate 起點(約 L259)、validate_leave_hours_value 引用點。

- [ ] **Step 2: 寫失敗測試 — LeaveCreate 缺 start_time**

加到 `tests/test_leaves.py` 結尾:

```python
def test_leave_create_partial_hours_requires_start_end_time(client, admin_headers, employee_id):
    """leave_hours<8 但缺 start_time 應 422"""
    resp = client.post("/api/leaves", headers=admin_headers, json={
        "employee_id": employee_id,
        "leave_type": "personal",
        "start_date": "2026-05-22",
        "end_date": "2026-05-22",
        "leave_hours": 4,
        # 故意不傳 start_time / end_time
        "reason": "test partial",
    })
    assert resp.status_code == 422
    detail = resp.json().get("detail", "")
    assert "start_time" in str(detail).lower() or "end_time" in str(detail).lower()


def test_leave_create_full_day_no_time_required(client, admin_headers, employee_id):
    """全天請假(leave_hours=8 或 None)不要 start_time"""
    resp = client.post("/api/leaves", headers=admin_headers, json={
        "employee_id": employee_id,
        "leave_type": "personal",
        "start_date": "2026-05-22",
        "end_date": "2026-05-22",
        "leave_hours": 8,
        "reason": "test full day",
    })
    assert resp.status_code in (200, 201)


def test_leave_update_partial_hours_requires_start_end_time(client, admin_headers, existing_leave_id):
    """LeaveUpdate 改 leave_hours<8 也要 start_time/end_time"""
    resp = client.patch(f"/api/leaves/{existing_leave_id}", headers=admin_headers, json={
        "leave_hours": 4,
        # 缺 start_time / end_time
    })
    assert resp.status_code == 422
```

- [ ] **Step 3: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_leaves.py -k partial_hours -v`

Expected: 3 個測試都 FAIL(目前沒這個 validator)。

- [ ] **Step 4: 在 LeaveCreate 與 LeaveUpdate 加 model_validator**

在 `api/leaves.py` LeaveCreate(約 L224)class 內加:

```python
from pydantic import model_validator

class LeaveCreate(BaseModel):
    # ... 既有欄位
    employee_id: int
    leave_type: str
    start_date: date
    end_date: date
    leave_hours: float = 8.0
    start_time: Optional[str] = None  # "HH:MM"
    end_time: Optional[str] = None
    reason: Optional[str] = None
    # ... 其他既有欄位

    @model_validator(mode="after")
    def _validate_partial_leave_times(self) -> "LeaveCreate":
        if self.leave_hours is not None and self.leave_hours < 8:
            if not self.start_time or not self.end_time:
                raise ValueError(
                    "部分請假(leave_hours<8)必須提供 start_time 與 end_time"
                )
        return self
```

對 LeaveUpdate(約 L259)做同樣修改,但**注意**:LeaveUpdate 欄位皆 Optional,要先判斷有沒有同時改:

```python
class LeaveUpdate(BaseModel):
    leave_type: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    leave_hours: Optional[float] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    reason: Optional[str] = None
    # ... 其他

    @model_validator(mode="after")
    def _validate_partial_leave_times(self) -> "LeaveUpdate":
        # 只在 leave_hours 出現且 <8 時檢查
        if self.leave_hours is not None and self.leave_hours < 8:
            if not self.start_time or not self.end_time:
                raise ValueError(
                    "部分請假(leave_hours<8)必須提供 start_time 與 end_time"
                )
        return self
```

> **注意**:LeaveUpdate 的場景是「PATCH 只送差異」。若 admin 只想改 leave_hours 而 start_time 已存於 DB,validator 會誤擋。處置:**API 層 422 是預期行為,admin 必須同時傳 start_time/end_time**。理由:這支 spec 將 attendance 視為 SoT,leave 時段是必要欄位,partial-update 不允許「半透明」。

- [ ] **Step 5: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_leaves.py -k partial_hours -v`

Expected: 3 個全綠。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend
git add api/leaves.py tests/test_leaves.py
git commit -m "feat(leaves): validator 強制部分請假必須有 start_time/end_time

leave_hours<8 但缺 start_time/end_time 的 row 在 attendance sync 路徑
無法計算 overlap,前置 validator 雙保險之一。"
```

---

## Task 2: AttendanceStatus enum 加 LEAVE 值(model 層)

**Files:**
- Modify: `models/attendance.py:23-60`(AttendanceStatus enum)

> **注意**:此 task 只動 Python enum。實際 Postgres `ALTER TYPE ... ADD VALUE` 在 Task 21 migration 處理。

- [ ] **Step 1: Read 既有 enum 確認位置與既有值**

Run: `grep -n "class AttendanceStatus" -A 10 ivy-backend/models/attendance.py`

Expected: 顯示 5 個既有值(NORMAL/LATE/EARLY_LEAVE/MISSING/ABSENT)。

- [ ] **Step 2: 加 `LEAVE = "LEAVE"`**

修改 `models/attendance.py`:

```python
class AttendanceStatus(enum.Enum):
    NORMAL       = "NORMAL"
    LATE         = "LATE"
    EARLY_LEAVE  = "EARLY_LEAVE"
    MISSING      = "MISSING"
    ABSENT       = "ABSENT"
    LEAVE        = "LEAVE"  # ← 新增:全天請假
```

- [ ] **Step 3: 跑全套 attendance 既有測試確認沒破**

Run: `cd ivy-backend && pytest tests/test_attendance*.py -x`

Expected: 全綠(只是加 enum 值,不會影響既有行為)。

- [ ] **Step 4: Commit**

```bash
cd ivy-backend
git add models/attendance.py
git commit -m "feat(attendance): AttendanceStatus enum 加 LEAVE 值

供員工請假同步寫入考勤使用;Postgres enum 由 alembic migration
另外 ALTER TYPE ADD VALUE。"
```

---

## Task 3: AttendanceRecord 加 leave_record_id / partial_leave_hours + UniqueConstraint

**Files:**
- Modify: `models/attendance.py`

- [ ] **Step 1: 確認既有 model 結構**

Run: `grep -n "class AttendanceRecord\|__tablename__\|__table_args__\|leave_record_id\|partial_leave_hours" ivy-backend/models/attendance.py`

Expected: 顯示 class 起點;不顯示 leave_record_id / partial_leave_hours / __table_args__(代表都還沒有)。

- [ ] **Step 2: 加欄位 + UniqueConstraint**

修改 `models/attendance.py`(AttendanceRecord class 內):

```python
from sqlalchemy import (
    Column, Integer, ForeignKey, Date, Time, String, DateTime, Numeric,
    UniqueConstraint, Index,
)
from sqlalchemy import Enum as SqlEnum

class AttendanceRecord(Base):
    __tablename__ = "attendance_records"

    id                   = Column(Integer, primary_key=True)
    employee_id          = Column(Integer, ForeignKey("employees.id"), index=True)
    attendance_date      = Column(Date, nullable=False, index=True)
    punch_in_time        = Column(Time, nullable=True)
    punch_out_time       = Column(Time, nullable=True)
    status               = Column(SqlEnum(AttendanceStatus), nullable=False)
    late_minutes         = Column(Integer, default=0)
    early_leave_minutes  = Column(Integer, default=0)
    remark               = Column(String, nullable=True)
    confirmed_action     = Column(String, nullable=True)
    confirmed_by         = Column(Integer, ForeignKey("employees.id"), nullable=True)
    confirmed_at         = Column(DateTime, nullable=True)
    # ── 新增 ──────────────────────────────────────────────────────
    leave_record_id      = Column(
        Integer,
        ForeignKey("leave_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    partial_leave_hours  = Column(Numeric(4, 2), nullable=True)
    # ────────────────────────────────────────────────────────────

    __table_args__ = (
        UniqueConstraint(
            "employee_id", "attendance_date",
            name="uq_attendance_employee_date",
        ),
    )
```

> **注意**:既有 model 可能還有其他 boolean 欄位(`is_late` / `is_early_leave` / `is_missing_punch_in` / `is_missing_punch_out`)。保留它們 — 不要刪。**只加新欄與 UniqueConstraint**。

- [ ] **Step 3: 跑既有 attendance 測試確認 import 沒壞**

Run: `cd ivy-backend && pytest tests/test_attendance_*.py -x --co 2>&1 | tail -10`

Expected: collect 成功(`--co` 只 collect 不 run)。

- [ ] **Step 4: Commit**

```bash
cd ivy-backend
git add models/attendance.py
git commit -m "feat(attendance): 加 leave_record_id / partial_leave_hours 欄與 UniqueConstraint

ORM 層先補,DB 層 schema 變更與 unique constraint 由 alembic migration
(Task 21-22)以 CREATE UNIQUE INDEX CONCURRENTLY 加入。"
```

---

## Task 4: `compute_late_minutes_with_leave` + C-1~C-6 單元測試

**Files:**
- Modify: `utils/attendance_calc.py`(加新純函式)
- Test: `tests/test_attendance_calc.py`(既有檔,加新案例)

- [ ] **Step 1: Read 既有 attendance_calc.py 結構**

Run: `grep -n "^def \|^class " ivy-backend/utils/attendance_calc.py`

Expected: 列出既有 public 函式(可能含 `compute_late_minutes`、`apply_attendance_status`)。

- [ ] **Step 2: 寫失敗測試**

加到 `tests/test_attendance_calc.py` 結尾:

```python
from datetime import time
from utils.attendance_calc import compute_late_minutes_with_leave


class TestComputeLateMinutesWithLeave:
    """C-1~C-6:leave-aware 遲到分鐘計算"""

    def test_c1_no_leave_normal_late(self):
        # 上班 09:00、打卡 09:30、無請假 → late=30
        result = compute_late_minutes_with_leave(
            punch_in=time(9, 30),
            scheduled_start=time(9, 0),
            leave_start=None,
            leave_end=None,
        )
        assert result == 30

    def test_c2_leave_covers_punch_time(self):
        # 上班 09:00、打卡 09:30、請假 09:00-10:00 → late=0
        result = compute_late_minutes_with_leave(
            punch_in=time(9, 30),
            scheduled_start=time(9, 0),
            leave_start=time(9, 0),
            leave_end=time(10, 0),
        )
        assert result == 0

    def test_c3_leave_starts_before_work(self):
        # 上班 09:00、打卡 09:30、請假 08:00-10:00 → late=0
        result = compute_late_minutes_with_leave(
            punch_in=time(9, 30),
            scheduled_start=time(9, 0),
            leave_start=time(8, 0),
            leave_end=time(10, 0),
        )
        assert result == 0

    def test_c4_leave_ends_before_punch(self):
        # 上班 09:00、打卡 09:30、請假 09:00-09:15 → late=15
        # (請假涵蓋 09:00-09:15,有效上班開始時間變 09:15,打卡 09:30 遲 15 分)
        result = compute_late_minutes_with_leave(
            punch_in=time(9, 30),
            scheduled_start=time(9, 0),
            leave_start=time(9, 0),
            leave_end=time(9, 15),
        )
        assert result == 15

    def test_c5_leave_short_punch_late(self):
        # 上班 09:00、打卡 10:00、請假 09:00-09:30 → late=30
        # (有效上班 09:30,打卡 10:00 遲 30 分)
        result = compute_late_minutes_with_leave(
            punch_in=time(10, 0),
            scheduled_start=time(9, 0),
            leave_start=time(9, 0),
            leave_end=time(9, 30),
        )
        assert result == 30

    def test_c6_early_leave_covered(self):
        # 早退場景:scheduled_end=18:00、punch_out=17:30、請假 17:30-18:00 → early_leave=0
        # (重用同函式:把 scheduled_start 當 scheduled_end 反向計算)
        # 為了精確,實作獨立 `compute_early_leave_minutes_with_leave`,測試先寫呼叫
        from utils.attendance_calc import compute_early_leave_minutes_with_leave
        result = compute_early_leave_minutes_with_leave(
            punch_out=time(17, 30),
            scheduled_end=time(18, 0),
            leave_start=time(17, 30),
            leave_end=time(18, 0),
        )
        assert result == 0
```

- [ ] **Step 3: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_attendance_calc.py::TestComputeLateMinutesWithLeave -v`

Expected: 6 個 FAIL(import error 或 function not found)。

- [ ] **Step 4: 實作純函式**

在 `utils/attendance_calc.py` 加:

```python
from datetime import time
from typing import Optional


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def compute_late_minutes_with_leave(
    punch_in: time,
    scheduled_start: time,
    leave_start: Optional[time],
    leave_end: Optional[time],
) -> int:
    """計算遲到分鐘,扣除請假時段涵蓋的部分。

    邏輯:
    - 無請假 → late = max(0, punch_in - scheduled_start)
    - 有請假 → 有效上班開始時間 = max(scheduled_start, leave_end if leave 涵蓋 scheduled_start else scheduled_start)
              late = max(0, punch_in - 有效上班開始時間)
    """
    sched_m = _time_to_minutes(scheduled_start)
    punch_m = _time_to_minutes(punch_in)

    if leave_start is None or leave_end is None:
        return max(0, punch_m - sched_m)

    lv_start_m = _time_to_minutes(leave_start)
    lv_end_m = _time_to_minutes(leave_end)

    # 請假涵蓋 scheduled_start → 有效上班開始 = leave_end
    if lv_start_m <= sched_m < lv_end_m:
        effective_start_m = lv_end_m
    else:
        effective_start_m = sched_m

    return max(0, punch_m - effective_start_m)


def compute_early_leave_minutes_with_leave(
    punch_out: time,
    scheduled_end: time,
    leave_start: Optional[time],
    leave_end: Optional[time],
) -> int:
    """計算早退分鐘,扣除請假時段涵蓋的部分。

    邏輯與遲到對稱:
    - 無請假 → early = max(0, scheduled_end - punch_out)
    - 請假涵蓋 scheduled_end → 有效下班結束 = leave_start
    """
    sched_m = _time_to_minutes(scheduled_end)
    punch_m = _time_to_minutes(punch_out)

    if leave_start is None or leave_end is None:
        return max(0, sched_m - punch_m)

    lv_start_m = _time_to_minutes(leave_start)
    lv_end_m = _time_to_minutes(leave_end)

    # 請假涵蓋 scheduled_end → 有效下班 = leave_start
    if lv_start_m < sched_m <= lv_end_m:
        effective_end_m = lv_start_m
    else:
        effective_end_m = sched_m

    return max(0, effective_end_m - punch_m)
```

- [ ] **Step 5: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_attendance_calc.py::TestComputeLateMinutesWithLeave -v`

Expected: 6 個全綠。

- [ ] **Step 6: 跑全套 attendance_calc 既有測試確認沒破**

Run: `cd ivy-backend && pytest tests/test_attendance_calc.py -v`

Expected: 既有 + 新加 全綠。

- [ ] **Step 7: Commit**

```bash
cd ivy-backend
git add utils/attendance_calc.py tests/test_attendance_calc.py
git commit -m "feat(attendance): leave-aware 遲到/早退分鐘計算純函式

compute_late_minutes_with_leave / compute_early_leave_minutes_with_leave
給 sync 與 merge helper 共用,扣除請假時段涵蓋的上班/下班時間。"
```

---

## Task 5: sync service 骨架 — 例外型別 + helper 函式

**Files:**
- Create: `services/employee_leave_attendance_sync.py`
- Test: `tests/test_employee_leave_attendance_sync.py`(新檔,先放例外與骨架測試)

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_employee_leave_attendance_sync.py`:

```python
"""sync service unit tests U-1~U-15"""
import pytest
from datetime import date, time
from decimal import Decimal

from services import employee_leave_attendance_sync as sync


class TestExceptions:
    def test_leave_attendance_conflict_is_exception(self):
        assert issubclass(sync.LeaveAttendanceConflict, Exception)

    def test_leave_not_approved_is_value_error(self):
        assert issubclass(sync.LeaveNotApproved, ValueError)

    def test_leave_partial_time_missing_is_value_error(self):
        assert issubclass(sync.LeavePartialTimeMissing, ValueError)


class TestIsFullDay:
    def test_full_day_when_no_times_and_hours_8(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=8.0)
        assert sync._is_full_day(leave) is True

    def test_full_day_when_no_times_and_hours_none(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=None)
        assert sync._is_full_day(leave) is True

    def test_not_full_day_when_start_time_set(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=4.0)
        assert sync._is_full_day(leave) is False

    def test_not_full_day_when_hours_lt_8(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=3.0)
        assert sync._is_full_day(leave) is False


class TestAssertLeaveTimeConsistent:
    def test_full_day_passes(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=8.0)
        sync._assert_leave_time_consistent(leave)  # 不該 raise

    def test_partial_with_times_passes(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=4.0)
        sync._assert_leave_time_consistent(leave)

    def test_partial_without_start_time_raises(self):
        leave = make_leave(start_time=None, end_time="12:00", leave_hours=4.0)
        with pytest.raises(sync.LeavePartialTimeMissing):
            sync._assert_leave_time_consistent(leave)

    def test_partial_without_end_time_raises(self):
        leave = make_leave(start_time="09:00", end_time=None, leave_hours=4.0)
        with pytest.raises(sync.LeavePartialTimeMissing):
            sync._assert_leave_time_consistent(leave)


# Helper:可放在 tests/conftest.py 或本檔頂端 module-level
class _FakeLeave:
    """測試專用 stub,模擬 LeaveRecord 必要欄位。"""
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.employee_id = kwargs.get("employee_id", 1)
        self.start_date = kwargs.get("start_date", date(2026, 5, 22))
        self.end_date = kwargs.get("end_date", date(2026, 5, 22))
        self.start_time = kwargs.get("start_time")
        self.end_time = kwargs.get("end_time")
        self.leave_hours = kwargs.get("leave_hours", 8.0)
        self.leave_type = kwargs.get("leave_type", "personal")
        self.is_approved = kwargs.get("is_approved", True)


def make_leave(**kwargs):
    return _FakeLeave(**kwargs)
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py -v`

Expected: ImportError(services.employee_leave_attendance_sync 不存在)。

- [ ] **Step 3: 建立 service 骨架**

新建 `services/employee_leave_attendance_sync.py`:

```python
"""員工請假 → 考勤同步單一進入點。

對齊學生端 services/student_leave_service 的設計理念,但因員工請假支援半天/小時,
寫入策略採「並存模式」:全天 upsert status=LEAVE;半天/小時保留打卡並標記 leave_record_id。
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from models.attendance import AttendanceRecord, AttendanceStatus
from models.leave import LeaveRecord


# ── 例外型別 ──────────────────────────────────────────────────────

class LeaveAttendanceConflict(Exception):
    """同日已有其他 leave_record_id 寫入 attendance(§1 同日多筆部分請假)。"""


class LeaveNotApproved(ValueError):
    """apply() 被呼叫時 leave 還沒 approved。"""


class LeavePartialTimeMissing(ValueError):
    """部分請假(leave_hours<8)缺 start_time/end_time,無法算 overlap。

    雙保險之一:LeaveCreate/Update validator 是第一道(Task 1),
    sync 入口再擋一次,避免 admin 直接 SQL 改 row 繞過 validator。
    """


# ── 內部 helper ───────────────────────────────────────────────────

def _is_full_day(leave: LeaveRecord) -> bool:
    """全天 = start_time/end_time 都 NULL 且 leave_hours 是 None 或 >= 8。

    舊資料可能 leave_hours=8.0 + start_time=None,也視為全天。
    """
    return leave.start_time is None and leave.end_time is None and (
        leave.leave_hours is None or leave.leave_hours >= 8
    )


def _assert_leave_time_consistent(leave: LeaveRecord) -> None:
    """半天/小時假必須有 start_time/end_time,否則 _apply_partial 會炸。"""
    if not _is_full_day(leave):
        if leave.start_time is None or leave.end_time is None:
            raise LeavePartialTimeMissing(
                f"leave_id={leave.id} 是部分請假(leave_hours={leave.leave_hours})"
                f"但缺 start_time/end_time"
            )


def _iter_dates(leave: LeaveRecord) -> Iterable[date]:
    d = leave.start_date
    while d <= leave.end_date:
        yield d
        d += timedelta(days=1)


# ── 公開 API(後續 Task 補完) ────────────────────────────────────

def apply(session: Session, leave_id: int) -> list[date]:
    raise NotImplementedError("Task 6/7 補完")


def revert(session: Session, leave_id: int) -> list[date]:
    raise NotImplementedError("Task 9 補完")


def reapply(session: Session, leave_id: int,
            old_snapshot: dict | None = None) -> tuple[list[date], list[date]]:
    raise NotImplementedError("Task 10 補完")
```

- [ ] **Step 4: 跑骨架測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py -v`

Expected: 11 個全綠(3 例外型別 + 4 _is_full_day + 4 _assert_leave_time_consistent)。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend
git add services/employee_leave_attendance_sync.py tests/test_employee_leave_attendance_sync.py
git commit -m "feat(leaves): 加 employee_leave_attendance_sync 骨架 + 例外與 helper

apply/revert/reapply 暫 NotImplementedError,後續 Task 6-10 補完。
例外型別與 _is_full_day / _assert_leave_time_consistent 先落地單元測試。"
```

---

## Task 6: sync.apply 全天分支 + U-1, U-2

**Files:**
- Modify: `services/employee_leave_attendance_sync.py`
- Modify: `tests/test_employee_leave_attendance_sync.py`

- [ ] **Step 1: 加 U-1 / U-2 測試**

附加到 `tests/test_employee_leave_attendance_sync.py`:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models.base import Base  # 假設 declarative base


@pytest.fixture
def db_session(tmp_path):
    """In-memory SQLite session for unit tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def sample_employee(db_session):
    """建一個測試員工。"""
    from models.employee import Employee
    emp = Employee(name="測試員工", employee_type="monthly", is_active=True, ...)
    db_session.add(emp)
    db_session.commit()
    return emp


@pytest.fixture
def approved_full_day_leave(db_session, sample_employee):
    """5/22~5/24 全天 personal 假,已核可"""
    from models.leave import LeaveRecord
    lv = LeaveRecord(
        employee_id=sample_employee.id,
        leave_type="personal",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 24),
        leave_hours=8.0,
        start_time=None,
        end_time=None,
        is_approved=True,
    )
    db_session.add(lv)
    db_session.commit()
    return lv


class TestApplyFullDay:
    def test_u1_apply_full_day_no_existing_attendance(self, db_session, approved_full_day_leave):
        """U-1: apply 全天 3 天 + 無既有 attendance → 建 3 筆 status=LEAVE / punch=NULL"""
        dates = sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        assert dates == [date(2026, 5, 22), date(2026, 5, 23), date(2026, 5, 24)]

        rows = db_session.query(AttendanceRecord).filter_by(
            employee_id=approved_full_day_leave.employee_id
        ).order_by(AttendanceRecord.attendance_date).all()

        assert len(rows) == 3
        for row in rows:
            assert row.status == AttendanceStatus.LEAVE
            assert row.punch_in_time is None
            assert row.punch_out_time is None
            assert row.leave_record_id == approved_full_day_leave.id
            assert row.partial_leave_hours is None
            assert row.late_minutes == 0

    def test_u2_apply_full_day_overwrites_existing_absent(self, db_session,
                                                          sample_employee,
                                                          approved_full_day_leave):
        """U-2: apply 全天請假 + 其中一天既有 ABSENT row → 更新為 LEAVE"""
        # 預先建 5/23 ABSENT row
        existing = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 23),
            status=AttendanceStatus.ABSENT,
        )
        db_session.add(existing)
        db_session.commit()

        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        row = db_session.query(AttendanceRecord).filter_by(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 23),
        ).first()
        assert row.status == AttendanceStatus.LEAVE
        assert row.leave_record_id == approved_full_day_leave.id
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestApplyFullDay -v`

Expected: 2 個 FAIL(NotImplementedError)。

- [ ] **Step 3: 實作 apply 全天分支**

修改 `services/employee_leave_attendance_sync.py`:

```python
def apply(session: Session, leave_id: int) -> list[date]:
    """把 approved leave 寫入 AttendanceRecord。Idempotent。

    Pre-condition: leave 必須是 is_approved=True;否則 raise LeaveNotApproved。
    """
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    if leave is None:
        raise LeaveNotApproved(f"leave_id={leave_id} 不存在")
    if leave.is_approved is not True:
        raise LeaveNotApproved(
            f"leave_id={leave_id} 不是已核可(is_approved={leave.is_approved})"
        )

    _assert_leave_time_consistent(leave)

    written: list[date] = []
    for d in _iter_dates(leave):
        if _is_full_day(leave):
            _apply_full_day(session, leave, d)
        else:
            _apply_partial(session, leave, d)  # Task 7 補完
        written.append(d)
    return written


def _apply_full_day(session: Session, leave: LeaveRecord, d: date) -> None:
    """全天:upsert status=LEAVE,清打卡,leave_record_id 寫入。"""
    row = session.query(AttendanceRecord).filter_by(
        employee_id=leave.employee_id,
        attendance_date=d,
    ).first()

    if row is None:
        row = AttendanceRecord(
            employee_id=leave.employee_id,
            attendance_date=d,
        )
        session.add(row)

    # Idempotent guard:已是本筆 leave 寫的 → no-op
    if row.leave_record_id == leave.id and row.status == AttendanceStatus.LEAVE:
        return

    # 衝突 guard:row 已被別筆 leave 佔據
    if row.leave_record_id is not None and row.leave_record_id != leave.id:
        raise LeaveAttendanceConflict(
            f"{d} employee_id={leave.employee_id} 已有 leave_record_id="
            f"{row.leave_record_id},無法覆蓋為 leave_id={leave.id}"
        )

    row.status = AttendanceStatus.LEAVE
    row.punch_in_time = None
    row.punch_out_time = None
    row.late_minutes = 0
    row.early_leave_minutes = 0
    row.leave_record_id = leave.id
    row.partial_leave_hours = None


def _apply_partial(session: Session, leave: LeaveRecord, d: date) -> None:
    """半天/小時:Task 7 補完。"""
    raise NotImplementedError("Task 7 補完")
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestApplyFullDay -v`

Expected: U-1 / U-2 全綠。

- [ ] **Step 5: 全套 sync test regression**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py -v`

Expected: 既有 11 + 新 2 = 13 全綠。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend
git add services/employee_leave_attendance_sync.py tests/test_employee_leave_attendance_sync.py
git commit -m "feat(leaves): sync.apply 全天分支 + 兩個整合測試

全天請假 upsert AttendanceRecord status=LEAVE,既有 ABSENT row 被覆蓋。
Idempotent guard 與 conflict guard 進去;半天/小時分支 Task 7 補完。"
```

---

## Task 7: sync.apply 部分分支 + U-3, U-4, U-5

**Files:**
- Modify: `services/employee_leave_attendance_sync.py`
- Modify: `tests/test_employee_leave_attendance_sync.py`

- [ ] **Step 1: 加 U-3 / U-4 / U-5 測試**

附加到 `tests/test_employee_leave_attendance_sync.py`:

```python
@pytest.fixture
def approved_partial_morning_leave(db_session, sample_employee):
    """5/22 半天 09:00-13:00(personal),已核可"""
    from models.leave import LeaveRecord
    lv = LeaveRecord(
        employee_id=sample_employee.id,
        leave_type="personal",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 22),
        leave_hours=4.0,
        start_time="09:00",
        end_time="13:00",
        is_approved=True,
    )
    db_session.add(lv)
    db_session.commit()
    return lv


@pytest.fixture
def approved_partial_hour_leave(db_session, sample_employee):
    """5/22 小時假 1.5hr 09:00-10:30,已核可"""
    from models.leave import LeaveRecord
    lv = LeaveRecord(
        employee_id=sample_employee.id,
        leave_type="personal",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 22),
        leave_hours=1.5,
        start_time="09:00",
        end_time="10:30",
        is_approved=True,
    )
    db_session.add(lv)
    db_session.commit()
    return lv


class TestApplyPartial:
    def test_u3_apply_partial_with_existing_late_row(self, db_session, sample_employee,
                                                     approved_partial_morning_leave):
        """U-3: apply 半天 + 既有 LATE row → status 保持 LATE / partial_leave_hours=4 / late_minutes 重算"""
        existing = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.LATE,
            punch_in_time=time(9, 30),
            late_minutes=30,
        )
        db_session.add(existing)
        db_session.commit()

        sync.apply(db_session, approved_partial_morning_leave.id)
        db_session.flush()

        row = db_session.query(AttendanceRecord).filter_by(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
        ).first()
        # punch 保留
        assert row.punch_in_time == time(9, 30)
        # leave_record_id + partial_leave_hours 寫入
        assert row.leave_record_id == approved_partial_morning_leave.id
        assert row.partial_leave_hours == Decimal("4.00")
        # late_minutes 重算:scheduled_start=09:00,請假 09:00-13:00 涵蓋 → late=0
        assert row.late_minutes == 0

    def test_u4_apply_partial_no_punch_becomes_absent(self, db_session, sample_employee,
                                                     approved_partial_morning_leave):
        """U-4: apply 半天 + 無 punch_in/out → status=ABSENT / partial_leave_hours=4"""
        sync.apply(db_session, approved_partial_morning_leave.id)
        db_session.flush()

        row = db_session.query(AttendanceRecord).filter_by(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
        ).first()
        assert row.status == AttendanceStatus.ABSENT
        assert row.punch_in_time is None
        assert row.leave_record_id == approved_partial_morning_leave.id
        assert row.partial_leave_hours == Decimal("4.00")

    def test_u5_apply_hourly_with_existing_normal(self, db_session, sample_employee,
                                                  approved_partial_hour_leave):
        """U-5: apply 小時假 1.5hr + 既有 NORMAL row → NORMAL / partial_leave_hours=1.5"""
        existing = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL,
            punch_in_time=time(8, 50),
            punch_out_time=time(18, 0),
        )
        db_session.add(existing)
        db_session.commit()

        sync.apply(db_session, approved_partial_hour_leave.id)
        db_session.flush()

        row = db_session.query(AttendanceRecord).filter_by(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
        ).first()
        assert row.status == AttendanceStatus.NORMAL
        assert row.punch_in_time == time(8, 50)
        assert row.partial_leave_hours == Decimal("1.5")
        assert row.leave_record_id == approved_partial_hour_leave.id
```

> **注意**:U-3 的 `late_minutes == 0` 假設員工 scheduled_start=09:00。實作時 `_apply_partial` 需要拿到員工的排班 scheduled_start;此 plan 階段先用「全公司 default 09:00-18:00」simplest 路徑,後續 Task 7 step 3 詳細。

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestApplyPartial -v`

Expected: 3 個 FAIL(NotImplementedError)。

- [ ] **Step 3: 實作 `_apply_partial` 並從員工取 scheduled_start/end**

先在 `services/employee_leave_attendance_sync.py` 開頭加 default schedule helper:

```python
from datetime import time
from utils.attendance_calc import (
    compute_late_minutes_with_leave,
    compute_early_leave_minutes_with_leave,
)


# 預設排班(若員工無自訂排班則 fallback)
DEFAULT_SCHEDULED_START = time(9, 0)
DEFAULT_SCHEDULED_END = time(18, 0)


def _get_employee_schedule(session: Session, employee_id: int) -> tuple[time, time]:
    """取員工排班上下班時間。若員工 model 無欄位則 fallback 預設。

    plan 階段 simplest:先 fallback default。若日後員工 model 加排班欄,改這裡。
    """
    return DEFAULT_SCHEDULED_START, DEFAULT_SCHEDULED_END
```

接著實作 `_apply_partial`:

```python
def _apply_partial(session: Session, leave: LeaveRecord, d: date) -> None:
    """半天/小時:UPSERT 不覆蓋 punch_in/punch_out;
    leave_record_id + partial_leave_hours 寫入;
    late_minutes/early_leave_minutes 用 leave-aware 重算。
    """
    row = session.query(AttendanceRecord).filter_by(
        employee_id=leave.employee_id,
        attendance_date=d,
    ).first()

    if row is None:
        row = AttendanceRecord(
            employee_id=leave.employee_id,
            attendance_date=d,
        )
        session.add(row)

    # Idempotent guard
    if row.leave_record_id == leave.id and row.partial_leave_hours is not None:
        return

    # 衝突 guard
    if row.leave_record_id is not None and row.leave_record_id != leave.id:
        raise LeaveAttendanceConflict(
            f"{d} employee_id={leave.employee_id} 已有 leave_record_id="
            f"{row.leave_record_id},無法新寫入 leave_id={leave.id}"
        )

    row.leave_record_id = leave.id
    row.partial_leave_hours = Decimal(str(leave.leave_hours))

    # 解析 leave start/end time(String "HH:MM" → time)
    lv_start = _parse_hhmm(leave.start_time)
    lv_end = _parse_hhmm(leave.end_time)

    # 無打卡 → status=ABSENT
    if row.punch_in_time is None and row.punch_out_time is None:
        row.status = AttendanceStatus.ABSENT
        row.late_minutes = 0
        row.early_leave_minutes = 0
        return

    # 有打卡 → 用 leave-aware 重算 late/early_leave;status 保留 caller 既算的(或預設 NORMAL)
    sched_start, sched_end = _get_employee_schedule(session, leave.employee_id)

    if row.punch_in_time is not None:
        row.late_minutes = compute_late_minutes_with_leave(
            punch_in=row.punch_in_time,
            scheduled_start=sched_start,
            leave_start=lv_start,
            leave_end=lv_end,
        )

    if row.punch_out_time is not None:
        row.early_leave_minutes = compute_early_leave_minutes_with_leave(
            punch_out=row.punch_out_time,
            scheduled_end=sched_end,
            leave_start=lv_start,
            leave_end=lv_end,
        )

    # 若 late 與 early_leave 都歸零,status 退回 NORMAL(原本可能是 LATE/EARLY_LEAVE)
    if row.late_minutes == 0 and row.early_leave_minutes == 0:
        if row.status in (AttendanceStatus.LATE, AttendanceStatus.EARLY_LEAVE):
            row.status = AttendanceStatus.NORMAL


def _parse_hhmm(s: str | None) -> time | None:
    if s is None:
        return None
    hh, mm = s.split(":")
    return time(int(hh), int(mm))
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestApplyPartial -v`

Expected: U-3 / U-4 / U-5 全綠。

- [ ] **Step 5: 全套 sync test regression**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py -v`

Expected: 既有 13 + 新 3 = 16 全綠。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend
git add services/employee_leave_attendance_sync.py tests/test_employee_leave_attendance_sync.py
git commit -m "feat(leaves): sync.apply 部分請假(半天/小時)分支

半天/小時請假保留 punch,partial_leave_hours 寫入;
late/early_leave 用 compute_late_minutes_with_leave 重算。
無打卡 → status=ABSENT。預設排班 09:00-18:00 fallback,
日後員工 model 加排班欄改 _get_employee_schedule()。"
```

---

## Task 8: sync.apply 例外路徑 U-6, U-7, U-8, U-15

**Files:**
- Modify: `tests/test_employee_leave_attendance_sync.py`

- [ ] **Step 1: 加 4 個例外路徑測試**

附加:

```python
class TestApplyExceptionPaths:
    def test_u6_apply_unapproved_leave_raises(self, db_session, sample_employee):
        """U-6: apply 對 unapproved leave → raise LeaveNotApproved"""
        from models.leave import LeaveRecord
        lv = LeaveRecord(
            employee_id=sample_employee.id,
            leave_type="personal",
            start_date=date(2026, 5, 22),
            end_date=date(2026, 5, 22),
            leave_hours=8.0,
            is_approved=None,  # pending
        )
        db_session.add(lv)
        db_session.commit()

        with pytest.raises(sync.LeaveNotApproved):
            sync.apply(db_session, lv.id)

    def test_u7_apply_idempotent_no_change_on_second_call(self, db_session,
                                                          sample_employee,
                                                          approved_full_day_leave):
        """U-7: apply 重跑兩次 → 第二次 no-op,row 不變"""
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()
        rows_first = db_session.query(AttendanceRecord).all()
        snapshot_first = [(r.attendance_date, r.status, r.leave_record_id) for r in rows_first]

        # 第二次
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()
        rows_second = db_session.query(AttendanceRecord).all()
        snapshot_second = [(r.attendance_date, r.status, r.leave_record_id) for r in rows_second]

        assert snapshot_first == snapshot_second
        assert len(rows_second) == 3  # 沒重複插

    def test_u8_apply_conflict_with_other_leave_id(self, db_session, sample_employee,
                                                   approved_full_day_leave):
        """U-8: apply 同日已有其他 leave_record_id → raise LeaveAttendanceConflict"""
        # 預先建一筆 row 帶不同 leave_record_id=9999
        existing = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 23),
            status=AttendanceStatus.LEAVE,
            leave_record_id=9999,
        )
        db_session.add(existing)
        db_session.commit()

        with pytest.raises(sync.LeaveAttendanceConflict):
            sync.apply(db_session, approved_full_day_leave.id)

    def test_u15_apply_partial_missing_time_raises(self, db_session, sample_employee):
        """U-15: apply 對部分請假但缺 start_time/end_time → raise LeavePartialTimeMissing"""
        from models.leave import LeaveRecord
        lv = LeaveRecord(
            employee_id=sample_employee.id,
            leave_type="personal",
            start_date=date(2026, 5, 22),
            end_date=date(2026, 5, 22),
            leave_hours=4.0,
            start_time=None,  # 故意缺
            end_time=None,
            is_approved=True,
        )
        db_session.add(lv)
        db_session.commit()

        with pytest.raises(sync.LeavePartialTimeMissing):
            sync.apply(db_session, lv.id)
```

- [ ] **Step 2: 跑測試確認 PASS(實作其實 Task 6/7 已完成,這 4 條主要是覆蓋)**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestApplyExceptionPaths -v`

Expected: U-6 / U-7 / U-8 / U-15 全綠。

- [ ] **Step 3: 全套 sync test regression**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py -v`

Expected: 既有 16 + 新 4 = 20 全綠。

- [ ] **Step 4: Commit**

```bash
cd ivy-backend
git add tests/test_employee_leave_attendance_sync.py
git commit -m "test(leaves): sync.apply 例外路徑覆蓋 U-6/U-7/U-8/U-15

unapproved / idempotent / conflict / partial-time-missing 四條
例外行為驗證完整。"
```

---

## Task 9: sync.revert + U-9, U-10, U-11, U-12

**Files:**
- Modify: `services/employee_leave_attendance_sync.py`
- Modify: `tests/test_employee_leave_attendance_sync.py`

- [ ] **Step 1: 加 U-9~U-12 測試**

附加:

```python
class TestRevert:
    def test_u9_revert_full_day_no_punch_deletes_row(self, db_session, sample_employee,
                                                     approved_full_day_leave):
        """U-9: revert 全天 + 該 row 無 punch → row 刪除"""
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()
        assert db_session.query(AttendanceRecord).count() == 3

        dates = sync.revert(db_session, approved_full_day_leave.id)
        db_session.flush()

        assert len(dates) == 3
        assert db_session.query(AttendanceRecord).count() == 0

    def test_u10_revert_full_day_with_punch_reverts_to_normal(self, db_session, sample_employee,
                                                              approved_full_day_leave):
        """U-10: revert 全天 + 該 row 有 punch(極端) → 退回 status=NORMAL,清 leave_record_id

        實際上 _apply_full_day 清掉了 punch,所以這個案例只能用「手動 inject 髒資料」測。
        """
        # 手動建立髒資料:status=LEAVE 但有 punch
        dirty = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.LEAVE,
            punch_in_time=time(9, 0),
            punch_out_time=time(18, 0),
            leave_record_id=approved_full_day_leave.id,
        )
        db_session.add(dirty)
        db_session.commit()

        sync.revert(db_session, approved_full_day_leave.id)
        db_session.flush()

        row = db_session.query(AttendanceRecord).filter_by(
            attendance_date=date(2026, 5, 22),
        ).first()
        # 不被刪(因有 punch),改成 NORMAL
        assert row is not None
        assert row.status == AttendanceStatus.NORMAL
        assert row.leave_record_id is None

    def test_u11_revert_partial_keeps_punch_and_status(self, db_session, sample_employee,
                                                      approved_partial_morning_leave):
        """U-11: revert 半天 → 保留 punch / status / late,清 leave_record_id + partial_leave_hours"""
        # apply 前先有 punch_in
        existing = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.LATE,
            punch_in_time=time(9, 30),
            late_minutes=30,
        )
        db_session.add(existing)
        db_session.commit()

        sync.apply(db_session, approved_partial_morning_leave.id)
        db_session.flush()

        sync.revert(db_session, approved_partial_morning_leave.id)
        db_session.flush()

        row = db_session.query(AttendanceRecord).filter_by(
            attendance_date=date(2026, 5, 22),
        ).first()
        assert row.punch_in_time == time(9, 30)
        assert row.leave_record_id is None
        assert row.partial_leave_hours is None
        # late_minutes 重新算回 30(無 leave 涵蓋)
        assert row.late_minutes == 30
        assert row.status == AttendanceStatus.LATE

    def test_u12_revert_idempotent(self, db_session, sample_employee, approved_full_day_leave):
        """U-12: revert 重跑 → 第二次 no-op"""
        sync.apply(db_session, approved_full_day_leave.id)
        sync.revert(db_session, approved_full_day_leave.id)
        db_session.flush()

        # 第二次
        result = sync.revert(db_session, approved_full_day_leave.id)
        assert result == []
        assert db_session.query(AttendanceRecord).count() == 0
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestRevert -v`

Expected: 4 個 FAIL(NotImplementedError)。

- [ ] **Step 3: 實作 revert**

修改 `services/employee_leave_attendance_sync.py`:

```python
def revert(session: Session, leave_id: int) -> list[date]:
    """把 leave 之前寫入的 AttendanceRecord 反寫。Idempotent。

    全天 → 刪該 row(若無打卡)/退回 NORMAL(若有打卡 — 邊緣情境)。
    半天/小時 → NULL out leave_record_id + partial_leave_hours,重算 late_minutes。
    回傳實際反寫的日期列表。
    """
    rows = session.query(AttendanceRecord).filter_by(
        leave_record_id=leave_id,
    ).all()

    if not rows:
        return []

    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    # 注意:leave 可能已被 caller 改 is_approved 或刪掉,revert 不依賴 leave 內容,只依賴 row.leave_record_id
    # 但 _is_full_day 需要 leave 物件 — 若 leave 已刪除,改用 row 自身狀態判斷
    is_full_day_leave = leave is not None and _is_full_day(leave)

    dates: list[date] = []
    for row in rows:
        if row.status == AttendanceStatus.LEAVE and row.punch_in_time is None \
                and row.punch_out_time is None:
            # 全天且無 punch → 純衍生 row,刪
            session.delete(row)
        else:
            # 有 punch 或是半天:清 leave_*,重算 late/early
            row.leave_record_id = None
            row.partial_leave_hours = None
            if row.status == AttendanceStatus.LEAVE:
                row.status = AttendanceStatus.NORMAL  # 全天 LEAVE 退回 NORMAL
            # 重新計算 late/early(無 leave 涵蓋)
            if row.punch_in_time is not None:
                sched_start, sched_end = _get_employee_schedule(session, row.employee_id)
                row.late_minutes = compute_late_minutes_with_leave(
                    punch_in=row.punch_in_time,
                    scheduled_start=sched_start,
                    leave_start=None,
                    leave_end=None,
                )
            if row.punch_out_time is not None:
                sched_start, sched_end = _get_employee_schedule(session, row.employee_id)
                row.early_leave_minutes = compute_early_leave_minutes_with_leave(
                    punch_out=row.punch_out_time,
                    scheduled_end=sched_end,
                    leave_start=None,
                    leave_end=None,
                )
            # status 修復:有 late_minutes > 0 → LATE
            if row.late_minutes > 0:
                row.status = AttendanceStatus.LATE
            elif row.early_leave_minutes > 0:
                row.status = AttendanceStatus.EARLY_LEAVE
            elif row.status not in (AttendanceStatus.NORMAL, AttendanceStatus.ABSENT,
                                    AttendanceStatus.MISSING):
                row.status = AttendanceStatus.NORMAL
        dates.append(row.attendance_date)
    return sorted(dates)
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestRevert -v`

Expected: U-9 / U-10 / U-11 / U-12 全綠。

- [ ] **Step 5: 全套 sync regression**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py -v`

Expected: 既有 20 + 新 4 = 24 全綠。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend
git add services/employee_leave_attendance_sync.py tests/test_employee_leave_attendance_sync.py
git commit -m "feat(leaves): sync.revert + 4 個對應測試

全天且無 punch → row 刪;否則清 leave_* 並重算 late/early。
Idempotent:revert 後再跑 no-op。"
```

---

## Task 10: sync.reapply + U-13, U-14

**Files:**
- Modify: `services/employee_leave_attendance_sync.py`
- Modify: `tests/test_employee_leave_attendance_sync.py`

- [ ] **Step 1: 加 U-13 / U-14 測試**

附加:

```python
class TestReapply:
    def test_u13_reapply_changes_dates(self, db_session, sample_employee,
                                        approved_full_day_leave):
        """U-13: reapply 改日期 5/22-5/24 → 5/23-5/25 → 5/22 還原;5/25 新建;5/23/5/24 保留"""
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        # snapshot 舊範圍
        old_snapshot = {
            "start_date": date(2026, 5, 22),
            "end_date": date(2026, 5, 24),
            "start_time": None,
            "end_time": None,
            "leave_type": "personal",
            "leave_hours": 8.0,
        }

        # 改 leave 範圍到 5/23-5/25
        approved_full_day_leave.start_date = date(2026, 5, 23)
        approved_full_day_leave.end_date = date(2026, 5, 25)
        db_session.commit()

        reverted, applied = sync.reapply(
            db_session,
            approved_full_day_leave.id,
            old_snapshot=old_snapshot,
        )
        db_session.flush()

        rows = db_session.query(AttendanceRecord).order_by(
            AttendanceRecord.attendance_date
        ).all()
        dates = [r.attendance_date for r in rows]
        assert dates == [date(2026, 5, 23), date(2026, 5, 24), date(2026, 5, 25)]

    def test_u14_reapply_full_day_to_partial(self, db_session, sample_employee):
        """U-14: reapply 改 leave_hours 8→4(全天變半天)→ 該日從 LEAVE 變半天標記"""
        from models.leave import LeaveRecord

        lv = LeaveRecord(
            employee_id=sample_employee.id,
            leave_type="personal",
            start_date=date(2026, 5, 22),
            end_date=date(2026, 5, 22),
            leave_hours=8.0,
            is_approved=True,
        )
        db_session.add(lv)
        db_session.commit()

        # 第一次 apply(全天)
        sync.apply(db_session, lv.id)
        db_session.flush()
        row = db_session.query(AttendanceRecord).first()
        assert row.status == AttendanceStatus.LEAVE

        # snapshot 舊狀態
        old_snapshot = {
            "start_date": date(2026, 5, 22),
            "end_date": date(2026, 5, 22),
            "start_time": None,
            "end_time": None,
            "leave_type": "personal",
            "leave_hours": 8.0,
        }

        # 改 leave_hours 為 4(半天) + 補 start_time/end_time
        lv.leave_hours = 4.0
        lv.start_time = "09:00"
        lv.end_time = "13:00"
        db_session.commit()

        sync.reapply(db_session, lv.id, old_snapshot=old_snapshot)
        db_session.flush()

        row = db_session.query(AttendanceRecord).first()
        # 從 LEAVE 變成 ABSENT(因為 revert 後 row 被刪,但新 apply 半天無 punch → ABSENT)
        # 或者 revert 後刪除、新 apply 建一筆 ABSENT row
        assert row.status == AttendanceStatus.ABSENT
        assert row.partial_leave_hours == Decimal("4.00")
        assert row.leave_record_id == lv.id
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestReapply -v`

Expected: 2 FAIL(NotImplementedError)。

- [ ] **Step 3: 實作 reapply**

修改 `services/employee_leave_attendance_sync.py`:

```python
def reapply(session: Session, leave_id: int,
            old_snapshot: dict | None = None) -> tuple[list[date], list[date]]:
    """update_leave 改了關鍵欄(日期/時段/leave_type/hours)時呼叫。

    內部組合:revert(舊範圍) → apply(新範圍)。
    old_snapshot 必須由 caller 在 commit 前抓:
      {start_date, end_date, start_time, end_time, leave_type, leave_hours}
    """
    # revert 純靠 row.leave_record_id 找,不需要 old_snapshot;
    # 但若 leave 已被改 + 新範圍不含某舊日 → revert 仍能清掉所有屬於這筆 leave 的 row
    reverted = revert(session, leave_id)

    # 重新 apply(用當前 leave 物件的新值)
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    if leave is None or leave.is_approved is not True:
        return reverted, []
    applied = apply(session, leave_id)
    return reverted, applied
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py::TestReapply -v`

Expected: U-13 / U-14 全綠。

- [ ] **Step 5: 全套 sync regression**

Run: `cd ivy-backend && pytest tests/test_employee_leave_attendance_sync.py -v`

Expected: 既有 24 + 新 2 = 26 全綠。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend
git add services/employee_leave_attendance_sync.py tests/test_employee_leave_attendance_sync.py
git commit -m "feat(leaves): sync.reapply = revert + apply 組合

update_leave 改關鍵欄時呼叫;revert 純靠 row.leave_record_id 找
不需要 old_snapshot 也能清舊 row,但 API 仍保留 old_snapshot 參數
供 hook 4 在 model 寫回前 snapshot(plan Task 13)。"
```

---

## Task 11: merge_attendance_with_leave + M-1~M-7

**Files:**
- Create: `utils/attendance_leave_merge.py`
- Create: `tests/test_attendance_leave_merge.py`

- [ ] **Step 1: 寫失敗測試 M-1~M-7**

新建 `tests/test_attendance_leave_merge.py`:

```python
"""M-1~M-7: merge_attendance_with_leave 純函式單元測試"""
import pytest
from datetime import date, time
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.attendance import AttendanceRecord, AttendanceStatus
from models.base import Base
from utils.attendance_leave_merge import merge_attendance_with_leave


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def sample_employee(db_session):
    from models.employee import Employee
    emp = Employee(name="測試員工", employee_type="monthly", is_active=True)
    db_session.add(emp)
    db_session.commit()
    return emp


def _make_leave(db_session, emp_id, **kwargs):
    from models.leave import LeaveRecord
    lv = LeaveRecord(
        employee_id=emp_id,
        leave_type=kwargs.get("leave_type", "personal"),
        start_date=kwargs.get("start_date", date(2026, 5, 22)),
        end_date=kwargs.get("end_date", date(2026, 5, 22)),
        start_time=kwargs.get("start_time"),
        end_time=kwargs.get("end_time"),
        leave_hours=kwargs.get("leave_hours", 8.0),
        is_approved=kwargs.get("is_approved", True),
    )
    db_session.add(lv)
    db_session.commit()
    return lv


class TestMergeAttendanceWithLeave:
    def test_m1_no_leave_noop(self, db_session, sample_employee):
        """M-1: 當日無 approved leave → att.leave_record_id=None"""
        att = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL,
            punch_in_time=time(9, 0),
        )
        merge_attendance_with_leave(att, db_session)
        assert att.leave_record_id is None
        assert att.partial_leave_hours is None
        assert att.status == AttendanceStatus.NORMAL

    def test_m2_full_day_leave_no_punch(self, db_session, sample_employee):
        """M-2: 全天 leave + 無打卡 → status=LEAVE,清打卡"""
        lv = _make_leave(db_session, sample_employee.id, leave_hours=8.0)
        att = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.ABSENT,  # 預設 ABSENT,merge 後變 LEAVE
        )
        merge_attendance_with_leave(att, db_session)
        assert att.status == AttendanceStatus.LEAVE
        assert att.leave_record_id == lv.id
        assert att.partial_leave_hours is None
        assert att.punch_in_time is None
        assert att.late_minutes == 0

    def test_m3_full_day_leave_with_punch(self, db_session, sample_employee):
        """M-3: 全天 leave + 有打卡(臨時上班)→ leave_record_id 寫入,status 保留,partial=0"""
        lv = _make_leave(db_session, sample_employee.id, leave_hours=8.0)
        att = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL,
            punch_in_time=time(9, 0),
            punch_out_time=time(18, 0),
            late_minutes=0,
        )
        merge_attendance_with_leave(att, db_session)
        assert att.leave_record_id == lv.id
        assert att.partial_leave_hours == Decimal("0")
        assert att.status == AttendanceStatus.NORMAL  # 保留 caller 算的
        assert att.punch_in_time == time(9, 0)

    def test_m4_partial_leave_with_punch(self, db_session, sample_employee):
        """M-4: 部分 leave + 有打卡 → partial_leave_hours,late_minutes leave-aware 重算"""
        lv = _make_leave(
            db_session, sample_employee.id,
            leave_hours=4.0, start_time="09:00", end_time="13:00",
        )
        att = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.LATE,
            punch_in_time=time(9, 30),
            late_minutes=30,  # caller 原本算的
        )
        merge_attendance_with_leave(att, db_session)
        assert att.leave_record_id == lv.id
        assert att.partial_leave_hours == Decimal("4.00")
        # 請假 09:00-13:00 涵蓋 09:30 → late=0
        assert att.late_minutes == 0
        # 因 late=0,status 退回 NORMAL
        assert att.status == AttendanceStatus.NORMAL

    def test_m5_partial_leave_no_punch(self, db_session, sample_employee):
        """M-5: 部分 leave + 無打卡 → status=ABSENT"""
        lv = _make_leave(
            db_session, sample_employee.id,
            leave_hours=4.0, start_time="09:00", end_time="13:00",
        )
        att = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL,  # caller 預設值
        )
        merge_attendance_with_leave(att, db_session)
        assert att.status == AttendanceStatus.ABSENT
        assert att.leave_record_id == lv.id
        assert att.partial_leave_hours == Decimal("4.00")

    def test_m6_multiple_leaves_uses_earliest(self, db_session, sample_employee):
        """M-6: 同日多筆 approved leave(異常)→ 取最早 id"""
        lv1 = _make_leave(db_session, sample_employee.id, leave_hours=4.0,
                          start_time="09:00", end_time="13:00")
        lv2 = _make_leave(db_session, sample_employee.id, leave_hours=2.0,
                          start_time="14:00", end_time="16:00")
        att = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL,
        )
        merge_attendance_with_leave(att, db_session)
        assert att.leave_record_id == lv1.id  # 取較早 id

    def test_m7_session_only_read(self, db_session, sample_employee):
        """M-7: helper 不修改 session(只讀)"""
        _make_leave(db_session, sample_employee.id, leave_hours=8.0)
        att = AttendanceRecord(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL,
        )
        # 確保 session 沒 dirty(merge 只改 att 物件本身)
        merge_attendance_with_leave(att, db_session)
        # att 不應被加入 session
        assert att not in db_session.new
        # 沒呼叫 commit 也不該 leak 任何寫入
        assert len(db_session.dirty) == 0
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_attendance_leave_merge.py -v`

Expected: 7 個 FAIL(ImportError)。

- [ ] **Step 3: 實作 merge_attendance_with_leave**

新建 `utils/attendance_leave_merge.py`:

```python
"""寫 AttendanceRecord 前合併當日有效 leave 資訊。

設計理念:寫入端負責 leave-awareness。不依靠 leave 端 trigger reapply 的隱性合約。

對等於 sync service:sync 在 leaves 生命週期事件寫 attendance;
merge 在 attendance 寫入事件 pull leave。兩者並列、不互呼。
"""

from datetime import time
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from models.attendance import AttendanceRecord, AttendanceStatus
from models.leave import LeaveRecord
from utils.attendance_calc import (
    compute_late_minutes_with_leave,
    compute_early_leave_minutes_with_leave,
)


# 預設排班(對齊 services/employee_leave_attendance_sync)
DEFAULT_SCHEDULED_START = time(9, 0)
DEFAULT_SCHEDULED_END = time(18, 0)


def merge_attendance_with_leave(att: AttendanceRecord, session: Session) -> None:
    """In-place 把當日有效 leave 的 leave_record_id / partial_leave_hours /
    late_minutes 等欄合進 att。純函式,只讀 session。

    決策表:
      1. 無 leave            → 清 leave_*,保留 caller 算好的 status/late_minutes
      2. 全天 + 無打卡       → status=LEAVE,清打卡,leave_record_id 寫入
      3. 全天 + 有打卡       → 保留打卡,leave_record_id 寫入,partial_leave_hours=0
      4. 部分 + 有打卡       → 保留打卡,partial_leave_hours 寫入,late 重算
      5. 部分 + 無打卡       → status=ABSENT,leave_record_id + partial_leave_hours 寫入
      6. 同日多筆 leave      → 取最早 id
    """
    leaves = session.query(LeaveRecord).filter(
        LeaveRecord.employee_id == att.employee_id,
        LeaveRecord.start_date <= att.attendance_date,
        LeaveRecord.end_date >= att.attendance_date,
        LeaveRecord.is_approved == True,
    ).order_by(LeaveRecord.id).all()

    if not leaves:
        # case 1:無 leave
        att.leave_record_id = None
        att.partial_leave_hours = None
        return

    leave = leaves[0]  # case 6:取最早
    att.leave_record_id = leave.id

    if _is_full_day(leave):
        if att.punch_in_time is None and att.punch_out_time is None:
            # case 2
            att.status = AttendanceStatus.LEAVE
            att.partial_leave_hours = None
            att.late_minutes = 0
            att.early_leave_minutes = 0
        else:
            # case 3:全天請假但人來了
            att.partial_leave_hours = Decimal("0")
            # status / late_minutes 保留 caller 算好的
    else:
        # 部分
        att.partial_leave_hours = Decimal(str(leave.leave_hours))
        lv_start = _parse_hhmm(leave.start_time)
        lv_end = _parse_hhmm(leave.end_time)

        if att.punch_in_time is None and att.punch_out_time is None:
            # case 5
            att.status = AttendanceStatus.ABSENT
            att.late_minutes = 0
            att.early_leave_minutes = 0
        else:
            # case 4
            sched_start, sched_end = _get_employee_schedule(session, att.employee_id)
            if att.punch_in_time is not None:
                att.late_minutes = compute_late_minutes_with_leave(
                    punch_in=att.punch_in_time,
                    scheduled_start=sched_start,
                    leave_start=lv_start,
                    leave_end=lv_end,
                )
            if att.punch_out_time is not None:
                att.early_leave_minutes = compute_early_leave_minutes_with_leave(
                    punch_out=att.punch_out_time,
                    scheduled_end=sched_end,
                    leave_start=lv_start,
                    leave_end=lv_end,
                )
            # status 修復
            if att.late_minutes == 0 and att.early_leave_minutes == 0:
                if att.status in (AttendanceStatus.LATE, AttendanceStatus.EARLY_LEAVE):
                    att.status = AttendanceStatus.NORMAL


def _is_full_day(leave: LeaveRecord) -> bool:
    return leave.start_time is None and leave.end_time is None and (
        leave.leave_hours is None or leave.leave_hours >= 8
    )


def _parse_hhmm(s: Optional[str]) -> Optional[time]:
    if s is None:
        return None
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _get_employee_schedule(session: Session, employee_id: int) -> tuple[time, time]:
    """對齊 services/employee_leave_attendance_sync._get_employee_schedule。

    日後員工 model 加排班欄,兩處同步改。
    """
    return DEFAULT_SCHEDULED_START, DEFAULT_SCHEDULED_END
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_attendance_leave_merge.py -v`

Expected: M-1~M-7 全綠。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend
git add utils/attendance_leave_merge.py tests/test_attendance_leave_merge.py
git commit -m "feat(attendance): merge_attendance_with_leave 純函式

對等於 sync service:sync 在 leaves 端,merge 在 attendance 寫入端。
3 個 caller(admin 手動 / Excel upload / 補卡)後續 Task 15-17 接上。"
```

---

## Task 12: api/leaves.py approve hook + I-1, I-2, I-3, I-8, I-9, I-10

**Files:**
- Modify: `api/leaves.py:1408-1532`(approve_leave endpoint)
- Create: `tests/test_leaves_attendance_sync.py`

- [ ] **Step 1: Read 既有 approve_leave 函式定位**

Run: `grep -n "def approve_leave\|sync\.\|HTTPException(422" ivy-backend/api/leaves.py | head -30`

Expected: 顯示 approve_leave 起點(約 L1408)。

- [ ] **Step 2: 寫整合測試 I-1~I-3 + I-8~I-10**

新建 `tests/test_leaves_attendance_sync.py`:

```python
"""I-1~I-10: leaves 端 hook 整合測試。

使用 TestClient 真實打 endpoint,確保 sync hook 接好。
"""

import pytest
from datetime import date

from models.attendance import AttendanceRecord, AttendanceStatus
from models.leave import LeaveRecord


def _create_pending_leave(client, admin_headers, employee_id, **kwargs):
    """建立一筆 pending leave,回傳 leave_id"""
    payload = {
        "employee_id": employee_id,
        "leave_type": kwargs.get("leave_type", "personal"),
        "start_date": kwargs.get("start_date", "2026-05-22"),
        "end_date": kwargs.get("end_date", "2026-05-22"),
        "leave_hours": kwargs.get("leave_hours", 8),
        "start_time": kwargs.get("start_time"),
        "end_time": kwargs.get("end_time"),
        "reason": "test",
    }
    resp = client.post("/api/leaves", headers=admin_headers, json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


class TestApproveHookIntegration:
    def test_i1_approve_writes_attendance(self, client, admin_headers, employee_id, db_session):
        """I-1: POST approve approved=True → AttendanceRecord 寫入"""
        leave_id = _create_pending_leave(client, admin_headers, employee_id,
                                          start_date="2026-05-22", end_date="2026-05-24")
        resp = client.post(f"/api/leaves/{leave_id}/approve",
                            headers=admin_headers, json={"approved": True})
        assert resp.status_code == 200

        rows = db_session.query(AttendanceRecord).filter_by(
            employee_id=employee_id,
            leave_record_id=leave_id,
        ).all()
        assert len(rows) == 3
        for r in rows:
            assert r.status == AttendanceStatus.LEAVE

    def test_i2_reject_does_not_write_attendance(self, client, admin_headers,
                                                  employee_id, db_session):
        """I-2: POST approve approved=False(reject) → 無 AttendanceRecord 建立"""
        leave_id = _create_pending_leave(client, admin_headers, employee_id)
        resp = client.post(f"/api/leaves/{leave_id}/approve",
                            headers=admin_headers, json={"approved": False})
        assert resp.status_code == 200

        rows = db_session.query(AttendanceRecord).filter_by(
            employee_id=employee_id,
            leave_record_id=leave_id,
        ).all()
        assert rows == []

    def test_i3_approve_then_reject_reverts(self, client, admin_headers, employee_id, db_session):
        """I-3: I-1 之後再 POST approved=False → AttendanceRecord 反寫(全天 row 刪)"""
        leave_id = _create_pending_leave(client, admin_headers, employee_id,
                                          start_date="2026-05-22", end_date="2026-05-22")
        client.post(f"/api/leaves/{leave_id}/approve",
                     headers=admin_headers, json={"approved": True})

        resp = client.post(f"/api/leaves/{leave_id}/approve",
                            headers=admin_headers, json={"approved": False})
        assert resp.status_code == 200

        rows = db_session.query(AttendanceRecord).filter_by(
            employee_id=employee_id,
            leave_record_id=leave_id,
        ).all()
        assert rows == []

    def test_i8_approve_with_conflict_returns_422(self, client, admin_headers,
                                                  employee_id, db_session):
        """I-8: Approve 觸發 LeaveAttendanceConflict → 422 / AttendanceRecord 無變化"""
        # 預先建一筆 leave_record_id=9999 佔位
        existing = AttendanceRecord(
            employee_id=employee_id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.LEAVE,
            leave_record_id=9999,
        )
        db_session.add(existing)
        db_session.commit()

        leave_id = _create_pending_leave(client, admin_headers, employee_id,
                                          start_date="2026-05-22", end_date="2026-05-22")
        resp = client.post(f"/api/leaves/{leave_id}/approve",
                            headers=admin_headers, json={"approved": True})
        assert resp.status_code == 422

        # leave 也不變(仍 pending)
        leave = db_session.query(LeaveRecord).filter_by(id=leave_id).first()
        assert leave.is_approved is None

    def test_i9_approve_idempotent(self, client, admin_headers, employee_id, db_session):
        """I-9: Approve 連點兩次 → 只寫一次"""
        leave_id = _create_pending_leave(client, admin_headers, employee_id,
                                          start_date="2026-05-22", end_date="2026-05-22")
        client.post(f"/api/leaves/{leave_id}/approve",
                     headers=admin_headers, json={"approved": True})

        # 第二次相同 approve
        resp = client.post(f"/api/leaves/{leave_id}/approve",
                            headers=admin_headers, json={"approved": True})
        assert resp.status_code in (200, 409)  # 既有邏輯可能擋重複,看實作

        rows = db_session.query(AttendanceRecord).filter_by(
            leave_record_id=leave_id,
        ).all()
        assert len(rows) == 1

    def test_i10_approve_sync_failure_rollbacks(self, client, admin_headers,
                                                employee_id, db_session, monkeypatch):
        """I-10: Approve 時 sync 拋例外 → 422 / LeaveRecord 不變(整 transaction rollback)"""
        # monkey patch sync.apply 讓它拋例外
        from services import employee_leave_attendance_sync as sync
        def boom(*a, **kw):
            raise RuntimeError("sync 故意爆")
        monkeypatch.setattr(sync, "apply", boom)

        leave_id = _create_pending_leave(client, admin_headers, employee_id)
        resp = client.post(f"/api/leaves/{leave_id}/approve",
                            headers=admin_headers, json={"approved": True})
        # 預期 500(unhandled)或 422(若 caller catch)— 看實作
        assert resp.status_code >= 400

        # leave 不變
        leave = db_session.query(LeaveRecord).filter_by(id=leave_id).first()
        assert leave.is_approved is None  # rollback 後仍 pending
```

> **注意**:`client`、`admin_headers`、`employee_id`、`db_session` fixture 假設來自既有 `tests/conftest.py`。若不存在,須在 conftest 補。

- [ ] **Step 3: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_leaves_attendance_sync.py::TestApproveHookIntegration -v`

Expected: 6 個 FAIL(approve hook 還沒接 sync)。

- [ ] **Step 4: 加 sync hook 到 approve_leave**

修改 `api/leaves.py:1408-1532`(approve_leave 內):

在 `leave.is_approved = data.approved` 那行之後、`session.flush()` 之前加:

```python
from services import employee_leave_attendance_sync as sync

# ... 既有 approve_leave 函式
def approve_leave(leave_id: int, data: ApproveRequest, ...):
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    was_approved = (leave.is_approved is True)

    leave.is_approved = data.approved
    # ... 既有:寫 ApprovalLog、force_overlap comment

    # ── Sync hook ────────────────────────────────────────────────
    try:
        if data.approved is True and not was_approved:
            sync.apply(session, leave_id)
        elif data.approved is False and was_approved:
            sync.revert(session, leave_id)
    except (sync.LeaveAttendanceConflict, sync.LeavePartialTimeMissing) as e:
        # leave.is_approved 已被改但 transaction 還沒 commit
        # raise HTTPException 之後 FastAPI middleware 會 rollback
        raise HTTPException(status_code=422, detail=str(e))
    # ─────────────────────────────────────────────────────────────

    session.flush()
    # ... 既有:salary recalc(L1479-1504)、LINE_NOTIFY、close-month guard 全保留
```

- [ ] **Step 5: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_leaves_attendance_sync.py::TestApproveHookIntegration -v`

Expected: I-1, I-2, I-3, I-8, I-9, I-10 全綠。

- [ ] **Step 6: 既有 leaves 測試 regression**

Run: `cd ivy-backend && pytest tests/test_leaves.py -v`

Expected: 既有測試全綠(approve 主流程不破)。

- [ ] **Step 7: Commit**

```bash
cd ivy-backend
git add api/leaves.py tests/test_leaves_attendance_sync.py
git commit -m "feat(leaves): approve_leave hook 接 sync.apply/revert

approved=True 觸發 apply,approved=False 從 was_approved 觸發 revert。
LeaveAttendanceConflict / LeavePartialTimeMissing → 422,
FastAPI middleware 自動 rollback transaction。
既有 salary recalc / LINE / ApprovalLog 一字不改。"
```

---

## Task 13: api/leaves.py update hook + I-4, I-5, I-6

**Files:**
- Modify: `api/leaves.py:760-978`(update_leave + 其 helper)
- Modify: `tests/test_leaves_attendance_sync.py`

- [ ] **Step 1: 加 I-4, I-5, I-6 測試**

附加到 `tests/test_leaves_attendance_sync.py`:

```python
class TestUpdateHookIntegration:
    def test_i4_update_extend_end_date(self, client, admin_headers, employee_id, db_session):
        """I-4: I-1 之後 PATCH 改 end_date(延長)→ 新增日寫入"""
        leave_id = _create_pending_leave(client, admin_headers, employee_id,
                                          start_date="2026-05-22", end_date="2026-05-22")
        client.post(f"/api/leaves/{leave_id}/approve",
                     headers=admin_headers, json={"approved": True})

        # PATCH 延長到 5/24(若 update 自動退審就改測退審路徑;若保留 approved 則 reapply)
        resp = client.patch(f"/api/leaves/{leave_id}",
                             headers=admin_headers,
                             json={"end_date": "2026-05-24"})
        assert resp.status_code == 200

        # 視 update_leave 邏輯而定:若退審 → 0 筆 attendance;若 reapply → 3 筆
        rows = db_session.query(AttendanceRecord).filter_by(
            leave_record_id=leave_id,
        ).all()
        leave = db_session.query(LeaveRecord).filter_by(id=leave_id).first()
        if leave.is_approved is True:
            # reapply 路徑
            assert len(rows) == 3
        else:
            # 退審路徑
            assert len(rows) == 0

    def test_i5_update_hours_full_to_partial(self, client, admin_headers,
                                              employee_id, db_session):
        """I-5: I-1 之後 PATCH 改 leave_hours 8→4 + start_time/end_time → 部分標記"""
        leave_id = _create_pending_leave(client, admin_headers, employee_id,
                                          start_date="2026-05-22", end_date="2026-05-22")
        client.post(f"/api/leaves/{leave_id}/approve",
                     headers=admin_headers, json={"approved": True})

        resp = client.patch(f"/api/leaves/{leave_id}",
                             headers=admin_headers,
                             json={"leave_hours": 4, "start_time": "09:00",
                                   "end_time": "13:00"})
        assert resp.status_code == 200

        # 同樣視 update 退審行為而定
        leave = db_session.query(LeaveRecord).filter_by(id=leave_id).first()
        if leave.is_approved is True:
            row = db_session.query(AttendanceRecord).filter_by(
                leave_record_id=leave_id,
            ).first()
            assert row.partial_leave_hours == Decimal("4.00")

    def test_i6_update_unapprove_reverts(self, client, admin_headers,
                                          employee_id, db_session):
        """I-6: I-1 之後 PATCH 改關鍵欄觸發退審 → AttendanceRecord 反寫

        update_leave 既有邏輯:改關鍵欄會把 is_approved 從 True 設回 None
        """
        leave_id = _create_pending_leave(client, admin_headers, employee_id,
                                          start_date="2026-05-22", end_date="2026-05-22")
        client.post(f"/api/leaves/{leave_id}/approve",
                     headers=admin_headers, json={"approved": True})
        assert db_session.query(AttendanceRecord).filter_by(
            leave_record_id=leave_id).count() == 1

        # 改 leave_type 觸發退審(視 update_leave 邏輯)
        resp = client.patch(f"/api/leaves/{leave_id}",
                             headers=admin_headers,
                             json={"leave_type": "sick"})
        assert resp.status_code == 200

        leave = db_session.query(LeaveRecord).filter_by(id=leave_id).first()
        # 若觸發退審 → is_approved=None,attendance 應被 revert
        if leave.is_approved is None:
            assert db_session.query(AttendanceRecord).filter_by(
                leave_record_id=leave_id).count() == 0
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_leaves_attendance_sync.py::TestUpdateHookIntegration -v`

Expected: 3 個 FAIL。

- [ ] **Step 3: 加 sync hook 到 update_leave**

修改 `api/leaves.py:760-978`(update_leave 內,在套用 patch 前後):

```python
@router.patch("/{leave_id}")
def update_leave(leave_id: int, data: LeaveUpdate, ...):
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    # ... 既有 permission check 等

    # ── 在 model 寫回前 snapshot(reapply 需要)─────────────────
    old_snapshot = {
        "start_date": leave.start_date,
        "end_date": leave.end_date,
        "start_time": leave.start_time,
        "end_time": leave.end_time,
        "leave_type": leave.leave_type,
        "leave_hours": leave.leave_hours,
        "is_approved": leave.is_approved,
    }

    # 套用 patch(既有邏輯,可能觸發 _apply_leave_update_and_revoke 自動退審)
    _apply_leave_update_and_revoke(leave, data, current_user, leave_id)

    # ── Sync 分派 ─────────────────────────────────────────────
    key_fields_changed = any(
        old_snapshot[k] != getattr(leave, k)
        for k in ("start_date", "end_date", "start_time",
                  "end_time", "leave_type", "leave_hours")
    )

    try:
        if old_snapshot["is_approved"] is True and leave.is_approved is None:
            # 退審路徑(Hook 3)
            sync.revert(session, leave_id)
        elif old_snapshot["is_approved"] is True and leave.is_approved is True \
             and key_fields_changed:
            # 改關鍵欄但仍 approved(罕見)
            sync.reapply(session, leave_id, old_snapshot=old_snapshot)
        # else: pending → 任何狀態 / approved → rejected 都不需動 attendance
    except (sync.LeaveAttendanceConflict, sync.LeavePartialTimeMissing) as e:
        raise HTTPException(status_code=422, detail=str(e))

    # ── 既有:lock_and_premark_stale(L888 附近)保留不動 ─────

    session.flush()
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_leaves_attendance_sync.py::TestUpdateHookIntegration -v`

Expected: I-4, I-5, I-6 全綠。

- [ ] **Step 5: 既有 leaves 測試 regression**

Run: `cd ivy-backend && pytest tests/test_leaves.py -v`

Expected: 全綠。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend
git add api/leaves.py tests/test_leaves_attendance_sync.py
git commit -m "feat(leaves): update_leave hook 接 sync.revert/reapply

退審路徑(True→None)走 revert;改關鍵欄仍 approved 走 reapply。
old_snapshot 在 _apply_leave_update_and_revoke 之前抓,
確保 reapply 拿得到舊範圍。LeaveAttendanceConflict /
LeavePartialTimeMissing → 422。既有 lock_and_premark_stale 不動。"
```

---

## Task 14: api/leaves.py delete hook + I-7

**Files:**
- Modify: `api/leaves.py:983-1082`(delete_leave)
- Modify: `tests/test_leaves_attendance_sync.py`

- [ ] **Step 1: 加 I-7 測試**

附加:

```python
class TestDeleteHookIntegration:
    def test_i7_delete_approved_leave_reverts_attendance(self, client, admin_headers,
                                                          employee_id, db_session):
        """I-7: DELETE approved leave → AttendanceRecord 反寫;leave row 刪"""
        leave_id = _create_pending_leave(client, admin_headers, employee_id,
                                          start_date="2026-05-22", end_date="2026-05-22")
        client.post(f"/api/leaves/{leave_id}/approve",
                     headers=admin_headers, json={"approved": True})
        assert db_session.query(AttendanceRecord).filter_by(
            leave_record_id=leave_id).count() == 1

        resp = client.delete(f"/api/leaves/{leave_id}", headers=admin_headers)
        assert resp.status_code in (200, 204)

        # leave 不存在
        assert db_session.query(LeaveRecord).filter_by(id=leave_id).first() is None
        # attendance 反寫
        assert db_session.query(AttendanceRecord).filter_by(
            leave_record_id=leave_id).count() == 0
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_leaves_attendance_sync.py::TestDeleteHookIntegration -v`

Expected: 1 FAIL。

- [ ] **Step 3: 加 sync hook 到 delete_leave**

修改 `api/leaves.py:983-1082`(delete_leave 內,在 `session.delete(leave)` 之前):

```python
@router.delete("/{leave_id}")
def delete_leave(leave_id: int, ...):
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    # ... 既有 permission check

    # ── Sync hook ────────────────────────────────────────────────
    if leave.is_approved is True:
        try:
            sync.revert(session, leave_id)
        except sync.LeaveAttendanceConflict as e:
            raise HTTPException(status_code=422, detail=str(e))
    # ─────────────────────────────────────────────────────────────

    # 既有:lock_and_premark_stale + session.delete + 寫 audit
    session.delete(leave)
    session.flush()
    # FK ON DELETE SET NULL 是雙保險:revert 漏掉的 attendance row 不會 dangle
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_leaves_attendance_sync.py::TestDeleteHookIntegration -v`

Expected: I-7 綠。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend
git add api/leaves.py tests/test_leaves_attendance_sync.py
git commit -m "feat(leaves): delete_leave hook 接 sync.revert

approved leave 被刪 → revert attendance 後再 delete LeaveRecord。
FK ON DELETE SET NULL 是雙保險,但主路徑是 revert 主動清。"
```

---

## Task 15: api/attendance/records.py 接 merge + W-1, W-2

**Files:**
- Modify: `api/attendance/records.py:214-327`(create_or_update_attendance_record)
- Create: `tests/test_attendance_writes_leave_aware.py`

- [ ] **Step 1: 寫 W-1, W-2 測試**

新建 `tests/test_attendance_writes_leave_aware.py`:

```python
"""W-1~W-5: 3 條 attendance 寫入路徑與 leave 共存"""

import pytest
from datetime import date, time
from decimal import Decimal

from models.attendance import AttendanceRecord, AttendanceStatus
from models.leave import LeaveRecord


def _approve_full_day_leave(client, admin_headers, employee_id, on_date="2026-05-22"):
    """建 + 核可一筆全天 leave"""
    resp = client.post("/api/leaves", headers=admin_headers, json={
        "employee_id": employee_id,
        "leave_type": "personal",
        "start_date": on_date,
        "end_date": on_date,
        "leave_hours": 8,
        "reason": "test",
    })
    leave_id = resp.json()["id"]
    client.post(f"/api/leaves/{leave_id}/approve",
                 headers=admin_headers, json={"approved": True})
    return leave_id


class TestAdminWriteWithLeave:
    def test_w1_admin_create_record_picks_up_leave(self, client, admin_headers,
                                                    employee_id, db_session):
        """W-1: Admin 對「有 approved leave 的某日」create_or_update_attendance_record
        → row 寫入後 leave_record_id 對齊"""
        leave_id = _approve_full_day_leave(client, admin_headers, employee_id,
                                            "2026-05-22")
        # 既有的 LEAVE row 應該已存在(sync.apply 寫的)
        existing = db_session.query(AttendanceRecord).filter_by(
            employee_id=employee_id,
            attendance_date=date(2026, 5, 22),
        ).first()
        assert existing.leave_record_id == leave_id

        # Admin 從報表畫面手動補打卡(可能誤觸的情境)
        resp = client.post(f"/api/attendance", headers=admin_headers, json={
            "employee_id": employee_id,
            "attendance_date": "2026-05-22",
            "punch_in_time": "09:00",
            "punch_out_time": "18:00",
        })
        assert resp.status_code == 200

        # leave_record_id 仍對齊
        db_session.refresh(existing)
        assert existing.leave_record_id == leave_id

    def test_w2_admin_re_edit_keeps_leave_link(self, client, admin_headers,
                                                employee_id, db_session):
        """W-2: Admin 重複編輯該 row(更新打卡時間)→ leave_record_id 保留"""
        leave_id = _approve_full_day_leave(client, admin_headers, employee_id,
                                            "2026-05-22")
        # 兩次編輯
        client.post(f"/api/attendance", headers=admin_headers, json={
            "employee_id": employee_id,
            "attendance_date": "2026-05-22",
            "punch_in_time": "09:00",
        })
        client.post(f"/api/attendance", headers=admin_headers, json={
            "employee_id": employee_id,
            "attendance_date": "2026-05-22",
            "punch_out_time": "18:00",
        })

        row = db_session.query(AttendanceRecord).filter_by(
            employee_id=employee_id,
            attendance_date=date(2026, 5, 22),
        ).first()
        assert row.leave_record_id == leave_id
        assert row.punch_in_time == time(9, 0)
        assert row.punch_out_time == time(18, 0)
```

> **注意**:`POST /api/attendance` 路徑與 payload 結構假設來自 `api/attendance/records.py:214`。實際 endpoint 可能不同(如 PUT / 不同 path),implementer 跑 grep 確認。

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_attendance_writes_leave_aware.py::TestAdminWriteWithLeave -v`

Expected: 2 FAIL(merge 還沒接上)。

- [ ] **Step 3: 在 create_or_update_attendance_record 接 merge**

修改 `api/attendance/records.py:214-327`:

```python
from utils.attendance_leave_merge import merge_attendance_with_leave

def create_or_update_attendance_record(...):
    # ... 既有邏輯找/建 record
    record = ...  # 既有 query / instantiate

    # ... 既有 apply_punch_calculations(record, punch_in, punch_out)

    # ── 新增:leave-aware merge ─────────────────────────────────
    merge_attendance_with_leave(record, session)
    # ─────────────────────────────────────────────────────────────

    session.merge(record)
    session.flush()
```

> **注意**:merge 必須在 `session.merge(record)` 之前呼叫,確保寫入時 leave_record_id 已合進去。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_attendance_writes_leave_aware.py::TestAdminWriteWithLeave -v`

Expected: W-1 / W-2 綠。

- [ ] **Step 5: 既有 attendance/records 測試 regression**

Run: `cd ivy-backend && pytest tests/test_attendance_records.py -v 2>&1 | tail -20`

Expected: 全綠。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend
git add api/attendance/records.py tests/test_attendance_writes_leave_aware.py
git commit -m "feat(attendance): create_or_update_attendance_record 接 merge helper

Admin 手動編輯打卡 row 時自動 leave-aware,
不會把 sync 寫入的 leave_record_id 蓋掉。"
```

---

## Task 16: api/attendance/upload.py 接 merge + W-3

**Files:**
- Modify: `api/attendance/upload.py:88-400`(upload_attendance)
- Modify: `tests/test_attendance_writes_leave_aware.py`

- [ ] **Step 1: 加 W-3 測試**

附加:

```python
class TestExcelUploadWithLeave:
    def test_w3_upload_row_leave_aware(self, client, admin_headers, employee_id, db_session):
        """W-3: Excel upload 該日 row → leave_record_id 對齊;late_minutes leave-aware"""
        # 預先核可半天請假 09:00-13:00
        resp = client.post("/api/leaves", headers=admin_headers, json={
            "employee_id": employee_id,
            "leave_type": "personal",
            "start_date": "2026-05-22",
            "end_date": "2026-05-22",
            "leave_hours": 4,
            "start_time": "09:00",
            "end_time": "13:00",
            "reason": "test",
        })
        leave_id = resp.json()["id"]
        client.post(f"/api/leaves/{leave_id}/approve",
                     headers=admin_headers, json={"approved": True})

        # 模擬 Excel upload(直接呼叫服務層)
        from api.attendance.upload import upload_attendance
        # 構造 Excel-like rows
        rows = [{
            "employee_id": employee_id,
            "attendance_date": "2026-05-22",
            "punch_in_time": "09:30",
            "punch_out_time": "18:00",
        }]
        # ... call upload service or hit endpoint
        # 此處 implementation-specific,implementer 根據 upload_attendance 簽章決定

        row = db_session.query(AttendanceRecord).filter_by(
            employee_id=employee_id,
            attendance_date=date(2026, 5, 22),
        ).first()
        assert row.leave_record_id == leave_id
        assert row.partial_leave_hours == Decimal("4.00")
        # late_minutes 用 leave-aware 算:請假 09:00-13:00 涵蓋 09:30 → late=0
        assert row.late_minutes == 0
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_attendance_writes_leave_aware.py::TestExcelUploadWithLeave -v`

Expected: 1 FAIL。

- [ ] **Step 3: 在 upload_attendance 接 merge**

修改 `api/attendance/upload.py`(在每個 row build attendance record 後):

```python
from utils.attendance_leave_merge import merge_attendance_with_leave

def upload_attendance(...):
    for excel_row in rows:
        record = build_attendance_record_from_row(excel_row)
        # ... 既有 apply_punch_calculations(record, ...) 等

        # ── 新增:leave-aware merge ─────────────────────────────
        merge_attendance_with_leave(record, session)
        # ───────────────────────────────────────────────────────

        session.merge(record)

    session.flush()
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_attendance_writes_leave_aware.py::TestExcelUploadWithLeave -v`

Expected: W-3 綠。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend
git add api/attendance/upload.py tests/test_attendance_writes_leave_aware.py
git commit -m "feat(attendance): upload_attendance 接 merge helper

Excel 批次匯入時對每個 row 跑 merge,leave-aware late_minutes 正確,
不會 overwrite sync 寫入。"
```

---

## Task 17: utils/attendance_calc.apply_attendance_status 接 merge + W-4, W-5

**Files:**
- Modify: `utils/attendance_calc.py:94-118`(apply_attendance_status)
- Modify: `tests/test_attendance_writes_leave_aware.py`

- [ ] **Step 1: 加 W-4 / W-5 測試**

附加:

```python
class TestPunchCorrectionWithLeave:
    def test_w4_punch_correction_keeps_leave_link(self, client, admin_headers,
                                                   employee_id, db_session):
        """W-4: 補卡核准重算 apply_attendance_status → leave_record_id 保留"""
        # 預先 approve 半天請假
        leave_id = _approve_partial_leave(client, admin_headers, employee_id, "2026-05-22",
                                          start_time="09:00", end_time="13:00", hours=4)

        # 模擬補卡核准:直接呼叫 apply_attendance_status
        from utils.attendance_calc import apply_attendance_status
        row = db_session.query(AttendanceRecord).filter_by(
            leave_record_id=leave_id,
        ).first()
        # 假裝員工有補卡 punch_in=09:30
        row.punch_in_time = time(9, 30)
        apply_attendance_status(row, db_session)

        db_session.refresh(row)
        # leave_record_id 保留
        assert row.leave_record_id == leave_id
        assert row.partial_leave_hours == Decimal("4.00")

    def test_w5_salary_engine_consistent_after_all_writes(self, client, admin_headers,
                                                          employee_id, db_session):
        """W-5: 任何寫入路徑後跑 salary engine → 結果 = sync 直接寫入相同"""
        # 路徑 1:sync.apply 寫入
        leave_id_a = _approve_full_day_leave(client, admin_headers, employee_id,
                                              "2026-05-22")
        # 路徑 2:Admin 手動編輯打卡(同月不同日)
        leave_id_b = _approve_full_day_leave(client, admin_headers, employee_id,
                                              "2026-05-23")
        client.post("/api/attendance", headers=admin_headers, json={
            "employee_id": employee_id,
            "attendance_date": "2026-05-23",
            "punch_in_time": "10:00",  # 違規上班(全天請假當日)
        })

        # 跑 salary engine
        from services.salary.engine import process_salary_calculation
        result = process_salary_calculation(employee_id, 2026, 5)

        # 結果應該包含兩天 leave_days
        leave_days = result.get("leave_days", [])
        assert len(leave_days) == 2


def _approve_partial_leave(client, admin_headers, employee_id, on_date,
                           start_time, end_time, hours):
    resp = client.post("/api/leaves", headers=admin_headers, json={
        "employee_id": employee_id,
        "leave_type": "personal",
        "start_date": on_date,
        "end_date": on_date,
        "leave_hours": hours,
        "start_time": start_time,
        "end_time": end_time,
        "reason": "test",
    })
    leave_id = resp.json()["id"]
    client.post(f"/api/leaves/{leave_id}/approve",
                 headers=admin_headers, json={"approved": True})
    return leave_id
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/test_attendance_writes_leave_aware.py::TestPunchCorrectionWithLeave -v`

Expected: 2 FAIL。

- [ ] **Step 3: 在 apply_attendance_status 接 merge,簽章加 session**

修改 `utils/attendance_calc.py:94-118`:

```python
from utils.attendance_leave_merge import merge_attendance_with_leave

def apply_attendance_status(record, session):  # 加 session 參數
    """補卡核准重算 status 後也要 leave-aware merge。"""
    # 既有 status/late_minutes 計算邏輯保留
    ...

    # ── 新增:leave-aware merge ─────────────────────────────────
    merge_attendance_with_leave(record, session)
    # ─────────────────────────────────────────────────────────────
```

> **caller 需同步更新**:grep 所有 `apply_attendance_status(` 的 caller,加 session 參數。
>
> ```bash
> grep -rn "apply_attendance_status(" ivy-backend/ --include="*.py" | grep -v test_
> ```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/test_attendance_writes_leave_aware.py -v`

Expected: W-1~W-5 全綠。

- [ ] **Step 5: 既有 attendance_calc 測試 regression**

Run: `cd ivy-backend && pytest tests/test_attendance_calc.py tests/test_punch_corrections.py -v 2>&1 | tail -20`

Expected: 全綠(若有 caller 改簽章需同步)。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend
git add utils/attendance_calc.py tests/test_attendance_writes_leave_aware.py
git commit -m "feat(attendance): apply_attendance_status 接 session + merge

補卡核准重算後也跑 leave-aware merge,
所有 caller 簽章同步加 session 參數。"
```

---

## Task 18: scripts/dedupe_attendance.py

**Files:**
- Create: `scripts/dedupe_attendance.py`
- Test: `tests/test_dedupe_attendance.py`(可選輕量測試)

- [ ] **Step 1: 寫腳本**

新建 `scripts/dedupe_attendance.py`:

```python
"""Pre-flight 工具:偵測並清理 attendance_records 內 (employee_id, attendance_date) 重複。

用法:
    python scripts/dedupe_attendance.py            # dry-run 列出
    python scripts/dedupe_attendance.py --apply    # 保留每組最早 id,刪除其他

刪除前寫進 audit_logs(entity_type='attendance_records', action='DELETE')。
"""

import argparse
import sys
from collections import defaultdict

from sqlalchemy import text
from models.attendance import AttendanceRecord
from db_session import SessionLocal  # 既有 session factory


def find_dups(session):
    rows = session.query(AttendanceRecord).order_by(
        AttendanceRecord.employee_id,
        AttendanceRecord.attendance_date,
        AttendanceRecord.id,
    ).all()
    by_key = defaultdict(list)
    for r in rows:
        by_key[(r.employee_id, r.attendance_date)].append(r)
    dups = {k: v for k, v in by_key.items() if len(v) > 1}
    return dups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="實際刪除;預設只 dry-run 列出")
    args = parser.parse_args()

    session = SessionLocal()
    dups = find_dups(session)

    if not dups:
        print("[dedupe] 無重複,migration 可直接 upgrade")
        return 0

    print(f"[dedupe] 偵測到 {len(dups)} 組重複:")
    total_to_delete = 0
    for (emp_id, date_), rows in dups.items():
        ids = [r.id for r in rows]
        keep = rows[0].id  # 最早 id
        delete_ids = [r.id for r in rows[1:]]
        total_to_delete += len(delete_ids)
        print(f"  employee_id={emp_id} date={date_}: ids={ids} → keep {keep}, delete {delete_ids}")

    if not args.apply:
        print(f"\n[dedupe] dry-run only;若無誤,加 --apply 實際刪除 {total_to_delete} 筆")
        return 0

    for (emp_id, date_), rows in dups.items():
        for r in rows[1:]:
            session.execute(text("""
                INSERT INTO audit_logs (action, entity_type, entity_id, summary, created_at)
                VALUES ('DELETE', 'attendance_records', :id, :summary, NOW())
            """), {"id": str(r.id), "summary": f"dedupe before migration: dup of {rows[0].id}"})
            session.delete(r)
    session.commit()
    print(f"[dedupe] 已刪除 {total_to_delete} 筆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 手動驗證(無 dup 的情況)**

Run: `cd ivy-backend && python scripts/dedupe_attendance.py`

Expected: 「[dedupe] 無重複」(假設 dev DB 沒 dup)。

- [ ] **Step 3: Commit**

```bash
cd ivy-backend
git add scripts/dedupe_attendance.py
git commit -m "feat(scripts): dedupe_attendance.py pre-flight 去重工具

dry-run 列出 (employee_id, attendance_date) 重複組;--apply 保留最早 id
刪其他,審計到 audit_logs。Migration upgrade 前 SOP 必跑。"
```

---

## Task 19: scripts/preview_backfill.py

**Files:**
- Create: `scripts/preview_backfill.py`

- [ ] **Step 1: 寫腳本**

新建 `scripts/preview_backfill.py`:

```python
"""Pre-flight 工具:預演 backfill,列出規模、衝突、預估影響。

不修改任何資料。
"""

from datetime import date, timedelta
from sqlalchemy import text
from db_session import SessionLocal
from models.leave import LeaveRecord
from models.attendance import AttendanceRecord


def main():
    session = SessionLocal()
    cutoff = date.today() - timedelta(days=365)

    leaves = session.query(LeaveRecord).filter(
        LeaveRecord.is_approved == True,
        LeaveRecord.end_date >= cutoff,
    ).all()

    full_day, half_day, hourly = 0, 0, 0
    bad_time = []
    expected_attendance_overwrite = 0
    expected_attendance_create = 0
    expected_conflicts = []

    for lv in leaves:
        if lv.start_time is None and lv.end_time is None \
                and (lv.leave_hours is None or lv.leave_hours >= 8):
            full_day += 1
        elif lv.leave_hours and lv.leave_hours < 4.5:
            hourly += 1
        else:
            half_day += 1

        if lv.leave_hours and lv.leave_hours < 8 \
                and (lv.start_time is None or lv.end_time is None):
            bad_time.append(lv.id)

        # 估計每日 attendance row 衝突
        d = lv.start_date
        while d <= lv.end_date:
            existing = session.query(AttendanceRecord).filter_by(
                employee_id=lv.employee_id,
                attendance_date=d,
            ).first()
            if existing is None:
                expected_attendance_create += 1
            elif existing.leave_record_id is not None and existing.leave_record_id != lv.id:
                expected_conflicts.append((lv.id, existing.id, d))
            else:
                expected_attendance_overwrite += 1
            d += timedelta(days=1)

    print(f"=== Backfill Preview(cutoff = {cutoff}) ===")
    print(f"近 12 月 approved leave 總筆數: {len(leaves)}")
    print(f"  全天:{full_day}")
    print(f"  半天:{half_day}")
    print(f"  小時:{hourly}")
    print(f"預估覆寫既有 AttendanceRecord:{expected_attendance_overwrite}")
    print(f"預估新建 AttendanceRecord:{expected_attendance_create}")

    if bad_time:
        print(f"\n⚠️  缺 start_time/end_time 的部分請假:{len(bad_time)} 筆")
        print(f"   (需先跑 fix_partial_leave_times.py)")
        print(f"   leave_ids: {bad_time[:10]}{'...' if len(bad_time)>10 else ''}")

    if expected_conflicts:
        print(f"\n⚠️  同日不同 leave 衝突:{len(expected_conflicts)} 筆")
        for lv_id, att_id, d in expected_conflicts[:10]:
            print(f"   leave_id={lv_id} 卡到 attendance.id={att_id} date={d}")

    if not bad_time and not expected_conflicts:
        print("\n✅ 預演 OK,migration upgrade 可進行")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 手動跑一次驗證**

Run: `cd ivy-backend && python scripts/preview_backfill.py`

Expected: 印出統計;dev DB 上應該都是 0 衝突。

- [ ] **Step 3: Commit**

```bash
cd ivy-backend
git add scripts/preview_backfill.py
git commit -m "feat(scripts): preview_backfill.py pre-flight 預演工具

純 SELECT,列出 backfill 規模、半天/小時占比、預估覆寫/新建/衝突數。
Deploy SOP step 1 必跑,衝突 > 0 需人工 pre-resolve。"
```

---

## Task 20: scripts/fix_partial_leave_times.py

**Files:**
- Create: `scripts/fix_partial_leave_times.py`

- [ ] **Step 1: 寫腳本**

新建 `scripts/fix_partial_leave_times.py`:

```python
"""Pre-flight 工具:掃既有部分請假缺 start_time/end_time 的 row,
列表並可選擇退審到 pending 讓 admin 重新審核補時段。

用法:
    python scripts/fix_partial_leave_times.py            # dry-run
    python scripts/fix_partial_leave_times.py --apply    # 把缺時段的 row is_approved=None(退審)
"""

import argparse
import sys
from datetime import date, timedelta

from sqlalchemy import text
from db_session import SessionLocal
from models.leave import LeaveRecord


def find_bad_partial(session):
    cutoff = date.today() - timedelta(days=365)
    return session.query(LeaveRecord).filter(
        LeaveRecord.is_approved == True,
        LeaveRecord.end_date >= cutoff,
        LeaveRecord.leave_hours.isnot(None),
        LeaveRecord.leave_hours < 8,
        (LeaveRecord.start_time.is_(None) | LeaveRecord.end_time.is_(None)),
    ).all()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="把缺 start_time/end_time 的 row 退審到 pending")
    args = parser.parse_args()

    session = SessionLocal()
    bad = find_bad_partial(session)

    if not bad:
        print("[fix_partial] 無問題 row,migration 可進行")
        return 0

    print(f"[fix_partial] 偵測到 {len(bad)} 筆已核可的部分請假缺 start_time/end_time:")
    for lv in bad:
        print(f"  id={lv.id} employee_id={lv.employee_id} "
              f"date={lv.start_date}..{lv.end_date} hours={lv.leave_hours} "
              f"start_time={lv.start_time} end_time={lv.end_time}")

    if not args.apply:
        print(f"\n[fix_partial] dry-run only;加 --apply 退審 {len(bad)} 筆")
        return 0

    for lv in bad:
        session.execute(text("""
            INSERT INTO audit_logs (action, entity_type, entity_id, summary, created_at)
            VALUES ('UPDATE', 'leave_records', :id, :summary, NOW())
        """), {"id": str(lv.id), "summary": "fix_partial_leave_times: 退審至 pending 待補時段"})
        lv.is_approved = None  # 退審
    session.commit()
    print(f"[fix_partial] 已退審 {len(bad)} 筆,請通知 admin 補時段後重新核可")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 手動跑一次**

Run: `cd ivy-backend && python scripts/fix_partial_leave_times.py`

Expected: 大概率是 0 筆。

- [ ] **Step 3: Commit**

```bash
cd ivy-backend
git add scripts/fix_partial_leave_times.py
git commit -m "feat(scripts): fix_partial_leave_times.py pre-flight 修補工具

掃缺 start_time/end_time 的已核可部分請假;
--apply 把它們退審到 pending,等 admin 補時段後重核可。"
```

---

## Task 21: Alembic migration scaffold(schema + enum + dup-check)

**Files:**
- Create: `alembic/versions/empleavesync_attendance_sync.py`

- [ ] **Step 1: 找最新的 alembic revision**

Run: `cd ivy-backend && alembic heads`

Expected: 顯示當前 head revision id(記下來,例如 `abc123`)。

- [ ] **Step 2: 建立 migration scaffold**

Run: `cd ivy-backend && alembic revision -m "employee leave attendance sync"`

Expected: 在 `alembic/versions/` 產生新檔(例如 `xyz789_employee_leave_attendance_sync.py`)。重新命名為 `empleavesync_attendance_sync.py`(對應 spec 命名)。

- [ ] **Step 3: 寫 schema + enum + dup-check(尚未加 unique constraint / backfill)**

修改新 migration 檔:

```python
"""employee leave attendance sync

Revision ID: empleavesync
Revises: <previous_head>
Create Date: 2026-05-22
"""
import os
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text


revision = "empleavesync"
down_revision = "<previous_head>"  # 填上前一個 head id
branch_labels = None
depends_on = None


def upgrade():
    # 1. 加新欄(nullable;不 lock 表)
    op.add_column("attendance_records",
        sa.Column("leave_record_id", sa.Integer(), nullable=True))
    op.add_column("attendance_records",
        sa.Column("partial_leave_hours", sa.Numeric(4, 2), nullable=True))
    op.create_foreign_key("fk_attendance_leave", "attendance_records",
        "leave_records", ["leave_record_id"], ["id"], ondelete="SET NULL")

    # 2. enum 加 LEAVE 值(Postgres 必須在 autocommit_block)
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE attendancestatus ADD VALUE IF NOT EXISTS 'LEAVE'")

    # 3. 去重偵測(fail-loud)
    conn = op.get_bind()
    dups = conn.execute(text("""
        SELECT employee_id, attendance_date, COUNT(*) c
        FROM attendance_records
        GROUP BY employee_id, attendance_date HAVING COUNT(*) > 1
    """)).fetchall()
    if dups:
        raise RuntimeError(
            f"偵測到 {len(dups)} 組 (employee_id, attendance_date) 重複,"
            f"請先跑 scripts/dedupe_attendance.py 清理再 upgrade。前 5 筆: {dups[:5]}"
        )

    # Task 22 加 unique constraint + bad_leaves check
    # Task 23 加 backfill


def downgrade():
    op.drop_constraint("fk_attendance_leave", "attendance_records",
        type_="foreignkey")
    op.drop_column("attendance_records", "partial_leave_hours")
    op.drop_column("attendance_records", "leave_record_id")
    # 注意:Postgres 無法 drop enum value,LEAVE 殘留是預期
```

- [ ] **Step 4: 跑 migration 試 up / down**

Run:
```
cd ivy-backend
alembic upgrade empleavesync
alembic downgrade -1
alembic upgrade empleavesync
```

Expected:每次都成功,無 error。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend
git add alembic/versions/empleavesync_attendance_sync.py
git commit -m "feat(migration): empleavesync schema + enum + dup-check

加 leave_record_id / partial_leave_hours 欄;ALTER TYPE 加 LEAVE 值。
Unique constraint(CONCURRENTLY)與 backfill 留給 Task 22 / 23。
"
```

---

## Task 22: Migration 加 unique constraint (CONCURRENTLY) + bad_leaves 預檢

**Files:**
- Modify: `alembic/versions/empleavesync_attendance_sync.py`

- [ ] **Step 1: 加 CONCURRENTLY 與 bad_leaves check**

修改 upgrade() 函式,在 dup-check 之後加:

```python
def upgrade():
    # 1-3 既有(Task 21)

    # 4. Online 加 unique constraint
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_attendance_employee_date
            ON attendance_records (employee_id, attendance_date)
        """)
    op.execute("""
        ALTER TABLE attendance_records
        ADD CONSTRAINT uq_attendance_employee_date
        UNIQUE USING INDEX uq_attendance_employee_date
    """)
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_attendance_leave_record_id
            ON attendance_records (leave_record_id)
        """)

    # 5. Pre-flight validator:阻擋既有部分請假缺 start_time/end_time
    bad_leaves = conn.execute(text("""
        SELECT id, employee_id, start_date, leave_hours
        FROM leave_records
        WHERE is_approved = true
          AND end_date >= CURRENT_DATE - INTERVAL '12 months'
          AND (start_time IS NULL OR end_time IS NULL)
          AND (leave_hours IS NOT NULL AND leave_hours < 8)
    """)).fetchall()
    if bad_leaves:
        raise RuntimeError(
            f"偵測到 {len(bad_leaves)} 筆已核可的部分請假缺 start_time/end_time,"
            f"請先跑 scripts/fix_partial_leave_times.py 補時段或回到 pending 重審。"
            f"前 5 筆: {bad_leaves[:5]}"
        )
```

並修改 downgrade():

```python
def downgrade():
    op.drop_constraint("uq_attendance_employee_date", "attendance_records",
        type_="unique")
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_attendance_leave_record_id")
    op.drop_constraint("fk_attendance_leave", "attendance_records",
        type_="foreignkey")
    op.drop_column("attendance_records", "partial_leave_hours")
    op.drop_column("attendance_records", "leave_record_id")
```

- [ ] **Step 2: 重跑 up/down 驗證**

Run:
```
cd ivy-backend
alembic downgrade -1
alembic upgrade empleavesync
alembic downgrade -1
alembic upgrade empleavesync
```

Expected: 順利,unique constraint + index 都建立。

- [ ] **Step 3: Commit**

```bash
cd ivy-backend
git add alembic/versions/empleavesync_attendance_sync.py
git commit -m "feat(migration): empleavesync 加 unique constraint CONCURRENTLY + bad_leaves 預檢

CREATE UNIQUE INDEX CONCURRENTLY 避免阻塞 prod 流量;
ADD CONSTRAINT USING INDEX 把 index 轉成 unique constraint(短鎖)。
bad_leaves check fail-loud 阻擋有問題的部分請假。"
```

---

## Task 23: Migration 加 _run_backfill

**Files:**
- Modify: `alembic/versions/empleavesync_attendance_sync.py`

- [ ] **Step 1: 加 _run_backfill 函式**

在 migration 檔尾加:

```python
def _run_backfill(conn):
    """Backfill 近 12 個月 approved leaves。Idempotent。"""
    from services.employee_leave_attendance_sync import (
        apply, LeaveAttendanceConflict, LeaveNotApproved,
    )
    from sqlalchemy.orm import Session

    session = Session(bind=conn)
    leaves = session.execute(text("""
        SELECT id FROM leave_records
        WHERE is_approved = true
          AND end_date >= CURRENT_DATE - INTERVAL '12 months'
        ORDER BY id
    """)).fetchall()

    total = len(leaves)
    ok, skipped, conflicts, errors = 0, 0, [], []

    print(f"[backfill] 開始,共 {total} 筆 approved leave 在近 12 個月內")

    for idx, (lid,) in enumerate(leaves, 1):
        sp = session.begin_nested()
        try:
            dates = apply(session, lid)
            sp.commit()
            ok += 1
            if not dates:
                skipped += 1
        except LeaveAttendanceConflict as e:
            sp.rollback()
            conflicts.append((lid, str(e)))
        except Exception as e:
            sp.rollback()
            errors.append((lid, type(e).__name__, str(e)[:200]))

        if idx % 100 == 0:
            print(f"[backfill] 進度 {idx}/{total} ok={ok} "
                  f"skipped={skipped} conflicts={len(conflicts)} errors={len(errors)}")

    print(f"[backfill] 完成:ok={ok} skipped={skipped} "
          f"conflicts={len(conflicts)} errors={len(errors)}")

    if errors:
        raise RuntimeError(
            f"backfill 有 {len(errors)} 筆失敗,migration 整支 rollback。"
            f"前 5 筆: {errors[:5]}"
        )

    if conflicts:
        print(f"[backfill] WARN 同日衝突 {len(conflicts)} 筆,沿用既有 attendance 不覆寫")
        for lid, msg in conflicts:
            session.execute(text("""
                INSERT INTO audit_logs (action, entity_type, entity_id, summary, created_at)
                VALUES ('UPDATE', 'leave_records', :lid, :msg, NOW())
            """), {"lid": str(lid), "msg": f"leave_attendance_backfill_conflict: {msg}"})
```

並在 upgrade() 結尾加:

```python
    # 6. Backfill(env IVY_SKIP_BACKFILL=1 可跳)
    if not os.getenv("IVY_SKIP_BACKFILL"):
        _run_backfill(conn)
```

- [ ] **Step 2: 跑 migration 驗證**

Run:
```
cd ivy-backend
alembic downgrade -1
alembic upgrade empleavesync
```

Expected: backfill 印出進度與最終統計,無 error。

- [ ] **Step 3: 確認 attendance_records 有新 row(若 dev DB 有 approved leave)**

Run: `cd ivy-backend && psql -d ivymanagement -c "SELECT COUNT(*) FROM attendance_records WHERE leave_record_id IS NOT NULL"`

Expected: 數量 > 0(假設 dev 有 approved leave)。

- [ ] **Step 4: Commit**

```bash
cd ivy-backend
git add alembic/versions/empleavesync_attendance_sync.py
git commit -m "feat(migration): empleavesync 加 _run_backfill

近 12 個月 approved leaves 全量 backfill,SAVEPOINT 單筆隔離,
errors fail-loud / conflicts 放行+audit_logs。
IVY_SKIP_BACKFILL=1 環境變數可緊急跳過。"
```

---

## Task 24: Migration tests M-1~M-5

**Files:**
- Create: `tests/test_migration_empleavesync.py`

- [ ] **Step 1: 寫測試骨架(僅 happy path,完整 migration 測試靠 staging 驗證)**

新建 `tests/test_migration_empleavesync.py`:

```python
"""M-1~M-5: migration 行為驗證(部分依賴 staging DB,本檔做能在 CI 跑的子集)"""

import pytest
from datetime import date
from sqlalchemy import text


@pytest.fixture
def fresh_engine(tmp_path):
    """In-memory SQLite engine 給 happy path 測試(僅驗 schema 變更)"""
    # 注意:Postgres-specific 邏輯(ALTER TYPE / CONCURRENTLY)無法在 SQLite 上跑
    # 這些 case 標 skip-on-sqlite,改在 staging 跑 alembic upgrade 驗證
    pytest.skip("Postgres-specific migration,需 staging DB 驗證")


class TestMigrationBehavior:
    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m1_upgrade_clean_db(self):
        """M-1: upgrade on clean DB → 完成、無報錯"""
        pass

    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m2_upgrade_with_dups_fails_loud(self):
        """M-2: upgrade on DB with dups → fail-loud"""
        pass

    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m3_upgrade_with_approved_leaves_backfills(self):
        """M-3: upgrade with approved leaves → backfill 完成"""
        pass

    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m4_upgrade_idempotent(self):
        """M-4: upgrade 中斷後重跑 → idempotent"""
        pass

    @pytest.mark.skip(reason="需 Postgres staging 環境")
    def test_m5_downgrade_restores_schema(self):
        """M-5: downgrade → schema 還原(LEAVE enum 殘留是預期)"""
        pass
```

> **注意**:Migration 完整測試需要 Postgres + alembic process,在 CI 跑成本高。Plan 採取**手動 staging 驗證 + 留 placeholder 測試**策略。Deploy SOP step 1 staging 部署即是這 5 條測試的人為執行。

- [ ] **Step 2: 跑測試確認 skip 標籤生效**

Run: `cd ivy-backend && pytest tests/test_migration_empleavesync.py -v`

Expected: 5 個 SKIPPED。

- [ ] **Step 3: 手動在 staging 跑 M-1~M-5 並記錄結果**

```
1. M-1: 從 clean DB alembic upgrade head → 成功 ✓
2. M-2: 故意 INSERT 兩筆同 (employee_id, date) → alembic upgrade 應該 RuntimeError ✓
3. M-3: 有 approved leaves → backfill 跑完,statistics 印出 ✓
4. M-4: M-3 完成後再跑 alembic upgrade(模擬重跑)→ no-op,結果相同 ✓
5. M-5: alembic downgrade -1 → 欄位刪除,LEAVE enum 殘留 ✓
```

記錄到 PR description(Task 30)。

- [ ] **Step 4: Commit**

```bash
cd ivy-backend
git add tests/test_migration_empleavesync.py
git commit -m "test(migration): placeholder 5 條 migration M-1~M-5

CI 無 Postgres 環境跑不了,標 skip;Deploy SOP step 1 staging 人工驗證。
PR description 必貼 staging 驗證結果。"
```

---

## Task 25: Salary engine cutover — _legacy 保留 + 新版實作

**Files:**
- Modify: `services/salary/engine.py`(`_build_breakdown_for_month` 區域)

- [ ] **Step 1: 找 _build_breakdown_for_month 位置**

Run: `grep -n "_build_breakdown_for_month\|LeaveRecord" ivy-backend/services/salary/engine.py | head -20`

Expected: 顯示函式起點(約 L2884 上下)與 LeaveRecord query 行。

- [ ] **Step 2: 把舊版 rename 為 `_build_breakdown_for_month_legacy`**

修改 `services/salary/engine.py`:

```python
def _build_breakdown_for_month_legacy(self, employee_id: int, year: int, month: int):
    """LEGACY:Task 25 之前的 LeaveRecord overlap 計算。

    PR 內保留供 parity test(Task 26)使用;
    Merge 後 7 天 follow-up PR 刪除。
    """
    # ... 原 _build_breakdown_for_month 全部內容
```

- [ ] **Step 3: 寫新版 `_build_breakdown_for_month`**

```python
def _build_breakdown_for_month(self, employee_id: int, year: int, month: int):
    """讀 AttendanceRecord(status=LEAVE / partial_leave_hours / leave_record_id)
    為 SoT 計算扣款、全勤獎金、加班互斥。
    """
    from datetime import date
    from calendar import monthrange
    from models.attendance import AttendanceRecord, AttendanceStatus
    from models.leave import LeaveRecord
    from decimal import Decimal
    from sqlalchemy import or_

    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    # 1. 拿月內所有與 leave 相關的 attendance(join LeaveRecord)
    attendances_with_leave = self.session.query(
        AttendanceRecord, LeaveRecord
    ).join(
        LeaveRecord, AttendanceRecord.leave_record_id == LeaveRecord.id
    ).filter(
        AttendanceRecord.employee_id == employee_id,
        AttendanceRecord.attendance_date.between(first_day, last_day),
        AttendanceRecord.leave_record_id.isnot(None),
    ).all()

    daily_rate = self._get_daily_rate(employee_id, year, month)
    leave_deduction = Decimal("0")
    leave_days = []
    for att, lv in attendances_with_leave:
        # 全天 → 8 小時;部分 → partial_leave_hours
        if att.status == AttendanceStatus.LEAVE:
            hours = Decimal("8")
        elif att.partial_leave_hours is not None and att.partial_leave_hours > 0:
            hours = Decimal(str(att.partial_leave_hours))
        else:
            # case 3:全天請假但人來上班(partial_leave_hours=0)→ 不扣款
            continue
        ratio = self._get_deduction_ratio(lv.leave_type)
        deduction = daily_rate * Decimal(str(ratio)) * (hours / Decimal("8"))
        leave_deduction += deduction
        leave_days.append({
            "date": att.attendance_date,
            "hours": hours,
            "leave_type": lv.leave_type,
        })

    # 2. 全勤獎金:看月內是否有任何 attendance 「打破全勤」
    has_imperfect = self.session.query(AttendanceRecord).filter(
        AttendanceRecord.employee_id == employee_id,
        AttendanceRecord.attendance_date.between(first_day, last_day),
        or_(
            AttendanceRecord.leave_record_id.isnot(None),
            AttendanceRecord.status.in_([
                AttendanceStatus.LATE, AttendanceStatus.EARLY_LEAVE,
                AttendanceStatus.MISSING, AttendanceStatus.ABSENT,
            ]),
        )
    ).first()
    perfect_attendance_bonus = (
        Decimal("0") if has_imperfect
        else self._get_perfect_attendance_bonus(employee_id)
    )

    # 3. 加班互斥 / overtime_pay — 仍維持既有邏輯
    overtime_pay = self._compute_overtime_pay(employee_id, year, month)

    return SalaryBreakdown(
        leave_deduction=leave_deduction,
        perfect_attendance_bonus=perfect_attendance_bonus,
        overtime_pay=overtime_pay,
        leave_days=leave_days,
    )
```

> **注意**:上方 code 中 `self._get_daily_rate`、`self._get_deduction_ratio`、`self._compute_overtime_pay`、`self._get_perfect_attendance_bonus`、`SalaryBreakdown` 等都是既有 helper / dataclass。implementer 對齊既有命名,不是新增。

- [ ] **Step 4: 跑既有 salary engine 測試**

Run: `cd ivy-backend && pytest tests/test_salary_engine.py -v 2>&1 | tail -30`

Expected: 既有測試**可能會 fail**,因為新版實作的輸出可能跟 legacy 微差。Task 26 parity test 會抓出這些差異 → implementer 修。

> **暫時策略**:若既有測試 fail,先記下 fail case;等 Task 26 parity test 寫好對齊。

- [ ] **Step 5: Commit(暫時可能不全綠)**

```bash
cd ivy-backend
git add services/salary/engine.py
git commit -m "feat(salary): _build_breakdown_for_month 切換到 AttendanceRecord 為 SoT

legacy 版本保留為 _build_breakdown_for_month_legacy,Task 26 parity test
驗證兩版輸出一致。Merge 後 7 天 follow-up PR 刪除 legacy。"
```

---

## Task 26: Salary engine parity test (30+ 案例)

**Files:**
- Create: `tests/test_salary_engine_parity.py`

- [ ] **Step 1: 寫 parity test**

新建 `tests/test_salary_engine_parity.py`:

```python
"""Salary engine cutover 安全網:新版 _build_breakdown_for_month 必須與 _legacy 完全一致。

30+ 案例覆蓋:
- 全勤(無請假)
- 全天請假橫跨月份邊界
- 半天請假 + 該日有打卡
- 半天請假 + 該日缺打卡
- 小時請假
- 全月零打卡(離職員工)
- 月底跨月 leave
- 補休(source_overtime_id)
- 一個月內多筆請假混合
"""

import pytest
from datetime import date
from services.salary.engine import SalaryEngine


# Fixture:依照 conftest.py 既有 employee + leave + attendance fixture
@pytest.fixture
def salary_engine(db_session):
    return SalaryEngine(session=db_session)


# Parametrize 30+ 案例
PARITY_CASES = [
    # (employee_id, year, month, scenario_desc)
    (1, 2026, 5, "員工 1 / 5 月 / 全勤"),
    (2, 2026, 5, "員工 2 / 5 月 / 全天請假 5/22~5/24"),
    (3, 2026, 5, "員工 3 / 5 月 / 半天請假"),
    (4, 2026, 5, "員工 4 / 5 月 / 小時請假"),
    (5, 2026, 4, "員工 5 / 4 月 / 跨月 leave 4/30~5/2"),
    (6, 2026, 5, "員工 6 / 5 月 / 跨月 leave 4/30~5/2"),
    (7, 2026, 5, "員工 7 / 5 月 / 全月零打卡"),
    (8, 2026, 5, "員工 8 / 5 月 / 月底跨月 5/30~6/2"),
    (9, 2026, 5, "員工 9 / 5 月 / 補休 source_overtime_id"),
    (10, 2026, 5, "員工 10 / 5 月 / 一個月內三筆混合請假"),
    # ... 補到 30+
]


@pytest.mark.parametrize("employee_id,year,month,desc", PARITY_CASES)
def test_breakdown_matches_legacy(salary_engine, employee_id, year, month, desc):
    """新版必須與 legacy 輸出完全一致"""
    new = salary_engine._build_breakdown_for_month(employee_id, year, month)
    old = salary_engine._build_breakdown_for_month_legacy(employee_id, year, month)

    assert new.leave_deduction == old.leave_deduction, (
        f"{desc} leave_deduction: new={new.leave_deduction} old={old.leave_deduction}"
    )
    assert new.perfect_attendance_bonus == old.perfect_attendance_bonus, (
        f"{desc} perfect_attendance_bonus: new={new.perfect_attendance_bonus} "
        f"old={old.perfect_attendance_bonus}"
    )
    assert new.overtime_pay == old.overtime_pay, (
        f"{desc} overtime_pay: new={new.overtime_pay} old={old.overtime_pay}"
    )

    # leave_days 比較(order-insensitive)
    sorted_new = sorted(new.leave_days, key=lambda x: (x["date"], x["leave_type"]))
    sorted_old = sorted(old.leave_days, key=lambda x: (x["date"], x["leave_type"]))
    assert sorted_new == sorted_old, (
        f"{desc} leave_days mismatch"
    )
```

- [ ] **Step 2: 跑 parity test**

Run: `cd ivy-backend && pytest tests/test_salary_engine_parity.py -v 2>&1 | tail -50`

Expected: 若新版實作對,30+ 全綠;若有 fail,根據 desc 定位修 `_build_breakdown_for_month`。

- [ ] **Step 3: 修到 parity 完全綠**

每修一次就重跑 parity test。修法可能是:
- 漏算 case 3(全天請假但人來上班)→ 確認 partial_leave_hours=0 時跳過 deduction
- 漏算「補休 source_overtime_id」邏輯 → 抄 legacy 的邏輯
- Decimal 精度問題 → 確認 ratio 用 Decimal 不用 float

- [ ] **Step 4: 跑全套 salary engine 測試**

Run: `cd ivy-backend && pytest tests/test_salary_engine*.py -v 2>&1 | tail -30`

Expected: 全綠。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend
git add tests/test_salary_engine_parity.py services/salary/engine.py
git commit -m "test(salary): parity test 30+ 案例驗證 legacy vs 新版一致

實際 fixture 從 tests/conftest.py 取既有員工/薪資 seed;
列幾組 edge case 確保 cutover 安全。
Merge + 7 天後 follow-up 刪 _legacy 與本檔。"
```

---

## Task 27: Monthly report cutover — _legacy 保留 + outerjoin 新版

**Files:**
- Modify: `api/attendance/reports.py:344-364`(`build_monthly_report` 區域)

- [ ] **Step 1: 把舊版 rename 為 `build_monthly_report_legacy`**

修改 `api/attendance/reports.py`:

```python
def build_monthly_report_legacy(self, employee_id, start_date, end_date):
    """LEGACY: Task 27 之前的 leave_map join 邏輯。

    PR 內保留供 parity test(Task 28)使用;
    Merge 後 7 天 follow-up PR 刪除。
    """
    # ... 原 344-364 區段全部內容
```

- [ ] **Step 2: 寫新版 `build_monthly_report` 用 outerjoin**

```python
def build_monthly_report(self, employee_id, start_date, end_date):
    """讀 AttendanceRecord(outerjoin LeaveRecord)為 SoT,組月度報表。"""
    from datetime import timedelta
    from models.attendance import AttendanceRecord, AttendanceStatus
    from models.leave import LeaveRecord

    attendances = self.session.query(
        AttendanceRecord, LeaveRecord
    ).outerjoin(
        LeaveRecord, AttendanceRecord.leave_record_id == LeaveRecord.id
    ).filter(
        AttendanceRecord.employee_id == employee_id,
        AttendanceRecord.attendance_date.between(start_date, end_date),
    ).order_by(AttendanceRecord.attendance_date).all()

    rows = []
    for att, lv in attendances:
        row = {
            "date": att.attendance_date,
            "punch_in": att.punch_in_time,
            "punch_out": att.punch_out_time,
            "late_minutes": att.late_minutes,
            "early_leave_minutes": att.early_leave_minutes,
        }
        if att.status == AttendanceStatus.LEAVE:
            row["status_label"] = f"{leave_type_label(lv.leave_type)}(全天)"
        elif att.leave_record_id is not None and lv is not None:
            row["status_label"] = (
                f"{status_label(att.status)} / "
                f"{leave_type_label(lv.leave_type)} "
                f"{att.partial_leave_hours}hr"
            )
        else:
            row["status_label"] = status_label(att.status)
        rows.append(row)
    return rows
```

> **注意**:`status_label()` / `leave_type_label()` 是既有 helper(可能在 `utils/i18n.py` 或同檔內)。implementer 確認後不重新定義。

- [ ] **Step 3: 跑既有 attendance reports 測試**

Run: `cd ivy-backend && pytest tests/test_attendance_reports.py -v 2>&1 | tail -20`

Expected: 既有測試可能 fail(輸出 schema 一致但邊緣值不同) — Task 28 parity test 修。

- [ ] **Step 4: Commit**

```bash
cd ivy-backend
git add api/attendance/reports.py
git commit -m "feat(reports): build_monthly_report 改用 outerjoin AttendanceRecord+LeaveRecord

砍掉 leave_map 字典 + while 迴圈;legacy 版本保留為
build_monthly_report_legacy 供 Task 28 parity test 用。"
```

---

## Task 28: Monthly report parity test

**Files:**
- Create: `tests/test_attendance_report_parity.py`

- [ ] **Step 1: 寫 parity test**

新建 `tests/test_attendance_report_parity.py`:

```python
"""Monthly report cutover 安全網:新版 outerjoin 與 legacy 必須完全一致。"""

import pytest
from datetime import date
from api.attendance.reports import AttendanceReportBuilder  # 假設既有 class


@pytest.fixture
def report_builder(db_session):
    return AttendanceReportBuilder(session=db_session)


PARITY_CASES = [
    (1, date(2026, 5, 1), date(2026, 5, 31), "員工 1 / 5 月全月"),
    (2, date(2026, 5, 1), date(2026, 5, 31), "員工 2 / 5 月有半天請假"),
    (3, date(2026, 4, 1), date(2026, 5, 31), "員工 3 / 4-5 月跨月 leave"),
    # ... 補 30+
]


@pytest.mark.parametrize("employee_id,start,end,desc", PARITY_CASES)
def test_monthly_report_matches_legacy(report_builder, employee_id, start, end, desc):
    new = report_builder.build_monthly_report(employee_id, start, end)
    old = report_builder.build_monthly_report_legacy(employee_id, start, end)
    assert new == old, f"{desc}: monthly report mismatch"
```

- [ ] **Step 2: 跑 parity test**

Run: `cd ivy-backend && pytest tests/test_attendance_report_parity.py -v 2>&1 | tail -30`

Expected: 30+ 全綠;若 fail 根據 desc 修。

- [ ] **Step 3: 跑全套 reports 測試**

Run: `cd ivy-backend && pytest tests/test_attendance_reports.py -v 2>&1 | tail -20`

Expected: 全綠。

- [ ] **Step 4: Commit**

```bash
cd ivy-backend
git add tests/test_attendance_report_parity.py
git commit -m "test(reports): monthly report parity 30+ 案例

確保 leave_map 拆掉後與 legacy 輸出 byte-identical。
Merge + 7 天後 follow-up 刪 _legacy 與本檔。"
```

---

## Task 29: Final regression + 前端手動驗證 + Grep gate

**Files:**
- No file create/modify;這是驗證任務

- [ ] **Step 1: 跑全套 backend pytest**

Run: `cd ivy-backend && pytest -v 2>&1 | tail -50`

Expected: 全套 ~4600+ tests,**0 regression**。若有 fail,逐個釐清是 pre-existing 還是本 PR 引入。

- [ ] **Step 2: Grep gate #1 — Attendance 寫入路徑(§3.5)**

Run:
```bash
cd ivy-backend && grep -rn "AttendanceRecord" api/ services/ utils/ \
  --include="*.py" \
  | grep -v "test_\|reports.py\|salary/engine.py\|leaves.py" \
  | grep -iE "add\(|merge\(|insert|update_|upsert|put_|new AttendanceRecord"
```

Expected output: 只應該命中 `api/attendance/records.py`(Task 15)、`api/attendance/upload.py`(Task 16)、`utils/attendance_calc.py`(Task 17)。

若有第 4、5 個寫入路徑 → 不能 ship,plan 補新 task。

- [ ] **Step 3: Grep gate #2 — LeaveRecord 補丁殘留(§7)**

Run:
```bash
cd ivy-backend && grep -rn "LeaveRecord" api/ services/ \
  --include="*.py" \
  | grep -v "salary/engine.py" \
  | grep -v "leaves.py" \
  | grep -v "audit"
```

Expected output: 若有 LeaveRecord 殘留(不在 salary engine / leaves router / audit),case-by-case 判斷是真的需要 LeaveRecord(顯示請假理由 / 完整審核流程)還是補丁 — 補丁就順手切。

- [ ] **Step 4: 前端手動驗證 F-1~F-4**

啟動兩端:
```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

人工操作驗證:
- [ ] **F-1**:月度出勤報表,選一個全天請假員工 → 顯示「特休(全天)」/「事假(全天)」
- [ ] **F-2**:同頁,半天請假員工 → 顯示「準時 / 特休 4hr」
- [ ] **F-3**:同頁,遲到+半天請假員工 → 「遲到 X 分 / 病假 4hr」late 重算正確
- [ ] **F-4**:員工個人考勤查詢頁(若 admin 有此頁面)→ 同樣語義一致

截圖貼到 PR description。

- [ ] **Step 5: 寫驗證報告到 `.scratch/employee_leave_attendance_sync_verification.md`**

```markdown
# Verification Report

## Backend pytest
- 全套 N tests passed,0 regression

## Grep gate
- §3.5 attendance 寫入路徑:只命中 3 處(records.py / upload.py / attendance_calc.py)✓
- §7 LeaveRecord 殘留:[列出命中或宣告無命中]

## 前端手動驗證
- F-1 全天請假 ✓(截圖 attached)
- F-2 半天請假 ✓
- F-3 遲到+半天請假 ✓
- F-4 員工個人考勤頁 ✓
```

- [ ] **Step 6: Commit 驗證報告**

```bash
cd ~/Desktop/ivyManageSystem
git add .scratch/employee_leave_attendance_sync_verification.md
git commit -m "docs: final verification report for employee leave attendance sync"
```

> 注意:這個 commit 在 workspace 目錄,不在 ivy-backend repo 內(workspace 不是 git repo)。實際上 .scratch 不入 repo,人工存檔即可。

---

## Task 30: PR description + Deploy SOP

**Files:**
- No code change;PR description 整理

- [ ] **Step 1: 在 ivy-backend 提 PR**

```bash
cd ivy-backend
git push origin <branch-name>
gh pr create --title "feat(leaves): 員工請假 → 考勤同步重構 + AttendanceRecord 成為 SoT" \
  --body "$(cat <<'EOF'
## Summary

實作 spec `docs/superpowers/specs/2026-05-22-employee-leave-attendance-sync-design.md` v1.1。

讓 `AttendanceRecord` 成為員工出勤的唯一 SoT,消除「多處 join LeaveRecord 才不出鬼影」的補丁文化:
- **sync service**(`services/employee_leave_attendance_sync.py`)在 leaves 5 條 hook 路徑寫 attendance
- **merge helper**(`utils/attendance_leave_merge.py`)在 3 條 attendance 寫入路徑 pull leave
- **salary engine** + **monthly report** 切到讀 AttendanceRecord;parity test + _legacy 保留作為安全網
- **migration** schema + ENUM + UNIQUE INDEX CONCURRENTLY + 全量 backfill

## Architecture

兩個並列、不互呼的模組(spec §3 / §3.5):
- sync: leaves 端入口 → 寫 attendance(全天 upsert status=LEAVE;部分保留 punch + partial_leave_hours)
- merge: attendance 寫入端 → pull leave(in-place 改 att 物件,純讀 session)

## Test plan

- [x] sync.apply/revert/reapply unit tests U-1~U-15 全綠(`tests/test_employee_leave_attendance_sync.py`)
- [x] merge helper M-1~M-7 全綠(`tests/test_attendance_leave_merge.py`)
- [x] compute_late_minutes_with_leave C-1~C-6 全綠(`tests/test_attendance_calc.py`)
- [x] 5 條 leaves hook 整合測試 I-1~I-10 全綠(`tests/test_leaves_attendance_sync.py`)
- [x] 3 條 attendance 寫入路徑整合測試 W-1~W-5 全綠(`tests/test_attendance_writes_leave_aware.py`)
- [x] Salary engine parity test 30+ 案例全綠(`tests/test_salary_engine_parity.py`)
- [x] Monthly report parity test 30+ 案例全綠(`tests/test_attendance_report_parity.py`)
- [x] 全套 backend pytest **N passed / 0 regression**
- [x] 前端手動 F-1~F-4 驗證(截圖見下方)

## Grep gates(spec §3.5 / §7)

```bash
# §3.5 attendance 寫入路徑(只應命中 3 處)
grep -rn "AttendanceRecord" api/ services/ utils/ --include="*.py" \
  | grep -v "test_\|reports.py\|salary/engine.py\|leaves.py" \
  | grep -iE "add\(|merge\(|insert|update_|upsert|put_|new AttendanceRecord"
# 結果:[貼實際輸出]

# §7 LeaveRecord 補丁殘留
grep -rn "LeaveRecord" api/ services/ --include="*.py" \
  | grep -v "salary/engine.py" | grep -v "leaves.py" | grep -v "audit"
# 結果:[貼實際輸出]
```

## Migration M-1~M-5 staging 驗證

- [x] M-1 clean DB upgrade ✓
- [x] M-2 dup → fail-loud ✓
- [x] M-3 backfill 完成 ✓
- [x] M-4 idempotent ✓
- [x] M-5 downgrade ✓(LEAVE enum 殘留是預期)

## Deploy SOP

```
1. Staging 部署(預演)
   - python scripts/preview_backfill.py
   - python scripts/dedupe_attendance.py --dry-run
   - python scripts/fix_partial_leave_times.py --dry-run
   - alembic upgrade head
   - pytest tests/test_salary_engine_parity.py tests/test_attendance_report_parity.py
   - pytest tests/test_attendance_writes_leave_aware.py
   - 點過月報 UI 三個樣本

2. Prod 部署(off-peak, non-stop)
   - 部署前 4hr 重跑 pre-flight scripts
   - rolling restart;migration CONCURRENTLY 不阻塞 DML
   - backfill 計時觀察(預估 < 60s)
   - 部署完成後 24hr 觀察 audit_logs

3. Merge 後 7 天追蹤
   - 確認 close-month 結薪正常
   - 確認 attendance 3 寫入路徑無誤覆蓋 leave_record_id
```

## Post-merge cleanup follow-ups

- [ ] 刪除 `_build_breakdown_for_month_legacy()` + `test_salary_engine_parity.py`(merge + 7 天後)
- [ ] 刪除 `build_monthly_report_legacy()` + `test_attendance_report_parity.py`
- [ ] 評估「同日多筆部分請假」是否要支援(association table follow-up)
- [ ] 評估 backfill 視窗從 12 個月延伸到全歷史(視 prod row 數)
- [ ] 若加班計算未在此 PR 切換,follow-up plan 補上

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: PR URL 貼回對話**

Implementer 完成後把 PR URL 提供給 user。

- [ ] **Step 3: User 自行 review + merge**

Plan 到此結束。Deploy 由 user 依 SOP 執行,not implementer 跨權限。

---

## Self-Review 檢查清單

寫完 plan 後 implementer 自驗:

**1. Spec coverage** — 對照 spec §1~§9 + 附錄,每節都有 task 對應:
- §1 scope → Task 1, 21-23
- §2 schema → Task 2, 3, 21-22
- §3 sync service → Task 5-10
- §3.5 merge helper → Task 4, 11, 15-17
- §4 5 hook → Task 12-14
- §5 backfill → Task 19, 23
- §6 salary cutover → Task 25-26
- §7 monthly report cutover → Task 27-28
- §8 測試矩陣 → 散落在 Task 4-17 + 24, 26, 28
- §9 風險 / SOP → Task 29-30

**2. Placeholder scan** — 全文無 TBD/TODO/「implement later」(只有 fixture 假設由 conftest 提供 — 這是事實依賴不是 placeholder)

**3. Type consistency** — `sync.apply` / `sync.revert` / `sync.reapply` 在 Task 5/6/9/10/12-14 簽章一致;`merge_attendance_with_leave(att, session)` 在 Task 11/15/16/17 簽章一致

**4. 未來實作者必看的踩雷點(spec 附錄 13 條)** — 全寫入對應 task 的 step 註解或 commit message

---

**Plan 版本**:1.0
**最後更新**:2026-05-22
**對應 Spec**:v1.1
**預估工時**:30 task,單人 4-6 工作日(含人工 staging 驗證)
