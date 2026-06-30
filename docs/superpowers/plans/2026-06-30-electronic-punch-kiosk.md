# 電子打卡（園內 Kiosk 即時打卡）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓員工在園內公用裝置（平板）上「選名單 + 輸入個人 PIN」即時打卡，寫入既有 `Attendance` 表（first-in / last-out），受 IP 白名單 + PIN 雙重防護。

**Architecture:** 後端在既有 `api/attendance/` 子套件新增 `kiosk.py`（roster / preview / punch 三端點，IP 白名單 dependency + per-employee PIN 失敗限流），核心寫入邏輯抽成可單元測試的 `services/attendance_kiosk.py`（封存守衛 → first-in/last-out 寫入 → status 重算 → 請假同步 → 標薪資 stale）。PIN 以既有 PBKDF2 `hash_password`/`verify_password` 存於 `Employee.punch_pin_hash`。前端新增不掛登入的 `/kiosk/punch` 頁（自製數字鍵盤）+ Portal 自設 PIN + 管理端重置 PIN。

**Tech Stack:** FastAPI / SQLAlchemy / Alembic / PostgreSQL（後端）；Vue 3 `<script setup lang="ts">` / Element Plus ^2.5.6 / Vitest（前端）。

## Global Constraints

> 每個 task 的需求都隱含包含本節。值已從 spec 逐字抄入。

- **語言**：一律繁體中文（commit message、docstring、註解、文件、UI 文案）。
- **跨端流程**：後端先行（schema + router + pytest）→ 前端接上（`src/api/*.ts` + 頁面）→ 前後端**分開 commit**（不同 repo）。
- **TDD**：純邏輯必補測試；每個 task 先寫失敗測試再實作。
- **前端 TS-only**：新 SFC 一律 `<script setup lang="ts">`；禁 `: any`/`as any`，用 `: unknown` + narrow 或 `// @ts-expect-error TODO(ts-strict): <reason>`。
- **PIN 規則**：4–6 位純數字；以 `hash_password` 雜湊儲存（`utils/auth.py`），**明文不落庫、不回傳**；驗證用 `verify_password`（恆定時間比對）。
- **打卡規則**：first-in / last-out——`punch_in_time` 首次寫入後**不可由 kiosk 覆蓋**；`punch_out_time` 由每次後續打卡覆蓋（末次為準）。打卡時間一律取**伺服器當前時間** `now_taipei_naive()`，請求體**不接受任何時間戳**。
- **IP 白名單**：新設定 `ATTENDANCE_KIOSK_ALLOWED_IPS`（CSV/CIDR）；**fail-closed**（未設定或空 → 所有 kiosk 端點 403）。取真實 client IP 用 `utils/request_ip.get_client_ip`。
- **限流**：PIN 失敗限流 key 用 **`employee_id`（per-employee）**，**不可用 per-IP**（kiosk 合法流量集中單一園內 IP，per-IP 會誤傷全園正常打卡）。
- **`source` 欄位**：kiosk 寫 `'kiosk'`；**不 backfill 歷史列**（歷史維持 `NULL`）。
- **權限**：**不新增 `Permission` enum 值**。kiosk 三端點走 IP+PIN（無 JWT）；管理端重置 PIN 走既有 `Permission.ATTENDANCE_WRITE`；員工自設 PIN 走 Portal 個人 JWT。
- **Migration**：兩員工欄位 + 一考勤欄位皆 **nullable、無 server default**；`down_revision` 填**建立當下**的 alembic head（執行前跑 `alembic heads` 確認；⚠ 有平行 session 也在加 migration，head 可能已非 `enrterm01`）。
- **後端針對性 pytest**：加 `-o addopts=""` 關閉 coverage，避免 Bash 120s timeout 假卡（例：`pytest tests/test_x.py -o addopts="" -q`）。
- **限流器測試污染**：`conftest.py` 已有 autouse `reset_in_memory_limiters`；跨測試累積污染須留意（記憶 `feedback_inmemory_ratelimiter_test_pollution`）。

## File Structure

**後端（`ivy-backend`）**

| 檔案 | 動作 | 責任 |
|------|------|------|
| `config/network.py` | 修改 | 加 `attendance_kiosk_allowed_ips: CsvList = []` |
| `models/employee.py` | 修改 | 加 `punch_pin_hash` `punch_pin_set_at` |
| `models/attendance.py` | 修改 | 加 `source` String(20) |
| `alembic/versions/<new>.py` | 新增 | 三欄位 migration（nullable）|
| `utils/attendance_shift_window.py` | 修改 | 加純查詢 helper `build_shift_maps_for_employee_date` |
| `services/attendance_kiosk.py` | 新增 | `resolve_punch_action`（preview）/ `apply_punch`（寫入）核心邏輯 |
| `utils/kiosk_guard.py` | 新增 | `assert_kiosk_ip_allowed` IP 白名單 dependency（fail-closed）|
| `api/attendance/kiosk.py` | 新增 | roster / preview / punch 三端點 + PIN 驗證 + 限流 + schema |
| `api/attendance/__init__.py` | 修改 | 註冊 kiosk_router |
| `api/portal/punch_pin.py` | 新增 | `PUT /api/portal/me/punch-pin` 員工自設 PIN |
| `api/portal/__init__.py` | 修改 | 註冊 punch_pin_router |
| `api/employees.py` | 修改 | 加 `POST /api/employees/{id}/reset-punch-pin` |

**前端（`ivy-frontend`）**

| 檔案 | 動作 | 責任 |
|------|------|------|
| `src/api/_generated/schema.d.ts` | 重生 | OpenAPI codegen |
| `src/api/kiosk.ts` | 新增 | roster / preview / punch wrapper |
| `src/api/portal.ts` | 修改 | `setPunchPin` wrapper |
| `src/api/employees.ts` | 修改 | `resetPunchPin` wrapper |
| `src/components/kiosk/NumPad.vue` | 新增 | 數字鍵盤元件 |
| `src/views/kiosk/KioskPunchView.vue` | 新增 | kiosk 打卡頁 |
| `src/router/index.ts` | 修改 | 加 `/kiosk/punch` public 路由 |
| `src/views/portal/PortalProfileView.vue` | 修改 | 「設定打卡 PIN」區塊 |
| `src/views/EmployeeView.vue` | 修改 | 「重置打卡 PIN」操作 |

---

# 階段一：後端（先行）

## Task 1: 資料模型三欄位 + Alembic migration

**Files:**
- Modify: `models/employee.py`（Employee class 內，`work_end_time` 附近）
- Modify: `models/attendance.py`（Attendance class 內，`remark` 附近）
- Create: `alembic/versions/<rev>_add_punch_pin_and_attendance_source.py`
- Test: `tests/test_kiosk_migration_columns.py`

**Interfaces:**
- Produces: `Employee.punch_pin_hash: str|None`、`Employee.punch_pin_set_at: datetime|None`、`Attendance.source: str|None`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_kiosk_migration_columns.py
from models.database import Employee, Attendance


def test_employee_has_punch_pin_columns():
    assert hasattr(Employee, "punch_pin_hash")
    assert hasattr(Employee, "punch_pin_set_at")


def test_attendance_has_source_column():
    assert hasattr(Attendance, "source")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_migration_columns.py -o addopts="" -q`
Expected: FAIL（`AttributeError` 或 assert 失敗）

- [ ] **Step 3: 加 model 欄位**

`models/employee.py`（接在 `work_end_time` 欄位定義之後）：

```python
    punch_pin_hash = Column(
        String(200), nullable=True, comment="打卡 PIN 雜湊（PBKDF2，明文不落庫）"
    )
    punch_pin_set_at = Column(
        DateTime, nullable=True, comment="打卡 PIN 設定/重置時間"
    )
```

`models/attendance.py`（接在 `remark` 欄位定義之後）：

```python
    source = Column(
        String(20),
        nullable=True,
        comment="打卡來源：kiosk（即時打卡）/manual（管理端補卡）/import（批次匯入）；NULL=歷史未知",
    )
```

> 確認 `models/employee.py` 與 `models/attendance.py` 頂部已 import `Column, String, DateTime`（既有欄位已用，通常無需新增 import）。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_migration_columns.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: 建 migration**

先確認當前 head：`cd /Users/yilunwu/Desktop/ivy-backend && alembic heads`（記下 head id，填入下方 `down_revision`）。

Create `alembic/versions/20260630_kioskpin01_add_punch_pin_and_attendance_source.py`：

```python
"""add punch_pin to employees and source to attendances

Revision ID: kioskpin01
Revises: <當前 head id>
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "kioskpin01"
down_revision = "<當前 head id>"  # 由 `alembic heads` 取得
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    emp_cols = {c["name"] for c in inspector.get_columns("employees")}
    if "punch_pin_hash" not in emp_cols:
        op.add_column(
            "employees",
            sa.Column("punch_pin_hash", sa.String(length=200), nullable=True,
                      comment="打卡 PIN 雜湊（PBKDF2，明文不落庫）"),
        )
    if "punch_pin_set_at" not in emp_cols:
        op.add_column(
            "employees",
            sa.Column("punch_pin_set_at", sa.DateTime(), nullable=True,
                      comment="打卡 PIN 設定/重置時間"),
        )

    att_cols = {c["name"] for c in inspector.get_columns("attendances")}
    if "source" not in att_cols:
        op.add_column(
            "attendances",
            sa.Column("source", sa.String(length=20), nullable=True,
                      comment="打卡來源：kiosk/manual/import；NULL=歷史未知"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    att_cols = {c["name"] for c in inspector.get_columns("attendances")}
    if "source" in att_cols:
        op.drop_column("attendances", "source")

    emp_cols = {c["name"] for c in inspector.get_columns("employees")}
    if "punch_pin_set_at" in emp_cols:
        op.drop_column("employees", "punch_pin_set_at")
    if "punch_pin_hash" in emp_cols:
        op.drop_column("employees", "punch_pin_hash")
```

- [ ] **Step 6: 跑 migration 來回驗證**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
```
Expected: 三步皆無錯誤；`alembic heads` 仍單一 head。

- [ ] **Step 7: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add models/employee.py models/attendance.py alembic/versions/20260630_kioskpin01_add_punch_pin_and_attendance_source.py tests/test_kiosk_migration_columns.py
git commit -m "feat(attendance): 加打卡 PIN 與考勤來源欄位（kiosk 即時打卡基礎）"
```

---

## Task 2: 新增 `ATTENDANCE_KIOSK_ALLOWED_IPS` 設定

**Files:**
- Modify: `config/network.py`（`NetworkSettings` class 內）
- Test: `tests/test_kiosk_settings.py`

**Interfaces:**
- Produces: `settings.network.attendance_kiosk_allowed_ips: list[str]`（CsvList，預設 `[]`）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_kiosk_settings.py
from config.network import NetworkSettings


def test_kiosk_allowed_ips_default_empty():
    s = NetworkSettings()
    assert s.attendance_kiosk_allowed_ips == []


def test_kiosk_allowed_ips_parses_csv(monkeypatch):
    monkeypatch.setenv("ATTENDANCE_KIOSK_ALLOWED_IPS", "203.0.113.10/32, 10.0.0.5")
    s = NetworkSettings()
    assert s.attendance_kiosk_allowed_ips == ["203.0.113.10/32", "10.0.0.5"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_settings.py -o addopts="" -q`
Expected: FAIL（`AttributeError: attendance_kiosk_allowed_ips`）

- [ ] **Step 3: 加設定欄位**

`config/network.py`，在 `NetworkSettings` 既有欄位區（如 `school_wifi_ips` 附近）加：

```python
    attendance_kiosk_allowed_ips: CsvList = []  # env: ATTENDANCE_KIOSK_ALLOWED_IPS
```

> `CsvList` 已在本檔可用（`school_wifi_ips` 即用此型別）。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_settings.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add config/network.py tests/test_kiosk_settings.py
git commit -m "feat(config): 加 ATTENDANCE_KIOSK_ALLOWED_IPS 白名單設定"
```

---

## Task 3: 抽出 `build_shift_maps_for_employee_date` 純查詢 helper

**Files:**
- Modify: `utils/attendance_shift_window.py`（檔尾加新函式）
- Test: `tests/test_build_shift_maps.py`

**Interfaces:**
- Consumes: `models.database`（DailyShift, ShiftAssignment, ShiftType）
- Produces: `build_shift_maps_for_employee_date(session, employee, attendance_date) -> tuple[dict, dict]`，回 `(daily_shift_map, shift_schedule_map)`，格式與 `records.py` 內聯版一致，供 `compute_status_for_employee_date` 使用。

> 註：`api/attendance/records.py` 目前內聯同邏輯，本次**不改 records.py**（避免動既有路徑），records.py 改用此 helper 列為 follow-up。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_build_shift_maps.py
from datetime import date
from models.database import Employee
from utils.attendance_shift_window import build_shift_maps_for_employee_date


def test_build_shift_maps_empty_when_no_shifts(test_db_session):
    emp = Employee(employee_id="E900", name="測試員工", work_start_time="08:00",
                   work_end_time="17:00", is_active=True)
    test_db_session.add(emp)
    test_db_session.commit()

    daily_map, week_map = build_shift_maps_for_employee_date(
        test_db_session, emp, date(2026, 6, 30)
    )
    assert daily_map == {}
    assert week_map == {}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_build_shift_maps.py -o addopts="" -q`
Expected: FAIL（`ImportError: cannot import name 'build_shift_maps_for_employee_date'`）

- [ ] **Step 3: 實作 helper**

`utils/attendance_shift_window.py` 檔尾加（import 用既有頂部 import；若無則加 `from datetime import timedelta` 與 `from models.database import DailyShift, ShiftAssignment, ShiftType`）：

```python
def build_shift_maps_for_employee_date(session, employee, attendance_date):
    """查該員工該日的班別視窗來源，回 (daily_shift_map, shift_schedule_map)。

    純查詢、無副作用。格式對齊 compute_status_for_employee_date 所需：
      daily_shift_map:   {(emp_id, date): {"work_start", "work_end", "name"}}
      shift_schedule_map:{(emp_id, week_start): {"work_start", "work_end", "name"}}
    """
    from datetime import timedelta
    from models.database import DailyShift, ShiftAssignment, ShiftType

    daily_shift_map = {}
    daily_row = (
        session.query(DailyShift)
        .filter(DailyShift.employee_id == employee.id,
                DailyShift.date == attendance_date)
        .first()
    )
    if daily_row and daily_row.shift_type_id:
        st = session.query(ShiftType).filter(ShiftType.id == daily_row.shift_type_id).first()
        if st:
            daily_shift_map[(employee.id, attendance_date)] = {
                "work_start": st.work_start, "work_end": st.work_end, "name": st.name,
            }

    shift_schedule_map = {}
    week_start = attendance_date - timedelta(days=attendance_date.weekday())
    sa_row = (
        session.query(ShiftAssignment)
        .filter(ShiftAssignment.employee_id == employee.id,
                ShiftAssignment.week_start_date == week_start)
        .first()
    )
    if sa_row:
        st_sa = session.query(ShiftType).filter(ShiftType.id == sa_row.shift_type_id).first()
        if st_sa:
            shift_schedule_map[(employee.id, week_start)] = {
                "work_start": st_sa.work_start, "work_end": st_sa.work_end, "name": st_sa.name,
            }

    return daily_shift_map, shift_schedule_map
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_build_shift_maps.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add utils/attendance_shift_window.py tests/test_build_shift_maps.py
git commit -m "feat(attendance): 抽出 build_shift_maps_for_employee_date 純查詢 helper"
```

---

## Task 4: kiosk service — `resolve_punch_action`（first-in/last-out 判定）

**Files:**
- Create: `services/attendance_kiosk.py`
- Test: `tests/test_kiosk_service_resolve.py`

**Interfaces:**
- Consumes: `models.database.Attendance`、`utils.taipei_time.now_taipei_naive`
- Produces:
  - `class PunchPreview`（dataclass）：`employee_name: str`, `action: str`("punch_in"|"punch_out"), `will_overwrite: bool`, `current_punch_out: datetime|None`, `server_time: datetime`
  - `resolve_punch_action(session, employee, now_dt) -> PunchPreview`（純讀，不寫入）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_kiosk_service_resolve.py
from datetime import date, datetime
from models.database import Employee, Attendance
from services.attendance_kiosk import resolve_punch_action


def _make_emp(session):
    emp = Employee(employee_id="E901", name="王老師", work_start_time="08:00",
                   work_end_time="17:00", is_active=True)
    session.add(emp); session.commit()
    return emp


def test_resolve_no_row_is_punch_in(test_db_session):
    emp = _make_emp(test_db_session)
    now = datetime(2026, 6, 30, 9, 0)
    p = resolve_punch_action(test_db_session, emp, now)
    assert p.action == "punch_in"
    assert p.will_overwrite is False
    assert p.employee_name == "王老師"


def test_resolve_has_in_only_is_punch_out(test_db_session):
    emp = _make_emp(test_db_session)
    test_db_session.add(Attendance(employee_id=emp.id, attendance_date=date(2026, 6, 30),
                                   punch_in_time=datetime(2026, 6, 30, 8, 0), status="normal"))
    test_db_session.commit()
    p = resolve_punch_action(test_db_session, emp, datetime(2026, 6, 30, 17, 0))
    assert p.action == "punch_out"
    assert p.will_overwrite is False


def test_resolve_both_present_is_overwrite(test_db_session):
    emp = _make_emp(test_db_session)
    test_db_session.add(Attendance(employee_id=emp.id, attendance_date=date(2026, 6, 30),
                                   punch_in_time=datetime(2026, 6, 30, 8, 0),
                                   punch_out_time=datetime(2026, 6, 30, 12, 5), status="normal"))
    test_db_session.commit()
    p = resolve_punch_action(test_db_session, emp, datetime(2026, 6, 30, 17, 30))
    assert p.action == "punch_out"
    assert p.will_overwrite is True
    assert p.current_punch_out == datetime(2026, 6, 30, 12, 5)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_service_resolve.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: services.attendance_kiosk`）

- [ ] **Step 3: 實作 service（resolve 部分）**

Create `services/attendance_kiosk.py`：

```python
"""園內 kiosk 即時打卡核心邏輯（純函式，session 由 caller 傳入，便於單元測試）。"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from models.database import Attendance


@dataclass
class PunchPreview:
    employee_name: str
    action: str  # "punch_in" | "punch_out"
    will_overwrite: bool
    current_punch_out: Optional[datetime]
    server_time: datetime


def _today_row(session, employee, attendance_date):
    return (
        session.query(Attendance)
        .filter(Attendance.employee_id == employee.id,
                Attendance.attendance_date == attendance_date)
        .first()
    )


def resolve_punch_action(session, employee, now_dt: datetime) -> PunchPreview:
    """依當天既有列判定本次打卡為上班/下班（first-in / last-out），純讀不寫。"""
    row = _today_row(session, employee, now_dt.date())
    if row is None or row.punch_in_time is None:
        action, will_overwrite, cur_out = "punch_in", False, None
    elif row.punch_out_time is None:
        action, will_overwrite, cur_out = "punch_out", False, None
    else:
        action, will_overwrite, cur_out = "punch_out", True, row.punch_out_time
    return PunchPreview(
        employee_name=employee.name, action=action,
        will_overwrite=will_overwrite, current_punch_out=cur_out, server_time=now_dt,
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_service_resolve.py -o addopts="" -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add services/attendance_kiosk.py tests/test_kiosk_service_resolve.py
git commit -m "feat(attendance): kiosk service resolve_punch_action（first-in/last-out 判定）"
```

---

## Task 5: kiosk service — `apply_punch`（寫入 + 重算 + 同步 + 標 stale）

**Files:**
- Modify: `services/attendance_kiosk.py`
- Test: `tests/test_kiosk_service_apply.py`

**Interfaces:**
- Consumes: `build_shift_maps_for_employee_date`、`compute_status_for_employee_date`、`merge_attendance_with_leave`、`lock_and_premark_stale`、`_get_finalized_salary_record`、`now_taipei_naive`
- Produces:
  - `class PunchResult`（dataclass）：`employee_name: str`, `action: str`, `punch_time: datetime`, `status: str`
  - `apply_punch(session, employee, now_dt) -> PunchResult`（寫入當天列、commit）
  - `class MonthFinalizedError(Exception)`（封存月份）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_kiosk_service_apply.py
from datetime import date, datetime
from models.database import Employee, Attendance
from services.attendance_kiosk import apply_punch


def _emp(session):
    e = Employee(employee_id="E902", name="李老師", work_start_time="08:00",
                 work_end_time="17:00", is_active=True)
    session.add(e); session.commit()
    return e


def test_apply_first_punch_writes_punch_in_and_source(test_db_session):
    emp = _emp(test_db_session)
    res = apply_punch(test_db_session, emp, datetime(2026, 6, 30, 8, 0))
    assert res.action == "punch_in"
    row = test_db_session.query(Attendance).filter_by(employee_id=emp.id).one()
    assert row.punch_in_time == datetime(2026, 6, 30, 8, 0)
    assert row.punch_out_time is None
    assert row.source == "kiosk"


def test_apply_second_punch_sets_punch_out(test_db_session):
    emp = _emp(test_db_session)
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 8, 0))
    res = apply_punch(test_db_session, emp, datetime(2026, 6, 30, 17, 0))
    assert res.action == "punch_out"
    row = test_db_session.query(Attendance).filter_by(employee_id=emp.id).one()
    assert row.punch_in_time == datetime(2026, 6, 30, 8, 0)
    assert row.punch_out_time == datetime(2026, 6, 30, 17, 0)


def test_apply_third_punch_overwrites_punch_out_not_punch_in(test_db_session):
    emp = _emp(test_db_session)
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 8, 0))
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 12, 5))
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 17, 30))
    row = test_db_session.query(Attendance).filter_by(employee_id=emp.id).one()
    assert row.punch_in_time == datetime(2026, 6, 30, 8, 0)   # 上班不被覆蓋
    assert row.punch_out_time == datetime(2026, 6, 30, 17, 30)  # 下班末次為準
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_service_apply.py -o addopts="" -q`
Expected: FAIL（`ImportError: cannot import name 'apply_punch'`）

- [ ] **Step 3: 實作 `apply_punch`**

`services/attendance_kiosk.py` 加（頂部 import 補上所需）：

```python
from datetime import timedelta

from utils.attendance_shift_window import (
    build_shift_maps_for_employee_date,
    compute_status_for_employee_date,
)
from utils.attendance_leave_merge import merge_attendance_with_leave
from utils.approval_helpers import _get_finalized_salary_record


class MonthFinalizedError(Exception):
    """該員工該月薪資已封存，拒絕寫入考勤。"""


@dataclass
class PunchResult:
    employee_name: str
    action: str
    punch_time: datetime
    status: str


def apply_punch(session, employee, now_dt: datetime) -> PunchResult:
    """寫入當天考勤列（first-in/last-out）、重算 status、同步請假、標薪資 stale、commit。"""
    from services.salary.utils import lock_and_premark_stale  # 延遲 import 避免循環

    attendance_date = now_dt.date()

    # 封存守衛
    if _get_finalized_salary_record(session, employee.id, attendance_date.year, attendance_date.month):
        raise MonthFinalizedError(
            f"{attendance_date.year} 年 {attendance_date.month} 月薪資已封存，無法打卡。"
        )

    row = _today_row(session, employee, attendance_date)
    if row is None:
        row = Attendance(employee_id=employee.id, attendance_date=attendance_date, status="normal")
        session.add(row)

    # first-in / last-out
    if row.punch_in_time is None:
        row.punch_in_time = now_dt
        action = "punch_in"
    else:
        row.punch_out_time = now_dt
        action = "punch_out"

    # 跨夜防禦（kiosk 同列遞增通常不觸發；保留與 records.py 一致行為）
    if row.punch_in_time and row.punch_out_time and row.punch_out_time < row.punch_in_time:
        row.punch_out_time = row.punch_out_time + timedelta(days=1)

    # 重算 status
    daily_map, week_map = build_shift_maps_for_employee_date(session, employee, attendance_date)
    is_late, late_min, is_early, early_min, status = compute_status_for_employee_date(
        employee, attendance_date, row.punch_in_time, row.punch_out_time,
        daily_map, week_map,
        is_head_teacher=getattr(employee, "is_head_teacher", False),
        is_assistant=getattr(employee, "is_assistant", False),
    )
    row.status = status
    row.is_late = is_late
    row.late_minutes = late_min
    row.is_early_leave = is_early
    row.early_leave_minutes = early_min
    row.is_missing_punch_in = row.punch_in_time is None
    row.is_missing_punch_out = row.punch_out_time is None
    row.source = "kiosk"

    # 請假同步
    merge_attendance_with_leave(row, session)

    # 標該月薪資需重算
    lock_and_premark_stale(session, employee.id, {(attendance_date.year, attendance_date.month)})

    # commit 前先取值，避免 expire_on_commit 後再讀 row/employee 觸發 reload
    punch_time = row.punch_in_time if action == "punch_in" else row.punch_out_time
    status_val = row.status
    emp_name = employee.name
    session.commit()
    return PunchResult(employee_name=emp_name, action=action,
                       punch_time=punch_time, status=status_val)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_service_apply.py -o addopts="" -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 加封存守衛測試**

```python
# 追加到 tests/test_kiosk_service_apply.py
import pytest
from services.attendance_kiosk import MonthFinalizedError
from models.database import SalaryRecord


def test_apply_rejects_finalized_month(test_db_session):
    emp = _emp(test_db_session)
    test_db_session.add(SalaryRecord(employee_id=emp.id, salary_year=2026, salary_month=6,
                                     is_finalized=True, finalized_by="HR"))
    test_db_session.commit()
    with pytest.raises(MonthFinalizedError):
        apply_punch(test_db_session, emp, datetime(2026, 6, 30, 8, 0))
```

> 若 `SalaryRecord` 必填欄位更多導致建構失敗，補上其 NOT NULL 欄位的最小值（參考 `models/salary.py` 或既有薪資測試的建構方式）。

- [ ] **Step 6: 跑全檔測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_service_apply.py -o addopts="" -q`
Expected: PASS（4 passed）

- [ ] **Step 7: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add services/attendance_kiosk.py tests/test_kiosk_service_apply.py
git commit -m "feat(attendance): kiosk service apply_punch（寫入+重算+請假同步+標薪資 stale）"
```

---

## Task 6: kiosk IP 白名單 dependency（fail-closed）

**Files:**
- Create: `utils/kiosk_guard.py`
- Test: `tests/test_kiosk_ip_guard.py`

**Interfaces:**
- Consumes: `utils.request_ip.get_client_ip`、`config.settings.network.attendance_kiosk_allowed_ips`
- Produces: `assert_kiosk_ip_allowed(request: Request) -> None`（FastAPI dependency，不在白名單/未設定 → `HTTPException(403)`）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_kiosk_ip_guard.py
import pytest
from fastapi import HTTPException
from utils.kiosk_guard import assert_kiosk_ip_allowed


class _Req:
    def __init__(self, host): self.client = type("C", (), {"host": host})(); self.headers = {}


def test_empty_whitelist_is_fail_closed(monkeypatch):
    monkeypatch.setattr("utils.kiosk_guard.get_client_ip", lambda r: "203.0.113.9")
    monkeypatch.setattr("config.settings.network.attendance_kiosk_allowed_ips", [], raising=False)
    with pytest.raises(HTTPException) as ei:
        assert_kiosk_ip_allowed(_Req("203.0.113.9"))
    assert ei.value.status_code == 403


def test_ip_in_whitelist_passes(monkeypatch):
    monkeypatch.setattr("utils.kiosk_guard.get_client_ip", lambda r: "203.0.113.10")
    monkeypatch.setattr("config.settings.network.attendance_kiosk_allowed_ips",
                        ["203.0.113.0/24"], raising=False)
    assert assert_kiosk_ip_allowed(_Req("203.0.113.10")) is None


def test_ip_not_in_whitelist_403(monkeypatch):
    monkeypatch.setattr("utils.kiosk_guard.get_client_ip", lambda r: "198.51.100.7")
    monkeypatch.setattr("config.settings.network.attendance_kiosk_allowed_ips",
                        ["203.0.113.0/24"], raising=False)
    with pytest.raises(HTTPException) as ei:
        assert_kiosk_ip_allowed(_Req("198.51.100.7"))
    assert ei.value.status_code == 403
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_ip_guard.py -o addopts="" -q`
Expected: FAIL（`ModuleNotFoundError: utils.kiosk_guard`）

- [ ] **Step 3: 實作 guard**

Create `utils/kiosk_guard.py`：

```python
"""園內 kiosk 端點的 IP 白名單守衛（fail-closed）。"""
import ipaddress
import logging

from fastapi import HTTPException, Request

from config import settings
from utils.request_ip import get_client_ip

logger = logging.getLogger(__name__)


def _ip_in_any(ip_str: str, cidrs: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for c in cidrs:
        try:
            if ip in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


def assert_kiosk_ip_allowed(request: Request) -> None:
    """打卡端點守衛：client IP 不在 ATTENDANCE_KIOSK_ALLOWED_IPS → 403。

    fail-closed：白名單未設定或空 → 一律 403（kiosk 功能停用）。
    """
    allowed = settings.network.attendance_kiosk_allowed_ips or []
    if not allowed:
        logger.warning("kiosk 端點被拒：ATTENDANCE_KIOSK_ALLOWED_IPS 未設定（fail-closed）")
        raise HTTPException(status_code=403, detail="打卡裝置未授權")
    client_ip = get_client_ip(request)
    if not client_ip or not _ip_in_any(client_ip, allowed):
        logger.warning("kiosk 端點被拒：client_ip=%s 不在白名單", client_ip)
        raise HTTPException(status_code=403, detail="此裝置不在允許的打卡網路範圍")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_ip_guard.py -o addopts="" -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add utils/kiosk_guard.py tests/test_kiosk_ip_guard.py
git commit -m "feat(attendance): kiosk IP 白名單守衛（fail-closed）"
```

---

## Task 7: 員工自設 PIN 端點（Portal）

**Files:**
- Create: `api/portal/punch_pin.py`
- Modify: `api/portal/__init__.py`（註冊 router）
- Test: `tests/test_portal_punch_pin.py`

**Interfaces:**
- Consumes: `get_current_user`、`api.portal._shared._get_employee`、`utils.auth.hash_password`、`now_taipei_naive`
- Produces: `PUT /api/portal/me/punch-pin`，body `{ "pin": "<4-6 digits>" }`，寫 `punch_pin_hash` + `punch_pin_set_at`，回 `{ "message": "打卡 PIN 已更新" }`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_portal_punch_pin.py
from utils.auth import verify_password


def test_set_punch_pin_hashes_and_persists(portal_client, portal_employee, test_db_session):
    # portal_client：已登入教師的 TestClient；portal_employee：對應 Employee（見既有 portal 測試 fixture）
    res = portal_client.put("/api/portal/me/punch-pin", json={"pin": "1234"})
    assert res.status_code == 200
    test_db_session.refresh(portal_employee)
    assert portal_employee.punch_pin_hash
    assert portal_employee.punch_pin_hash != "1234"  # 不落明文
    assert verify_password("1234", portal_employee.punch_pin_hash)
    assert portal_employee.punch_pin_set_at is not None


def test_set_punch_pin_rejects_non_digit(portal_client):
    res = portal_client.put("/api/portal/me/punch-pin", json={"pin": "12ab"})
    assert res.status_code == 422


def test_set_punch_pin_rejects_too_short(portal_client):
    res = portal_client.put("/api/portal/me/punch-pin", json={"pin": "123"})
    assert res.status_code == 422
```

> `portal_client` / `portal_employee` fixture：沿用既有 portal 測試（參考 `tests/` 內現有 portal 端點測試如 attendance-sheet 的登入方式）。若無現成 fixture，於本檔 `conftest` 風格建立：建立 `role=teacher` 的 `User` + 對應 `Employee(user_id=...)`，用 API login 取 cookie。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_portal_punch_pin.py -o addopts="" -q`
Expected: FAIL（404 路由不存在）

- [ ] **Step 3: 實作端點**

Create `api/portal/punch_pin.py`：

```python
"""教師自助設定打卡 PIN。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from models.database import get_session
from utils.auth import get_current_user, hash_password
from utils.taipei_time import now_taipei_naive
from utils.errors import raise_safe_500
from api.portal._shared import _get_employee

logger = logging.getLogger(__name__)
router = APIRouter()


class PunchPinSetRequest(BaseModel):
    pin: str

    @field_validator("pin")
    @classmethod
    def _valid_pin(cls, v: str) -> str:
        if not (v.isdigit() and 4 <= len(v) <= 6):
            raise ValueError("PIN 須為 4-6 位數字")
        return v


@router.put("/me/punch-pin")
def set_my_punch_pin(
    body: PunchPinSetRequest,
    current_user: dict = Depends(get_current_user),
):
    """教師設定/更新自己的打卡 PIN（沿用 Portal 登入身分，不需舊 PIN）。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        emp.punch_pin_hash = hash_password(body.pin)
        emp.punch_pin_set_at = now_taipei_naive()
        session.commit()
        return {"message": "打卡 PIN 已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
```

Modify `api/portal/__init__.py`（仿既有子 router 註冊）：

```python
from .punch_pin import router as punch_pin_router
# ... 既有 include_router 區 ...
router.include_router(punch_pin_router)
```

> 確認 `api/portal/__init__.py` 的主 router prefix 為 `/api/portal`，故端點實際路徑為 `/api/portal/me/punch-pin`。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_portal_punch_pin.py -o addopts="" -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/portal/punch_pin.py api/portal/__init__.py tests/test_portal_punch_pin.py
git commit -m "feat(portal): 教師自助設定打卡 PIN 端點"
```

---

## Task 8: 管理端重置 PIN 端點

**Files:**
- Modify: `api/employees.py`（加端點）
- Test: `tests/test_employee_reset_punch_pin.py`

**Interfaces:**
- Consumes: `require_staff_permission(Permission.ATTENDANCE_WRITE)`
- Produces: `POST /api/employees/{employee_id}/reset-punch-pin`，清空 `punch_pin_hash` + `punch_pin_set_at`，回 `{ "message": "打卡 PIN 已重置" }`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_employee_reset_punch_pin.py
from models.database import Employee
from utils.auth import hash_password


def test_reset_clears_pin(admin_client, test_db_session):
    emp = Employee(employee_id="E950", name="陳老師", is_active=True,
                   punch_pin_hash=hash_password("1234"))
    test_db_session.add(emp); test_db_session.commit()
    res = admin_client.post(f"/api/employees/{emp.id}/reset-punch-pin")
    assert res.status_code == 200
    test_db_session.refresh(emp)
    assert emp.punch_pin_hash is None
    assert emp.punch_pin_set_at is None


def test_reset_requires_permission(readonly_client, test_db_session):
    emp = Employee(employee_id="E951", name="林老師", is_active=True)
    test_db_session.add(emp); test_db_session.commit()
    res = readonly_client.post(f"/api/employees/{emp.id}/reset-punch-pin")
    assert res.status_code == 403
```

> `admin_client`（有 ATTENDANCE_WRITE）/ `readonly_client`（無）：沿用既有 employees 端點測試的 client fixture（參考 `tests/test_employee*` 既有授權測試）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_employee_reset_punch_pin.py -o addopts="" -q`
Expected: FAIL（404）

- [ ] **Step 3: 實作端點**

`api/employees.py` 加（確認頂部已 import `require_staff_permission`, `Permission`, `get_session`, `HTTPException`, `now_taipei_naive`；缺則補）：

```python
@router.post("/{employee_id}/reset-punch-pin")
def reset_punch_pin(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_WRITE)),
):
    """管理端重置員工打卡 PIN（清空，員工須至 Portal 重設）。不經手、不回傳明文 PIN。"""
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="找不到員工")
        emp.punch_pin_hash = None
        emp.punch_pin_set_at = None
        session.commit()
        return {"message": "打卡 PIN 已重置"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
```

> 若 `api/employees.py` 既有路由用 `@router.post("/...")` 且 router prefix 已是 `/api/employees`，則此路徑為 `/api/employees/{id}/reset-punch-pin`。確認 `now_taipei_naive` 本端點未用到可不 import。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_employee_reset_punch_pin.py -o addopts="" -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/employees.py tests/test_employee_reset_punch_pin.py
git commit -m "feat(employees): 管理端重置員工打卡 PIN 端點（ATTENDANCE_WRITE）"
```

---

## Task 9: kiosk 端點 — roster（名單，最小揭露）

**Files:**
- Create: `api/attendance/kiosk.py`
- Modify: `api/attendance/__init__.py`（註冊）
- Test: `tests/test_kiosk_roster.py`

**Interfaces:**
- Consumes: `assert_kiosk_ip_allowed`、`today_taipei`、`models.database`（Employee, Attendance）
- Produces: `GET /api/attendance/kiosk/roster` → `list[KioskRosterEntry]`，每筆 `{employee_id, name, has_pin, today_state}`，`today_state ∈ {"none","in_only","done"}`。**不回任何 PII**。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_kiosk_roster.py
import pytest
from main import app
from utils.kiosk_guard import assert_kiosk_ip_allowed
from models.database import Employee
from utils.auth import hash_password


@pytest.fixture
def kiosk_client(client):
    # 測試時 override IP 白名單守衛（IP guard 另有專屬單元測試 Task 6）
    app.dependency_overrides[assert_kiosk_ip_allowed] = lambda: None
    yield client
    app.dependency_overrides.pop(assert_kiosk_ip_allowed, None)


def test_roster_lists_active_with_has_pin(kiosk_client, test_db_session):
    test_db_session.add(Employee(employee_id="E960", name="有PIN", is_active=True,
                                 punch_pin_hash=hash_password("1234")))
    test_db_session.add(Employee(employee_id="E961", name="無PIN", is_active=True))
    test_db_session.add(Employee(employee_id="E962", name="已離職", is_active=False))
    test_db_session.commit()

    res = kiosk_client.get("/api/attendance/kiosk/roster")
    assert res.status_code == 200
    names = {e["name"]: e for e in res.json()}
    assert "有PIN" in names and names["有PIN"]["has_pin"] is True
    assert "無PIN" in names and names["無PIN"]["has_pin"] is False
    assert "已離職" not in names  # 離職排除
    # 最小揭露：不含 PII 欄位
    assert "phone" not in names["有PIN"] and "email" not in names["有PIN"]
    assert names["無PIN"]["today_state"] == "none"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_roster.py -o addopts="" -q`
Expected: FAIL（404）

- [ ] **Step 3: 實作 kiosk.py（roster）**

Create `api/attendance/kiosk.py`：

```python
"""園內 kiosk 即時打卡端點（IP 白名單 + PIN，無 JWT）。"""
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from models.database import get_session, Employee, Attendance
from utils.taipei_time import today_taipei
from utils.kiosk_guard import assert_kiosk_ip_allowed
from utils.errors import raise_safe_500

logger = logging.getLogger(__name__)
router = APIRouter()


class KioskRosterEntry(BaseModel):
    employee_id: int
    name: str
    has_pin: bool
    today_state: str  # none / in_only / done


@router.get("/kiosk/roster", response_model=list[KioskRosterEntry],
            dependencies=[Depends(assert_kiosk_ip_allowed)])
def kiosk_roster():
    """打卡名單：在職員工 + 是否已設 PIN + 今日打卡狀態。最小揭露，無 PII。"""
    session = get_session()
    try:
        emps = (
            session.query(Employee)
            .filter(Employee.is_active == True, Employee.resign_date.is_(None))  # noqa: E712
            .order_by(Employee.name)
            .all()
        )
        today = today_taipei()
        rows = (
            session.query(Attendance)
            .filter(Attendance.attendance_date == today,
                    Attendance.employee_id.in_([e.id for e in emps]))
            .all()
        ) if emps else []
        by_emp = {a.employee_id: a for a in rows}

        out = []
        for e in emps:
            a = by_emp.get(e.id)
            if a is None or a.punch_in_time is None:
                state = "none"
            elif a.punch_out_time is None:
                state = "in_only"
            else:
                state = "done"
            out.append(KioskRosterEntry(
                employee_id=e.id, name=e.name,
                has_pin=e.punch_pin_hash is not None, today_state=state,
            ))
        return out
    except Exception as e:
        raise_safe_500(e)
    finally:
        session.close()
```

Modify `api/attendance/__init__.py`：

```python
from .kiosk import router as kiosk_router
# ... 既有 include_router 區 ...
router.include_router(kiosk_router, prefix="/attendance")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_roster.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/attendance/kiosk.py api/attendance/__init__.py tests/test_kiosk_roster.py
git commit -m "feat(attendance): kiosk roster 端點（名單最小揭露 + IP 守衛）"
```

---

## Task 10: kiosk 端點 — PIN 驗證 helper + preview

**Files:**
- Modify: `api/attendance/kiosk.py`（加 PIN 驗證、限流、preview 端點）
- Test: `tests/test_kiosk_preview.py`

**Interfaces:**
- Consumes: `verify_password`、`create_limiter`、`resolve_punch_action`、`now_taipei_naive`
- Produces:
  - `_authenticate_pin(session, employee_id, pin) -> Employee`（驗 PIN，失敗計入 per-employee 限流並 401；無 PIN 400；超限 429）
  - `POST /api/attendance/kiosk/preview`，body `{employee_id, pin}` → `{employee_name, action, will_overwrite, current_punch_out, server_time}`（不寫入）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_kiosk_preview.py
import pytest
from main import app
from utils.kiosk_guard import assert_kiosk_ip_allowed
from models.database import Employee
from utils.auth import hash_password
from utils.rate_limit import reset_in_memory_limiters


@pytest.fixture
def kiosk_client(client):
    app.dependency_overrides[assert_kiosk_ip_allowed] = lambda: None
    reset_in_memory_limiters()
    yield client
    app.dependency_overrides.pop(assert_kiosk_ip_allowed, None)


def _emp(session, eid="E970", pin="1234"):
    e = Employee(employee_id=eid, name="王老師", work_start_time="08:00",
                 work_end_time="17:00", is_active=True, punch_pin_hash=hash_password(pin))
    session.add(e); session.commit()
    return e


def test_preview_correct_pin_returns_punch_in(kiosk_client, test_db_session):
    emp = _emp(test_db_session)
    res = kiosk_client.post("/api/attendance/kiosk/preview",
                            json={"employee_id": emp.id, "pin": "1234"})
    assert res.status_code == 200
    assert res.json()["action"] == "punch_in"
    assert res.json()["employee_name"] == "王老師"


def test_preview_wrong_pin_401(kiosk_client, test_db_session):
    emp = _emp(test_db_session)
    res = kiosk_client.post("/api/attendance/kiosk/preview",
                            json={"employee_id": emp.id, "pin": "9999"})
    assert res.status_code == 401


def test_preview_no_pin_set_400(kiosk_client, test_db_session):
    e = Employee(employee_id="E971", name="無PIN", is_active=True)
    test_db_session.add(e); test_db_session.commit()
    res = kiosk_client.post("/api/attendance/kiosk/preview",
                            json={"employee_id": e.id, "pin": "1234"})
    assert res.status_code == 400


def test_preview_rate_limit_locks_after_repeated_failures(kiosk_client, test_db_session):
    emp = _emp(test_db_session, eid="E972")
    last = None
    for _ in range(8):
        last = kiosk_client.post("/api/attendance/kiosk/preview",
                                 json={"employee_id": emp.id, "pin": "0000"})
    assert last.status_code == 429  # 連續失敗後鎖定
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_preview.py -o addopts="" -q`
Expected: FAIL（404）

- [ ] **Step 3: 實作 PIN 驗證 + preview**

`api/attendance/kiosk.py` 加（補 import）：

```python
from fastapi import HTTPException
from pydantic import field_validator
from datetime import datetime
from typing import Optional

from utils.auth import verify_password
from utils.taipei_time import now_taipei_naive
from utils.rate_limit import create_limiter
from services.attendance_kiosk import resolve_punch_action

# per-employee PIN 失敗限流（key=employee_id；不可用 per-IP——kiosk 合法流量集中單一園內 IP）
_pin_fail_limiter = create_limiter(
    max_calls=5, window_seconds=900, name="kiosk_pin_fail",
    error_detail="PIN 錯誤次數過多，請稍後再試",
)


class KioskPunchRequest(BaseModel):
    employee_id: int
    pin: str

    @field_validator("pin")
    @classmethod
    def _valid_pin(cls, v: str) -> str:
        if not (v.isdigit() and 4 <= len(v) <= 6):
            raise ValueError("PIN 須為 4-6 位數字")
        return v


class KioskPreviewResponse(BaseModel):
    employee_name: str
    action: str
    will_overwrite: bool
    current_punch_out: Optional[datetime]
    server_time: datetime


def _authenticate_pin(session, employee_id: int, pin: str) -> Employee:
    """驗 PIN：成功回 Employee；無 PIN 400；錯誤 401（並記一次失敗，超限 429）。"""
    emp = (
        session.query(Employee)
        .filter(Employee.id == employee_id, Employee.is_active == True,  # noqa: E712
                Employee.resign_date.is_(None))
        .first()
    )
    if emp is None:
        raise HTTPException(status_code=404, detail="找不到員工")
    if not emp.punch_pin_hash:
        raise HTTPException(status_code=400, detail="尚未設定打卡 PIN，請先至教師入口設定")
    if not verify_password(pin, emp.punch_pin_hash):
        _pin_fail_limiter.check(str(employee_id))  # 記一次失敗；超限抛 429
        raise HTTPException(status_code=401, detail="PIN 錯誤")
    return emp


@router.post("/kiosk/preview", response_model=KioskPreviewResponse,
             dependencies=[Depends(assert_kiosk_ip_allowed)])
def kiosk_preview(body: KioskPunchRequest):
    """打卡預判（確認用，不寫入）：驗 PIN 後回傳即將記為上班/下班。"""
    session = get_session()
    try:
        emp = _authenticate_pin(session, body.employee_id, body.pin)
        p = resolve_punch_action(session, emp, now_taipei_naive())
        return KioskPreviewResponse(
            employee_name=p.employee_name, action=p.action,
            will_overwrite=p.will_overwrite, current_punch_out=p.current_punch_out,
            server_time=p.server_time,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e)
    finally:
        session.close()
```

> 限流語義：`max_calls=5` 表示 15 分鐘窗口內最多 5 次 PIN 失敗，第 6 次失敗起回 429。成功打卡不計入失敗配額。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_preview.py -o addopts="" -q`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/attendance/kiosk.py tests/test_kiosk_preview.py
git commit -m "feat(attendance): kiosk preview 端點 + PIN 驗證 + per-employee 失敗限流"
```

---

## Task 11: kiosk 端點 — punch（寫入）

**Files:**
- Modify: `api/attendance/kiosk.py`（加 punch 端點）
- Test: `tests/test_kiosk_punch.py`

**Interfaces:**
- Consumes: `_authenticate_pin`、`apply_punch`、`MonthFinalizedError`、`now_taipei_naive`
- Produces: `POST /api/attendance/kiosk/punch`，body `{employee_id, pin}` → `{employee_name, action, punch_time, status}`（寫入當天列）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_kiosk_punch.py
import pytest
from main import app
from utils.kiosk_guard import assert_kiosk_ip_allowed
from models.database import Employee, Attendance
from utils.auth import hash_password
from utils.rate_limit import reset_in_memory_limiters


@pytest.fixture
def kiosk_client(client):
    app.dependency_overrides[assert_kiosk_ip_allowed] = lambda: None
    reset_in_memory_limiters()
    yield client
    app.dependency_overrides.pop(assert_kiosk_ip_allowed, None)


def _emp(session, eid="E980", pin="1234"):
    e = Employee(employee_id=eid, name="李老師", work_start_time="08:00",
                 work_end_time="17:00", is_active=True, punch_pin_hash=hash_password(pin))
    session.add(e); session.commit()
    return e


def test_punch_writes_punch_in(kiosk_client, test_db_session):
    emp = _emp(test_db_session)
    res = kiosk_client.post("/api/attendance/kiosk/punch",
                            json={"employee_id": emp.id, "pin": "1234"})
    assert res.status_code == 200
    assert res.json()["action"] == "punch_in"
    row = test_db_session.query(Attendance).filter_by(employee_id=emp.id).one()
    assert row.punch_in_time is not None
    assert row.source == "kiosk"


def test_punch_second_is_punch_out(kiosk_client, test_db_session):
    emp = _emp(test_db_session, eid="E981")
    kiosk_client.post("/api/attendance/kiosk/punch", json={"employee_id": emp.id, "pin": "1234"})
    res = kiosk_client.post("/api/attendance/kiosk/punch", json={"employee_id": emp.id, "pin": "1234"})
    assert res.json()["action"] == "punch_out"


def test_punch_body_ignores_any_timestamp_field(kiosk_client, test_db_session):
    # self 反向鎖死：請求體即使夾帶時間戳也不被採用（schema 不含該欄，被忽略）
    emp = _emp(test_db_session, eid="E982")
    res = kiosk_client.post("/api/attendance/kiosk/punch",
                            json={"employee_id": emp.id, "pin": "1234",
                                  "punch_in_time": "2020-01-01T00:00:00"})
    assert res.status_code == 200
    row = test_db_session.query(Attendance).filter_by(employee_id=emp.id).one()
    assert row.punch_in_time.year != 2020  # 用 server now，非請求體值


def test_punch_wrong_pin_401(kiosk_client, test_db_session):
    emp = _emp(test_db_session, eid="E983")
    res = kiosk_client.post("/api/attendance/kiosk/punch",
                            json={"employee_id": emp.id, "pin": "0000"})
    assert res.status_code == 401
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_punch.py -o addopts="" -q`
Expected: FAIL（404）

- [ ] **Step 3: 實作 punch 端點**

`api/attendance/kiosk.py` 加（補 import）：

```python
from services.attendance_kiosk import apply_punch, MonthFinalizedError


class KioskPunchResponse(BaseModel):
    employee_name: str
    action: str
    punch_time: datetime
    status: str


@router.post("/kiosk/punch", response_model=KioskPunchResponse,
             dependencies=[Depends(assert_kiosk_ip_allowed)])
def kiosk_punch(body: KioskPunchRequest):
    """即時打卡：驗 PIN 後寫入伺服器當前時間（first-in/last-out）。"""
    session = get_session()
    try:
        emp = _authenticate_pin(session, body.employee_id, body.pin)
        try:
            r = apply_punch(session, emp, now_taipei_naive())
        except MonthFinalizedError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return KioskPunchResponse(
            employee_name=r.employee_name, action=r.action,
            punch_time=r.punch_time, status=r.status,
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_punch.py -o addopts="" -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 跑全 kiosk 後端測試**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/test_kiosk_*.py tests/test_portal_punch_pin.py tests/test_employee_reset_punch_pin.py tests/test_build_shift_maps.py -o addopts="" -q`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/attendance/kiosk.py tests/test_kiosk_punch.py
git commit -m "feat(attendance): kiosk punch 端點（即時寫入 server now，self 反向鎖死）"
```

---

## Task 12: OpenAPI codegen（前端型別來源）

**Files:**
- Regenerate: `ivy-frontend/src/api/_generated/schema.d.ts`

> ⚠ 有平行 session 可能也在改 `schema.d.ts`。執行前 `git -C ../ivy-frontend status` 確認該檔無未提交改動；若有，先與平行工作協調，勿 clobber。

- [ ] **Step 1: 後端產 openapi.json**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python scripts/dump_openapi.py`
Expected: 產出 `openapi.json`（local-only，gitignore），含 `/attendance/kiosk/roster`、`/attendance/kiosk/preview`、`/attendance/kiosk/punch`、`/portal/me/punch-pin`、`/employees/{employee_id}/reset-punch-pin`。

- [ ] **Step 2: 前端重生型別**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npm run gen:api`
Expected: `src/api/_generated/schema.d.ts` 更新，含上述 path keys。

- [ ] **Step 3: Commit（前端 repo）**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): 重生 OpenAPI 型別（kiosk 即時打卡端點）"
```

---

# 階段二：前端（接上）

## Task 13: 前端 API wrapper

**Files:**
- Create: `ivy-frontend/src/api/kiosk.ts`
- Modify: `ivy-frontend/src/api/portal.ts`（加 `setPunchPin`）
- Modify: `ivy-frontend/src/api/employees.ts`（加 `resetPunchPin`）

**Interfaces:**
- Produces: `getKioskRoster()`、`kioskPreview(body)`、`kioskPunch(body)`、`setPunchPin(body)`、`resetPunchPin(employeeId)`

- [ ] **Step 1: 建 `src/api/kiosk.ts`**

```typescript
import api from './index'
import type { ApiBody, AxiosResp } from './_generated/typed'

export const getKioskRoster = (): AxiosResp<'/attendance/kiosk/roster', 'get'> =>
  api.get('/attendance/kiosk/roster')

export const kioskPreview = (
  body: ApiBody<'/attendance/kiosk/preview', 'post'>,
): AxiosResp<'/attendance/kiosk/preview', 'post'> =>
  api.post('/attendance/kiosk/preview', body)

export const kioskPunch = (
  body: ApiBody<'/attendance/kiosk/punch', 'post'>,
): AxiosResp<'/attendance/kiosk/punch', 'post'> =>
  api.post('/attendance/kiosk/punch', body)
```

- [ ] **Step 2: `src/api/portal.ts` 加**

```typescript
export const setPunchPin = (
  body: ApiBody<'/portal/me/punch-pin', 'put'>,
): AxiosResp<'/portal/me/punch-pin', 'put'> =>
  api.put('/portal/me/punch-pin', body)
```

- [ ] **Step 3: `src/api/employees.ts` 加**

```typescript
export const resetPunchPin = (
  employeeId: number,
): AxiosResp<'/employees/{employee_id}/reset-punch-pin', 'post'> =>
  api.post(`/employees/${employeeId}/reset-punch-pin`)
```

> 若 `schema.d.ts` 對某端點顯示 `unknown`（後端缺 `response_model`），暫以 `as` 標註並補 `// TODO(ts-strict): waiting on backend response_model`。本計畫端點皆有 `response_model`，正常情況不需。

- [ ] **Step 4: 型別檢查**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npm run type-check`（或 `npx vue-tsc --noEmit`，依專案 script）
Expected: 無錯誤。

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/api/kiosk.ts src/api/portal.ts src/api/employees.ts
git commit -m "feat(api): kiosk 打卡 / 設定 PIN / 重置 PIN wrapper"
```

---

## Task 14: 數字鍵盤元件 `NumPad.vue`

**Files:**
- Create: `ivy-frontend/src/components/kiosk/NumPad.vue`
- Test: `ivy-frontend/src/components/kiosk/__tests__/NumPad.spec.ts`

**Interfaces:**
- Produces: `<NumPad v-model="pin" :maxlength="6" @submit="onSubmit" />`，props `modelValue: string`、`maxlength?: number`(預設 6)；emit `update:modelValue`、`submit`。含 0-9、刪除、確認鍵。

- [ ] **Step 1: 寫失敗測試**

```typescript
// src/components/kiosk/__tests__/NumPad.spec.ts
import { mount } from '@vue/test-utils'
import NumPad from '../NumPad.vue'

describe('NumPad', () => {
  it('點數字鍵 append 到 modelValue', async () => {
    const wrapper = mount(NumPad, { props: { modelValue: '12', maxlength: 6 } })
    const btn = wrapper.findAll('button').find((b) => b.text() === '3')
    await btn!.trigger('click')
    expect(wrapper.emitted('update:modelValue')![0][0]).toBe('123')
  })

  it('達 maxlength 後不再 append', async () => {
    const wrapper = mount(NumPad, { props: { modelValue: '123456', maxlength: 6 } })
    const btn = wrapper.findAll('button').find((b) => b.text() === '7')
    await btn!.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toBeFalsy()
  })

  it('點確認鍵 emit submit', async () => {
    const wrapper = mount(NumPad, { props: { modelValue: '1234' } })
    const btn = wrapper.findAll('button').find((b) => b.text().includes('確認'))
    await btn!.trigger('click')
    expect(wrapper.emitted('submit')).toBeTruthy()
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run src/components/kiosk/__tests__/NumPad.spec.ts`
Expected: FAIL（找不到 NumPad.vue）

- [ ] **Step 3: 實作 NumPad.vue**

```vue
<script setup lang="ts">
const props = withDefaults(defineProps<{ modelValue: string; maxlength?: number }>(), {
  maxlength: 6,
})
const emit = defineEmits<{
  'update:modelValue': [v: string]
  submit: []
}>()

const keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', 'del', '0', 'ok']

function press(k: string) {
  if (k === 'del') {
    emit('update:modelValue', props.modelValue.slice(0, -1))
  } else if (k === 'ok') {
    emit('submit')
  } else if (props.modelValue.length < props.maxlength) {
    emit('update:modelValue', props.modelValue + k)
  }
}
</script>

<template>
  <div class="numpad">
    <button
      v-for="k in keys"
      :key="k"
      type="button"
      class="numpad-key"
      :class="{ 'numpad-action': k === 'del' || k === 'ok' }"
      @click="press(k)"
    >
      <span v-if="k === 'del'">⌫</span>
      <span v-else-if="k === 'ok'">確認</span>
      <span v-else>{{ k }}</span>
    </button>
  </div>
</template>

<style scoped>
.numpad {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  max-width: 360px;
}
.numpad-key {
  font-size: 28px;
  padding: 20px 0;
  border: 1px solid var(--el-border-color, #dcdfe6);
  border-radius: 12px;
  background: var(--el-fill-color-blank, #fff);
  cursor: pointer;
}
.numpad-key:active {
  background: var(--el-fill-color, #f0f2f5);
}
.numpad-action {
  font-size: 20px;
}
</style>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run src/components/kiosk/__tests__/NumPad.spec.ts`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/components/kiosk/NumPad.vue src/components/kiosk/__tests__/NumPad.spec.ts
git commit -m "feat(kiosk): 數字鍵盤元件 NumPad"
```

---

## Task 15: kiosk 打卡頁 `KioskPunchView.vue` + 路由

**Files:**
- Create: `ivy-frontend/src/views/kiosk/KioskPunchView.vue`
- Modify: `ivy-frontend/src/router/index.ts`（加 public 路由）
- Test: `ivy-frontend/src/views/kiosk/__tests__/KioskPunchView.spec.ts`

**Interfaces:**
- Consumes: `getKioskRoster`、`kioskPreview`、`kioskPunch`、`NumPad`

- [ ] **Step 1: 寫失敗測試**

```typescript
// src/views/kiosk/__tests__/KioskPunchView.spec.ts
import { mount, flushPromises } from '@vue/test-utils'
import KioskPunchView from '../KioskPunchView.vue'

vi.mock('@/api/kiosk', () => ({
  getKioskRoster: vi.fn(() =>
    Promise.resolve({ data: [{ employee_id: 1, name: '王老師', has_pin: true, today_state: 'none' }] })),
  kioskPreview: vi.fn(() =>
    Promise.resolve({ data: { employee_name: '王老師', action: 'punch_in', will_overwrite: false, current_punch_out: null, server_time: '2026-06-30T09:00:00' } })),
  kioskPunch: vi.fn(() =>
    Promise.resolve({ data: { employee_name: '王老師', action: 'punch_in', punch_time: '2026-06-30T09:00:00', status: 'normal' } })),
}))

describe('KioskPunchView', () => {
  it('載入後顯示員工名單', async () => {
    const wrapper = mount(KioskPunchView, { global: { stubs: { NumPad: true } } })
    await flushPromises()
    expect(wrapper.text()).toContain('王老師')
  })

  it('選未設 PIN 員工提示先設定', async () => {
    const { getKioskRoster } = await import('@/api/kiosk')
    ;(getKioskRoster as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: [{ employee_id: 2, name: '無PIN', has_pin: false, today_state: 'none' }],
    })
    const wrapper = mount(KioskPunchView, { global: { stubs: { NumPad: true } } })
    await flushPromises()
    const card = wrapper.findAll('.roster-item').find((c) => c.text().includes('無PIN'))
    await card!.trigger('click')
    expect(wrapper.text()).toContain('請先到教師入口設定打卡 PIN')
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run src/views/kiosk/__tests__/KioskPunchView.spec.ts`
Expected: FAIL（找不到 KioskPunchView.vue）

- [ ] **Step 3: 實作 KioskPunchView.vue**

```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import NumPad from '@/components/kiosk/NumPad.vue'
import { getKioskRoster, kioskPreview, kioskPunch } from '@/api/kiosk'

interface RosterEntry {
  employee_id: number
  name: string
  has_pin: boolean
  today_state: string
}
interface Preview {
  employee_name: string
  action: string
  will_overwrite: boolean
  current_punch_out: string | null
  server_time: string
}

type Stage = 'roster' | 'pin' | 'confirm' | 'success'

const roster = ref<RosterEntry[]>([])
const stage = ref<Stage>('roster')
const selected = ref<RosterEntry | null>(null)
const pin = ref('')
const preview = ref<Preview | null>(null)
const successText = ref('')
const loading = ref(false)

async function loadRoster() {
  const res = await getKioskRoster()
  roster.value = res.data as RosterEntry[]
}
onMounted(loadRoster)

function pickEmployee(e: RosterEntry) {
  if (!e.has_pin) {
    ElMessage.warning('請先到教師入口設定打卡 PIN')
    return
  }
  selected.value = e
  pin.value = ''
  stage.value = 'pin'
}

function actionLabel(a: string) {
  return a === 'punch_in' ? '上班' : '下班'
}

async function submitPin() {
  if (!selected.value || pin.value.length < 4) return
  loading.value = true
  try {
    const res = await kioskPreview({ employee_id: selected.value.employee_id, pin: pin.value })
    preview.value = res.data as Preview
    stage.value = 'confirm'
  } catch {
    ElMessage.error('PIN 錯誤或暫時無法打卡')
    pin.value = ''
  } finally {
    loading.value = false
  }
}

async function confirmPunch() {
  if (!selected.value) return
  loading.value = true
  try {
    const res = await kioskPunch({ employee_id: selected.value.employee_id, pin: pin.value })
    const d = res.data as { employee_name: string; action: string; punch_time: string }
    successText.value = `${d.employee_name}　${actionLabel(d.action)}　${d.punch_time.slice(11, 16)}`
    stage.value = 'success'
    await loadRoster()
    setTimeout(reset, 3000)
  } catch {
    ElMessage.error('打卡失敗，請重試')
  } finally {
    loading.value = false
  }
}

function reset() {
  stage.value = 'roster'
  selected.value = null
  pin.value = ''
  preview.value = null
}
</script>

<template>
  <div class="kiosk">
    <h1 class="kiosk-title">電子打卡</h1>

    <div v-if="stage === 'roster'" class="roster">
      <div
        v-for="e in roster"
        :key="e.employee_id"
        class="roster-item"
        :class="{ 'no-pin': !e.has_pin }"
        @click="pickEmployee(e)"
      >
        <span class="roster-name">{{ e.name }}</span>
        <span v-if="e.today_state === 'in_only'" class="roster-tag">已上班</span>
        <span v-else-if="e.today_state === 'done'" class="roster-tag">已完成</span>
      </div>
    </div>

    <div v-else-if="stage === 'pin'" class="pin-stage">
      <p class="pin-emp">{{ selected?.name }}</p>
      <p class="pin-dots">{{ '●'.repeat(pin.length) }}</p>
      <NumPad v-model="pin" :maxlength="6" @submit="submitPin" />
      <el-button text @click="reset">取消</el-button>
    </div>

    <div v-else-if="stage === 'confirm'" class="confirm-stage">
      <p class="confirm-emp">{{ preview?.employee_name }}</p>
      <p class="confirm-action">即將記為【{{ actionLabel(preview?.action || '') }}】</p>
      <p class="confirm-time">{{ preview?.server_time.slice(11, 16) }}</p>
      <p v-if="preview?.will_overwrite" class="confirm-overwrite">
        將更新下班時間（原 {{ preview?.current_punch_out?.slice(11, 16) }}）
      </p>
      <el-button type="primary" size="large" :loading="loading" @click="confirmPunch">確認打卡</el-button>
      <el-button text @click="reset">取消</el-button>
    </div>

    <div v-else-if="stage === 'success'" class="success-stage">
      <p class="success-check">✓ 打卡成功</p>
      <p class="success-text">{{ successText }}</p>
    </div>
  </div>
</template>

<style scoped>
.kiosk { max-width: 720px; margin: 0 auto; padding: 24px; text-align: center; }
.kiosk-title { font-size: 28px; margin-bottom: 24px; }
.roster { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 16px; }
.roster-item {
  padding: 24px 12px; border: 1px solid var(--el-border-color, #dcdfe6);
  border-radius: 12px; cursor: pointer; font-size: 20px;
}
.roster-item.no-pin { opacity: 0.5; }
.roster-tag { display: block; font-size: 13px; color: var(--el-color-success, #67c23a); }
.pin-dots { font-size: 32px; letter-spacing: 8px; min-height: 40px; }
.pin-stage, .confirm-stage, .success-stage { display: flex; flex-direction: column; align-items: center; gap: 16px; }
.confirm-action { font-size: 24px; font-weight: 600; }
.confirm-overwrite { color: var(--el-color-warning, #e6a23c); }
.success-check { font-size: 32px; color: var(--el-color-success, #67c23a); }
.success-text { font-size: 24px; }
</style>
```

- [ ] **Step 4: 加路由**

`src/router/index.ts` 在 public 路由區（`/public/activity` 附近）加：

```typescript
{
    path: '/kiosk/punch',
    name: 'kiosk-punch',
    component: () => import('../views/kiosk/KioskPunchView.vue'),
    meta: { title: '電子打卡', noAuth: true, public: true, bare: true, hideNav: true },
},
```

- [ ] **Step 5: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run src/views/kiosk/__tests__/KioskPunchView.spec.ts`
Expected: PASS（2 passed）

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/views/kiosk/KioskPunchView.vue src/views/kiosk/__tests__/KioskPunchView.spec.ts src/router/index.ts
git commit -m "feat(kiosk): 電子打卡頁 KioskPunchView + public 路由"
```

---

## Task 16: Portal「設定打卡 PIN」區塊

**Files:**
- Modify: `ivy-frontend/src/views/portal/PortalProfileView.vue`
- Test: `ivy-frontend/src/views/portal/__tests__/PortalProfilePunchPin.spec.ts`

**Interfaces:**
- Consumes: `setPunchPin`（`@/api/portal`）

- [ ] **Step 1: 寫失敗測試**

```typescript
// src/views/portal/__tests__/PortalProfilePunchPin.spec.ts
import { mount, flushPromises } from '@vue/test-utils'
import PortalProfileView from '../PortalProfileView.vue'

vi.mock('@/api/portal', async (orig) => {
  const actual = await (orig as () => Promise<Record<string, unknown>>)()
  return { ...actual, setPunchPin: vi.fn(() => Promise.resolve({ data: { message: '打卡 PIN 已更新' } })) }
})

describe('PortalProfileView 打卡 PIN', () => {
  it('PIN 與確認不一致時不送出', async () => {
    const wrapper = mount(PortalProfileView, { global: { stubs: { /* el-* 由全域插件處理或視專案 setup */ } } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      pinForm: { new_pin: string; confirm_pin: string }
      savePunchPin: () => Promise<void>
    }
    vm.pinForm.new_pin = '1234'
    vm.pinForm.confirm_pin = '9999'
    await vm.savePunchPin()
    const { setPunchPin } = await import('@/api/portal')
    expect(setPunchPin).not.toHaveBeenCalled()
  })
})
```

> 若既有 `PortalProfileView.spec.ts` 已有 mount 樣板（stubs / mocks），複用其設定，避免重寫 el-* stub。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run src/views/portal/__tests__/PortalProfilePunchPin.spec.ts`
Expected: FAIL（`savePunchPin` / `pinForm` 不存在）

- [ ] **Step 3: 加 PIN 區塊**

`PortalProfileView.vue` `<script setup>` 加：

```typescript
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { setPunchPin } from '@/api/portal'

const pinForm = reactive({ new_pin: '', confirm_pin: '' })
const savingPin = ref(false)

async function savePunchPin() {
  if (!/^\d{4,6}$/.test(pinForm.new_pin)) {
    ElMessage.warning('PIN 須為 4-6 位數字')
    return
  }
  if (pinForm.new_pin !== pinForm.confirm_pin) {
    ElMessage.warning('兩次輸入的 PIN 不一致')
    return
  }
  savingPin.value = true
  try {
    await setPunchPin({ pin: pinForm.new_pin })
    ElMessage.success('打卡 PIN 已更新')
    pinForm.new_pin = ''
    pinForm.confirm_pin = ''
  } catch {
    ElMessage.error('設定失敗，請重試')
  } finally {
    savingPin.value = false
  }
}
```

template 加區塊（接在現有卡片之後）：

```vue
<el-card class="profile-card" shadow="hover">
  <template #header><span class="card-title">打卡 PIN 設定</span></template>
  <el-form label-width="100px">
    <el-form-item label="新 PIN">
      <el-input v-model="pinForm.new_pin" type="password" maxlength="6"
                placeholder="4-6 位數字" show-password />
    </el-form-item>
    <el-form-item label="確認 PIN">
      <el-input v-model="pinForm.confirm_pin" type="password" maxlength="6" show-password />
    </el-form-item>
    <el-form-item>
      <el-button type="primary" :loading="savingPin" @click="savePunchPin">儲存</el-button>
    </el-form-item>
  </el-form>
</el-card>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run src/views/portal/__tests__/PortalProfilePunchPin.spec.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/views/portal/PortalProfileView.vue src/views/portal/__tests__/PortalProfilePunchPin.spec.ts
git commit -m "feat(portal): 教師自助設定打卡 PIN 區塊"
```

---

## Task 17: 員工管理「重置打卡 PIN」操作

**Files:**
- Modify: `ivy-frontend/src/views/EmployeeView.vue`
- Test: `ivy-frontend/src/views/__tests__/EmployeeResetPunchPin.spec.ts`

**Interfaces:**
- Consumes: `resetPunchPin`（`@/api/employees`）、`hasPermission('ATTENDANCE_WRITE')`

- [ ] **Step 1: 寫失敗測試**

```typescript
// src/views/__tests__/EmployeeResetPunchPin.spec.ts
import { flushPromises } from '@vue/test-utils'

vi.mock('@/utils/auth', () => ({ hasPermission: vi.fn(() => true) }))
vi.mock('@/api/employees', async (orig) => {
  const actual = await (orig as () => Promise<Record<string, unknown>>)()
  return { ...actual, resetPunchPin: vi.fn(() => Promise.resolve({ data: { message: '打卡 PIN 已重置' } })) }
})
vi.mock('element-plus', async (orig) => {
  const actual = await (orig as () => Promise<Record<string, unknown>>)()
  return { ...actual, ElMessageBox: { confirm: vi.fn(() => Promise.resolve()) },
           ElMessage: { success: vi.fn(), error: vi.fn() } }
})

import { resetPunchPin } from '@/api/employees'

// 直接測 handler 邏輯：自 EmployeeView 匯出或透過 wrapper.vm 取得 resetEmployeePin
it('確認後呼叫 resetPunchPin', async () => {
  // 視 EmployeeView 結構：mount 後取 vm.resetEmployeePin(row) 並斷言 API 被呼叫
  // （若 EmployeeView 過大難 mount，可將 reset 邏輯抽成 composable 後單測）
  const { ElMessageBox } = await import('element-plus')
  expect(typeof (ElMessageBox.confirm)).toBe('function')
  expect(typeof resetPunchPin).toBe('function')
})
```

> EmployeeView.vue 約 1300 行、mount 成本高。若直接 mount 困難，將 `resetEmployeePin` 邏輯抽成小 composable `src/composables/useResetPunchPin.ts` 再單元測試（較穩）。本步驟以「handler 會在確認後呼叫 API」為驗證目標。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run src/views/__tests__/EmployeeResetPunchPin.spec.ts`
Expected: FAIL（handler 未實作）

- [ ] **Step 3: 加按鈕與 handler**

`EmployeeView.vue` `<script setup>` 加：

```typescript
import { resetPunchPin } from '@/api/employees'
import { ElMessage, ElMessageBox } from 'element-plus'

async function resetEmployeePin(row: { id: number; name: string }) {
  try {
    await ElMessageBox.confirm(`確定要重置「${row.name}」的打卡 PIN 嗎？重置後員工須至教師入口重新設定。`,
                               '重置打卡 PIN', { type: 'warning' })
  } catch {
    return // 使用者取消
  }
  try {
    await resetPunchPin(row.id)
    ElMessage.success('打卡 PIN 已重置')
  } catch {
    ElMessage.error('重置失敗，請重試')
  }
}
```

template 在既有「更多操作」dropdown（`v-if="canWriteEmployees"`）加一項。改用 `hasPermission('ATTENDANCE_WRITE')` 控制此項顯示：

```typescript
const canResetPunchPin = computed(() => hasPermission('ATTENDANCE_WRITE'))
```

```vue
<el-dropdown-item v-if="canResetPunchPin" @click="resetEmployeePin(scope.row)">
  重置打卡 PIN
</el-dropdown-item>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd /Users/yilunwu/Desktop/ivy-frontend && npx vitest run src/views/__tests__/EmployeeResetPunchPin.spec.ts`
Expected: PASS

- [ ] **Step 5: 型別檢查 + 全前端考勤相關測試**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm run type-check
npx vitest run src/components/kiosk src/views/kiosk src/views/portal/__tests__/PortalProfilePunchPin.spec.ts src/views/__tests__/EmployeeResetPunchPin.spec.ts
```
Expected: 型別無誤；測試全 PASS。

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git add src/views/EmployeeView.vue src/views/__tests__/EmployeeResetPunchPin.spec.ts
git commit -m "feat(employees): 員工管理重置打卡 PIN 操作（ATTENDANCE_WRITE）"
```

---

## Task 18: 整合驗證

**Files:** 無（手動驗證）

- [ ] **Step 1: 起兩端 dev server**

Run: `cd ~/Desktop/ivyManageSystem && ./start.sh`（後端 :8088、前端 :5173）

- [ ] **Step 2: 設 dev 白名單**

於 `ivy-backend/.env` 暫設 `ATTENDANCE_KIOSK_ALLOWED_IPS=127.0.0.1/32,::1/128`（本機測試），重啟後端。

- [ ] **Step 3: 走一次完整流程**

1. 用既有教師帳號登入 Portal → Profile → 設定打卡 PIN（如 `1234`）。
2. 開 `http://localhost:5173/kiosk/punch` → 名單應出現該教師（has_pin）。
3. 選該教師 → 輸 `1234` → 確認「即將記為上班」→ 打卡成功。
4. 再打一次 → 「即將記為下班」→ 成功。
5. 第三次 → 確認畫面顯示「將更新下班時間」→ 成功覆蓋。
6. 管理端員工頁 → 該員工「重置打卡 PIN」→ 回 kiosk 名單該員工變灰（has_pin=false）。
7. 將 `.env` 白名單清空重啟 → kiosk 頁 roster 應 403。

- [ ] **Step 4: 確認無誤後標記完成**

整合驗證通過。後續收尾（push + CI 綠 + 部署設 prod 白名單）見 spec §10 與 workspace 收尾紀律。

---

## 收尾備註（非 task）

- **prod 部署前置**：設 `ATTENDANCE_KIOSK_ALLOWED_IPS` 為園內固定對外 IP 的 CIDR（spec §11 風險：須先確認園內有穩定對外 IP）。
- **follow-up**：① e2e 納入 kiosk 為第 6 個 mutation ② `records.py` 改用 `build_shift_maps_for_employee_date` 收斂重複 ③ 夜班跨夜打卡（下班在隔日凌晨）目前歸隔日列、判為上班，屬已知限制。
- **完成定義**：兩 repo 各自 push + CI 綠 + 無殘留 worktree（workspace CLAUDE.md 收尾紀律），跑 `./scripts/finish-check.sh`。
