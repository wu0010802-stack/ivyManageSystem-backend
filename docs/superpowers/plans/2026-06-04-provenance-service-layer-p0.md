# Provenance 服務層 P0（attendance 參考切片）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可重用的 `DerivedValue` provenance 介面與第一個參考 provider（考勤扣款），讓前端能對單一格子下鑽到「算式摘要 + 逐筆原始紀錄 + 跳轉連結」，且**不改動任何年終金標準數字**。

**Architecture:** Provider **包住既有 `services/year_end/auto_derive/attendance_deductions.derive_attendance_deductions` 取權威值**（value 由舊函式產出 → 保證零漂移），另外查逐筆 rows 組 `source_records`。介面 = Pydantic `DerivedValue` / `SourceRecord`（`schemas/provenance.py`）。一個 key→provider registry（`services/provenance/base.py`）供 API 泛型分派。新 router `GET /api/provenance/{key}?cycle_id=&employee_id=`。

**Tech Stack:** FastAPI、SQLAlchemy、Pydantic v2、pytest（in-memory SQLite，`test_db_session`→`base` fixture 慣例）、Decimal `ROUND_HALF_UP`。

**範圍邊界（YAGNI）：** 本計畫只做 **attendance 一個參考 provider + 介面 + 單筆下鑽 API**。其餘 provider（enrollment / activity / disciplinary / meeting / festival_diff / returning_rate）與 grid 批次串接是後續計畫（P0b、P2）。手動覆寫欄位（`is_override`/`override_meta`）只在 schema 預留，不在本計畫計算。

---

## File Structure

| 檔案 | 責任 |
|---|---|
| `schemas/provenance.py`（**Create**） | Pydantic `SourceRecord` / `DerivedValue` 介面（API 與 provider 共用回傳型別） |
| `services/provenance/__init__.py`（**Create**） | 套件出口（re-export provider 與 registry） |
| `services/provenance/attendance_provider.py`（**Create**） | 考勤扣款 provider：包既有 derive 取值 + 查逐筆組 source_records |
| `services/provenance/base.py`（**Create**） | `ProvenanceQuery` 入參 dataclass + key→provider registry（`resolve_provenance`） |
| `api/provenance/__init__.py`（**Create**） | `GET /api/provenance/{key}` 端點 |
| `main.py`（**Modify**） | 掛載 `provenance_router` |
| `tests/test_provenance_schema.py`（**Create**） | schema 序列化測試 |
| `tests/test_provenance_attendance_provider.py`（**Create**） | provider 值=引擎、Σ source_records 對帳、逐筆內容 |
| `tests/test_provenance_api.py`（**Create**） | 端點 200/404/400 + permission |

---

## Task 1: `DerivedValue` / `SourceRecord` 介面

**Files:**
- Create: `schemas/provenance.py`
- Test: `tests/test_provenance_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provenance_schema.py
"""provenance 介面 schema 測試。"""
from decimal import Decimal

from schemas.provenance import DerivedValue, SourceRecord


def test_source_record_minimal():
    sr = SourceRecord(
        date="2025-03-01",
        label="遲到",
        amount=Decimal("-50"),
        module="attendance",
        source_id=123,
    )
    assert sr.module == "attendance"
    assert sr.amount == Decimal("-50")


def test_derived_value_defaults_and_serialize():
    dv = DerivedValue(
        key="attendance_late",
        value=Decimal("-250.00"),
        formula_summary="遲到 5 次 × −50",
        breakdown={"late_count": 5},
        source_records=[
            SourceRecord(
                date="2025-03-01",
                label="遲到",
                amount=Decimal("-50"),
                module="attendance",
                source_id=1,
            )
        ],
        deep_link="/attendance?employee_id=7",
    )
    # override 欄位預留、預設關閉
    assert dv.is_override is False
    assert dv.override_meta is None
    dumped = dv.model_dump()
    assert dumped["key"] == "attendance_late"
    assert len(dumped["source_records"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provenance_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schemas.provenance'`

- [ ] **Step 3: Write minimal implementation**

```python
# schemas/provenance.py
"""provenance 介面：DerivedValue / SourceRecord。

每個「自動推導值」統一表達成 DerivedValue，供前端深度3 下鑽。
正確性保證（provider 測試）：_q2(Σ source_records.amount) == value。
is_override / override_meta 為手動覆寫預留欄（P2 才計算），此處只定義。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


class SourceRecord(BaseModel):
    """一筆原始來源紀錄（下鑽明細的一列）。"""

    date: str = Field(description="紀錄日期 ISO 字串，如 2025-03-01")
    label: str = Field(description="可讀標籤，如『遲到』『事假 8h』")
    amount: Decimal = Field(description="此筆對 value 的貢獻（罰則為負）")
    module: str = Field(description="來源模組 key，如 attendance/leave/meeting")
    source_id: Optional[int] = Field(default=None, description="來源資料列 PK")


class DerivedValue(BaseModel):
    """一個自動推導值 + 其完整 provenance。"""

    key: str = Field(description="推導項 key，如 attendance_late")
    value: Decimal = Field(description="權威值（與既有引擎一致，不可漂移）")
    formula_summary: str = Field(description="可讀算式摘要")
    breakdown: dict[str, Any] = Field(
        default_factory=dict, description="結構化組成（次數/單價/期間…）"
    )
    source_records: list[SourceRecord] = Field(
        default_factory=list, description="逐筆原始紀錄"
    )
    deep_link: Optional[str] = Field(
        default=None, description="跳轉來源模組的前端路由+filter"
    )
    is_override: bool = Field(default=False, description="是否被手動覆寫（P2）")
    override_meta: Optional[dict[str, Any]] = Field(
        default=None, description="{原自動值, 覆寫者, 時間, 原因}（P2）"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_provenance_schema.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add schemas/provenance.py tests/test_provenance_schema.py
git commit -m "feat(provenance): add DerivedValue/SourceRecord 介面 schema"
```

---

## Task 2: attendance provider（包既有引擎取值 + 逐筆 source_records）

**Files:**
- Create: `services/provenance/__init__.py`
- Create: `services/provenance/attendance_provider.py`
- Test: `tests/test_provenance_attendance_provider.py`

設計重點：
- `value` **直接取自** `attendance_deductions.derive_attendance_deductions(...)`（權威、零漂移）。
- `source_records` 由本 provider **新查逐筆 rows** 組成（既有函式只 COUNT）。
- 對帳保證：`_q2(Σ source_records.amount) == value`（late/meeting 無除法→嚴格相等；leave 用原始未進位 amount→distributive 後 `_q2` 嚴格相等）。
- 費率/期間取自既有 `derive_attendance_deductions` 回傳的 `calc_meta`（不重算、不 import 私有費率邏輯）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provenance_attendance_provider.py
"""考勤扣款 provider provenance 測試。

核心保證：
1. provenance value == 既有引擎 derive_attendance_deductions（零漂移）。
2. _q2(Σ source_records.amount) == value（逐筆對帳）。
3. 逐筆 source_records 內容正確（日期/標籤/金額/source_id）。
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from models.attendance import Attendance
from models.config import BonusConfig
from models.employee import Employee
from models.event import MeetingRecord
from models.leave import LeaveRecord
from models.year_end import YearEndCycle
from services.year_end.auto_derive import attendance_deductions as ad
from services.provenance.attendance_provider import derive_attendance_provenance

_Q2 = Decimal("0.01")


def _q2(x):
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def _mk_employee(db, code, name):
    emp = Employee(
        employee_id=code,
        name=name,
        id_number=f"A{code[-9:].rjust(9, '0')}",
        hire_date=date(2023, 8, 1),
        is_active=True,
    )
    db.add(emp)
    db.flush()
    return emp


def _mk_cycle(db, academic_year=114):
    cycle = YearEndCycle(
        academic_year=academic_year,
        start_date=date(academic_year + 1911, 8, 1),
        end_date=date(academic_year + 1912, 7, 31),
        bonus_calc_date=date(academic_year + 1912, 1, 15),
    )
    db.add(cycle)
    db.flush()
    return cycle


def _mk_config(db):
    cfg = BonusConfig(
        config_year=114,
        is_active=True,
        late_deduction_per_time=50,
        missing_punch_deduction_per_time=50,
        personal_leave_deduction_per_day=500,
        sick_leave_deduction_per_day=500,
        meeting_absence_penalty=100,
    )
    db.add(cfg)
    db.flush()
    return cfg


@pytest.fixture
def base(test_db_session):
    db = test_db_session
    cycle = _mk_cycle(db, 114)  # 期間 = 2025/1/1 ~ 2025/12/31
    _mk_config(db)
    emp = _mk_employee(db, "E_PROV_01", "測試員工")
    db.commit()
    return {"db": db, "cycle": cycle, "emp": emp}


def test_late_source_records_and_value(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    for i in range(5):
        a = Attendance(
            employee_id=emp.id, attendance_date=date(2025, 3, i + 1), is_late=True
        )
        db.add(a)
    db.commit()

    result = derive_attendance_provenance(db, cycle, emp)
    dv = result["attendance_late"]

    # value 與既有引擎一致（零漂移）
    assert dv.value == ad.derive_attendance_deductions(db, cycle, emp).late
    assert dv.value == Decimal("-250.00")
    # 逐筆對帳
    assert len(dv.source_records) == 5
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value
    assert all(sr.module == "attendance" and sr.amount == Decimal("-50")
               for sr in dv.source_records)
    assert dv.source_records[0].source_id is not None
    assert "遲到" in dv.formula_summary


def test_missing_punch_in_late_key(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    db.add(Attendance(employee_id=emp.id, attendance_date=date(2025, 4, 1),
                      is_missing_punch_in=True, is_missing_punch_out=True))
    db.commit()

    dv = derive_attendance_provenance(db, cycle, emp)["attendance_late"]
    assert dv.value == Decimal("-100.00")  # 2 次 × -50
    assert len(dv.source_records) == 2  # 上班 + 下班各一筆
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value


def test_personal_leave_reconciles(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    db.add(LeaveRecord(employee_id=emp.id, leave_type="personal",
                       start_date=date(2025, 5, 1), end_date=date(2025, 5, 2),
                       leave_hours=16, status="approved"))  # 2 天 × -500
    db.commit()

    dv = derive_attendance_provenance(db, cycle, emp)["personal_leave"]
    assert dv.value == ad.derive_attendance_deductions(db, cycle, emp).personal_leave
    assert dv.value == Decimal("-1000.00")
    assert len(dv.source_records) == 1
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value


def test_meeting_absence_reconciles(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    db.add(MeetingRecord(employee_id=emp.id, meeting_date=date(2025, 6, 1),
                         attended=False))
    db.commit()

    dv = derive_attendance_provenance(db, cycle, emp)["meeting_absence"]
    assert dv.value == Decimal("-100.00")
    assert len(dv.source_records) == 1
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value


def test_no_records_zero_no_error(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    result = derive_attendance_provenance(db, cycle, emp)
    for key in ("attendance_late", "personal_leave", "sick_leave", "meeting_absence"):
        assert result[key].value == Decimal("0.00")
        assert result[key].source_records == []
        assert "無紀錄" in result[key].formula_summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provenance_attendance_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.provenance'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/provenance/__init__.py
"""provenance 服務層：自動推導值的 DerivedValue + 逐筆來源。"""
from services.provenance.attendance_provider import derive_attendance_provenance

__all__ = ["derive_attendance_provenance"]
```

```python
# services/provenance/attendance_provider.py
"""考勤扣款 provenance provider。

value 取自既有 attendance_deductions.derive_attendance_deductions（權威、零漂移），
本 provider 另查逐筆 rows 組 source_records，並組 DerivedValue。

對帳保證：_q2(Σ source_records.amount) == value
  - 遲到/未打卡/會議：每筆 = 定額 → Σ 嚴格相等。
  - 事假/病假：每筆 amount = 原始未進位 -(hours/8 × rate)，Σ 後 _q2 == value
    （引擎是 sum(hours)/8×rate 再 _q2，distributive 後相等）。
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.attendance import Attendance
from models.event import MeetingRecord
from models.leave import LeaveRecord
from models.year_end import YearEndCycle
from schemas.provenance import DerivedValue, SourceRecord
from services.year_end.auto_derive.attendance_deductions import (
    derive_attendance_deductions,
)

_Q2 = Decimal("0.01")
_HOURS_PER_DAY = Decimal("8")


def _q2(x) -> Decimal:
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def _late_records(db: Session, emp_id: int, start: date, end: date,
                  late_rate: Decimal, miss_rate: Decimal):
    # 遲到用 late_rate、未打卡用 miss_rate（兩費率可不同 → 保 Σ == 引擎 value）
    out: list[SourceRecord] = []
    rows = db.execute(
        select(
            Attendance.id,
            Attendance.attendance_date,
            Attendance.is_late,
            Attendance.is_missing_punch_in,
            Attendance.is_missing_punch_out,
        ).where(
            Attendance.employee_id == emp_id,
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        )
    ).all()
    for r in rows:
        if r.is_late:
            out.append(SourceRecord(date=r.attendance_date.isoformat(), label="遲到",
                                    amount=-late_rate, module="attendance", source_id=r.id))
        if r.is_missing_punch_in:
            out.append(SourceRecord(date=r.attendance_date.isoformat(),
                                    label="未打卡(上班)", amount=-miss_rate,
                                    module="attendance", source_id=r.id))
        if r.is_missing_punch_out:
            out.append(SourceRecord(date=r.attendance_date.isoformat(),
                                    label="未打卡(下班)", amount=-miss_rate,
                                    module="attendance", source_id=r.id))
    return out


def _leave_records(db, emp_id, leave_type, label, start, end, rate):
    out: list[SourceRecord] = []
    rows = db.execute(
        select(LeaveRecord.id, LeaveRecord.start_date, LeaveRecord.leave_hours).where(
            LeaveRecord.employee_id == emp_id,
            LeaveRecord.leave_type == leave_type,
            LeaveRecord.status == "approved",
            LeaveRecord.start_date >= start,
            LeaveRecord.start_date <= end,
        )
    ).all()
    for r in rows:
        hours = Decimal(str(r.leave_hours or 0))
        raw = -(hours / _HOURS_PER_DAY * rate)  # 未進位，保 Σ 後 _q2 == value
        out.append(SourceRecord(date=r.start_date.isoformat(),
                                label=f"{label} {hours}h", amount=raw,
                                module="leave", source_id=r.id))
    return out


def _meeting_records(db, emp_id, start, end, penalty):
    out: list[SourceRecord] = []
    rows = db.execute(
        select(MeetingRecord.id, MeetingRecord.meeting_date).where(
            MeetingRecord.employee_id == emp_id,
            MeetingRecord.attended.is_(False),
            MeetingRecord.meeting_date >= start,
            MeetingRecord.meeting_date <= end,
        )
    ).all()
    for r in rows:
        out.append(SourceRecord(date=r.meeting_date.isoformat(), label="會議缺席",
                                amount=-penalty, module="meeting", source_id=r.id))
    return out


def _dv(key, value, summary, breakdown, records, deep_link):
    if not records:
        summary = "無紀錄"
    return DerivedValue(key=key, value=value, formula_summary=summary,
                        breakdown=breakdown, source_records=records,
                        deep_link=deep_link)


def derive_attendance_provenance(
    db: Session, cycle: YearEndCycle, emp
) -> dict[str, DerivedValue]:
    """回傳 {key -> DerivedValue}：attendance_late / personal_leave / sick_leave /
    meeting_absence。value 來自既有引擎（零漂移），source_records 為逐筆。"""
    base = derive_attendance_deductions(db, cycle, emp)
    m = base.calc_meta
    start = date.fromisoformat(m["period_start"])
    end = date.fromisoformat(m["period_end"])
    late_rate = Decimal(str(m["late_rate"]))
    miss_rate = Decimal(str(m["missing_punch_rate"]))
    personal_rate = Decimal(str(m["personal_rate"]))
    sick_rate = Decimal(str(m["sick_rate"]))
    meeting_penalty = Decimal(str(m["meeting_penalty"]))
    dl = f"/attendance?employee_id={emp.id}&start={start.isoformat()}&end={end.isoformat()}"
    leave_dl = f"/leaves?employee_id={emp.id}&start={start.isoformat()}&end={end.isoformat()}"

    late_recs = _late_records(db, emp.id, start, end, late_rate, miss_rate)
    personal_recs = _leave_records(db, emp.id, "personal", "事假", start, end, personal_rate)
    sick_recs = _leave_records(db, emp.id, "sick", "病假", start, end, sick_rate)
    meeting_recs = _meeting_records(db, emp.id, start, end, meeting_penalty)

    return {
        "attendance_late": _dv(
            "attendance_late", base.late,
            f"遲到 {m['late_count']} 次 × −{late_rate} + 未打卡 {m['missing_punch_count']} 次 × −{miss_rate} · {m['period_start']}~{m['period_end']}",
            {"late_count": m["late_count"], "missing_punch_count": m["missing_punch_count"],
             "late_rate": m["late_rate"], "missing_punch_rate": m["missing_punch_rate"]},
            late_recs, dl),
        "personal_leave": _dv(
            "personal_leave", base.personal_leave,
            f"事假 {m['personal_days']} 天 × −{personal_rate}", 
            {"personal_days": m["personal_days"], "personal_rate": m["personal_rate"]},
            personal_recs, leave_dl),
        "sick_leave": _dv(
            "sick_leave", base.sick_leave,
            f"病假 {m['sick_days']} 天 × −{sick_rate}",
            {"sick_days": m["sick_days"], "sick_rate": m["sick_rate"]},
            sick_recs, leave_dl),
        "meeting_absence": _dv(
            "meeting_absence", base.meeting,
            f"會議缺席 {m['meeting_absence_count']} 次 × −{meeting_penalty}",
            {"meeting_absence_count": m["meeting_absence_count"]}, meeting_recs, dl),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_provenance_attendance_provider.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add services/provenance/__init__.py services/provenance/attendance_provider.py tests/test_provenance_attendance_provider.py
git commit -m "feat(provenance): add attendance provider（包既有引擎取值+逐筆 source_records）"
```

---

## Task 3: key→provider registry（`resolve_provenance`）

**Files:**
- Create: `services/provenance/base.py`
- Modify: `services/provenance/__init__.py`（re-export `resolve_provenance`）
- Test: `tests/test_provenance_attendance_provider.py`（追加 registry 測試）

- [ ] **Step 1: Write the failing test（追加到既有測試檔末尾）**

```python
# tests/test_provenance_attendance_provider.py 末尾追加
from services.provenance.base import resolve_provenance, KNOWN_KEYS


def test_registry_resolves_attendance_keys(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    db.add(Attendance(employee_id=emp.id, attendance_date=date(2025, 3, 1),
                      is_late=True))
    db.commit()
    dv = resolve_provenance(db, cycle, emp, "attendance_late")
    assert dv.value == Decimal("-50.00")
    assert "attendance_late" in KNOWN_KEYS


def test_registry_unknown_key_raises(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    with pytest.raises(KeyError):
        resolve_provenance(db, cycle, emp, "no_such_key")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provenance_attendance_provider.py -k registry -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.provenance.base'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/provenance/base.py
"""provenance registry：key → DerivedValue 的泛型分派。

新增 provider 時：在對應 provider 模組產 {key->DerivedValue} 的函式，
於 _PROVIDER_GROUPS 註冊其『群組函式』即可（一次算同模組多 key，避免重複查）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models.year_end import YearEndCycle
from schemas.provenance import DerivedValue
from services.provenance.attendance_provider import derive_attendance_provenance

# 群組函式：(db, cycle, emp) -> dict[key, DerivedValue]
_PROVIDER_GROUPS = [derive_attendance_provenance]

# key → 群組函式
_KEY_TO_GROUP = {}
KNOWN_KEYS: set[str] = set()


def _build_index():
    # 用一個極簡空 stub 探不出 key（需 db）→ 改為靜態宣告各群組的 key。
    pass


# 各群組宣告自己產出的 key（靜態，免 db 即可建索引）
_GROUP_KEYS = {
    derive_attendance_provenance: (
        "attendance_late",
        "personal_leave",
        "sick_leave",
        "meeting_absence",
    ),
}
for _fn, _keys in _GROUP_KEYS.items():
    for _k in _keys:
        _KEY_TO_GROUP[_k] = _fn
        KNOWN_KEYS.add(_k)


def resolve_provenance(
    db: Session, cycle: YearEndCycle, emp, key: str
) -> DerivedValue:
    """依 key 分派到對應 provider 群組，回傳該 key 的 DerivedValue。

    未知 key → KeyError。"""
    if key not in _KEY_TO_GROUP:
        raise KeyError(f"unknown provenance key: {key}")
    group_fn = _KEY_TO_GROUP[key]
    return group_fn(db, cycle, emp)[key]
```

```python
# services/provenance/__init__.py  ← 覆寫為
"""provenance 服務層：自動推導值的 DerivedValue + 逐筆來源。"""
from services.provenance.attendance_provider import derive_attendance_provenance
from services.provenance.base import KNOWN_KEYS, resolve_provenance

__all__ = ["derive_attendance_provenance", "resolve_provenance", "KNOWN_KEYS"]
```

> 註：移除 `_build_index()` 這個無用 stub（上面實作已用 `_GROUP_KEYS` 靜態索引取代）。實作時直接不要寫 `_build_index`。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_provenance_attendance_provider.py -k registry -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add services/provenance/base.py services/provenance/__init__.py tests/test_provenance_attendance_provider.py
git commit -m "feat(provenance): add key→provider registry resolve_provenance"
```

---

## Task 4: 下鑽 API 端點 `GET /api/provenance/{key}`

**Files:**
- Create: `api/provenance/__init__.py`
- Modify: `main.py`（掛 router）
- Test: `tests/test_provenance_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_provenance_api.py
"""provenance 下鑽 API 端點測試。"""
from datetime import date
from decimal import Decimal

import pytest

from models.attendance import Attendance
from models.config import BonusConfig
from models.employee import Employee
from models.year_end import YearEndCycle


@pytest.fixture
def seeded(test_db_session):
    db = test_db_session
    cycle = YearEndCycle(academic_year=114, start_date=date(2025, 8, 1),
                         end_date=date(2026, 7, 31), bonus_calc_date=date(2026, 1, 15))
    db.add(cycle)
    db.add(BonusConfig(config_year=114, is_active=True,
                       late_deduction_per_time=50, missing_punch_deduction_per_time=50,
                       personal_leave_deduction_per_day=500,
                       sick_leave_deduction_per_day=500, meeting_absence_penalty=100))
    emp = Employee(employee_id="E_API_01", name="API員工",
                   id_number="A000000001", hire_date=date(2023, 8, 1), is_active=True)
    db.add(emp)
    db.flush()
    db.add(Attendance(employee_id=emp.id, attendance_date=date(2025, 3, 1),
                      is_late=True))
    db.commit()
    return {"db": db, "cycle": cycle, "emp": emp}


def test_get_provenance_ok(admin_client, seeded):
    cycle, emp = seeded["cycle"], seeded["emp"]
    r = admin_client.get(
        f"/api/provenance/attendance_late?cycle_id={cycle.id}&employee_id={emp.id}"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["key"] == "attendance_late"
    assert Decimal(str(body["value"])) == Decimal("-50")
    assert len(body["source_records"]) == 1
    assert body["source_records"][0]["module"] == "attendance"


def test_get_provenance_unknown_key_400(admin_client, seeded):
    cycle, emp = seeded["cycle"], seeded["emp"]
    r = admin_client.get(
        f"/api/provenance/bogus?cycle_id={cycle.id}&employee_id={emp.id}"
    )
    assert r.status_code == 400


def test_get_provenance_missing_cycle_404(admin_client, seeded):
    emp = seeded["emp"]
    r = admin_client.get(
        f"/api/provenance/attendance_late?cycle_id=999999&employee_id={emp.id}"
    )
    assert r.status_code == 404
```

> 註：`admin_client` 是既有 conftest fixture（已認證、帶 YEAR_END_READ 權限的 TestClient）。實作 Step 3 前先 `grep -n "def admin_client" tests/conftest.py` 確認名稱；若專案用別名（如 `auth_client` / `client_admin`）改用之。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provenance_api.py -v`
Expected: FAIL（404/Not Found，因 router 尚未掛載）

- [ ] **Step 3: Write minimal implementation**

```python
# api/provenance/__init__.py
"""api/provenance — 自動推導值下鑽（provenance 深度3）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.employee import Employee
from models.year_end import YearEndCycle
from schemas.provenance import DerivedValue
from services.provenance import resolve_provenance
from utils.auth import require_permission
from utils.permissions import Permission

provenance_router = APIRouter(prefix="/api/provenance", tags=["provenance"])


@provenance_router.get("/{key}", response_model=DerivedValue)
def get_provenance(
    key: str,
    cycle_id: int = Query(...),
    employee_id: int = Query(...),
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    """回傳單一 key 的 DerivedValue（含逐筆 source_records）。"""
    cycle = session.get(YearEndCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "cycle 不存在")
    emp = session.get(Employee, employee_id)
    if emp is None:
        raise HTTPException(404, "employee 不存在")
    try:
        return resolve_provenance(session, cycle, emp, key)
    except KeyError:
        raise HTTPException(400, f"未知的 provenance key: {key}")
```

在 `main.py` 掛載（找到既有 `year_end_router` 的 `include_router` 區塊，照同一風格加一行）：

```python
# main.py — 與其他 include_router 並列
from api.provenance import provenance_router  # 置於既有 import 區
app.include_router(provenance_router)         # 置於既有 include_router 區
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_provenance_api.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add api/provenance/__init__.py main.py tests/test_provenance_api.py
git commit -m "feat(provenance): add GET /api/provenance/{key} 下鑽端點"
```

---

## Task 5: 回歸 gate — 確認年終金標準零漂移

**Files:**
- Test: 既有 `tests/test_year_end_auto_derive_attendance.py` / `tests/test_year_end_engine.py`（**不改**，僅執行）

本計畫刻意「value 取自既有引擎」，故必須證明既有年終測試完全不受影響。

- [ ] **Step 1: 跑既有年終全套件**

Run: `pytest tests/test_year_end_auto_derive_attendance.py tests/test_year_end_engine.py tests/test_year_end_settlement_builder.py -v`
Expected: 全 PASS（與本計畫前相同；0 新增 fail）

- [ ] **Step 2: 跑本計畫新增三測試檔**

Run: `pytest tests/test_provenance_schema.py tests/test_provenance_attendance_provider.py tests/test_provenance_api.py -v`
Expected: 全 PASS

- [ ] **Step 3: （無 code 變更，免 commit）**

若上述有任何 fail → 回對應 Task 修正，不得放行。

---

## Self-Review（writing-plans 完成後自查）

- **Spec coverage**：本計畫對應 spec §5.1（DerivedValue 介面 Task 1）、§5.2 provider 包既有引擎 + provider/registry（Task 2/3）、§5.2 明細 API（Task 4）、§8「Σ source_records == value」測試（Task 2/5）。spec §5.2 batch provider、§5.4 簽核/快照、§5.5 override、§6 前端、§4 考核對齊 → **明確排除於 P0 first slice**（屬 P0b/P1/P2，見「範圍邊界」）。無遺漏未標註的 spec 要求。
- **Placeholder scan**：各 step 皆含完整可執行 code 與指令；無 TBD/TODO。唯二「需現場確認」處已具體標註：`admin_client` fixture 名稱（Task 4 註）、`main.py` include_router 位置（照既有風格）。
- **Type consistency**：`DerivedValue` / `SourceRecord` 欄位（key/value/formula_summary/breakdown/source_records/deep_link/is_override/override_meta）跨 Task 1→2→3→4 一致；`resolve_provenance(db, cycle, emp, key)` 簽名 Task 3 定義、Task 4 使用一致；provider 回傳 `dict[str, DerivedValue]` 一致。
- **DRY/零漂移**：value 單一來源 = `derive_attendance_deductions`，未複製費率/期間邏輯。

---

## 後續計畫（不在本 P0 first slice）
- **P0b**：其餘 provider（enrollment / activity / disciplinary / meeting / festival_diff / returning_rate / semester_dividend）依同 pattern + 各自 `_GROUP_KEYS` 註冊；batch 版供 grid。
- **P2**：grid/抽屜前端、`payout_roster_snapshot`、override（is_override/override_meta）。
- **P1**：考核對齊 Excel（獨立計畫）。
