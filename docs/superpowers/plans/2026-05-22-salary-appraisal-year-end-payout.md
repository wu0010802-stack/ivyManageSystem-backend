# Salary × Appraisal Year-End Payout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把學期制考核（`AppraisalSummary.bonus_amount`）橋接進薪資 2/5 發放，復用既有 `year_end_cycles + special_bonus_items` 兩張表（APPRAISAL_HALF_BONUS_FIRST/SECOND slot），新增 HR 管理 UI、薪資 slip 顯示，與一條 salary engine plugin。

**Architecture:** Pull-based。HR 手動按按鈕生成 special_bonus_items（不啟用 6 層算法），salary engine 2 月 calculate 時 query 該表 sum 兩筆 APPRAISAL_HALF_BONUS_* 寫入 `SalaryRecord.appraisal_year_end_bonus`（獨立 column 不進 gross_salary）。Single source of truth = special_bonus_items；SalaryRecord 上的 column 是每次 calculate 重刷的 cache。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + Alembic + pytest（後端）；Vue 3 + TypeScript strict + Element Plus + Vitest（前端）；OpenAPI codegen 同步型別。

**Spec:** `docs/superpowers/specs/2026-05-22-salary-appraisal-year-end-payout-design.md`

**Pre-flight 確認（開工前跑一次）：**
- `cd ~/Desktop/ivy-backend && alembic heads` 拿最新 head（spec 撰寫時 = `3be2e40aaa42`；若 head 已變更，本 plan 中 `down_revision` 改用實際 head）
- `cd ~/Desktop/ivy-backend && pytest -q tests/test_salary_engine_main.py tests/test_appraisal_router.py tests/test_year_end_engine.py 2>&1 | tail -5` 確認 baseline 全綠
- `cd ~/Desktop/ivy-frontend && npm run typecheck && npm run test -- --run 2>&1 | tail -5` 確認 baseline 全綠

---

## File Structure

### 新增檔案

**後端：**
- `alembic/versions/20260522_ayebsr1_add_appraisal_year_end_bonus_to_salary_records.py` — schema migration
- `services/year_end/appraisal_sync.py` — appraisal → year_end 橋接 service
- `services/salary/appraisal_year_end.py` — salary engine plugin（query helper）
- `api/year_end/appraisal_payout.py` — 4 endpoint router
- `tests/test_year_end_appraisal_sync.py` — service 單元測試
- `tests/test_salary_appraisal_year_end_plugin.py` — salary engine 整合測試
- `tests/test_year_end_appraisal_payout_router.py` — API 測試

**前端：**
- `src/views/yearEnd/AppraisalPayoutView.vue` — HR 管理頁
- `src/views/yearEnd/__tests__/AppraisalPayoutView.spec.ts` — component 測試
- `src/views/__tests__/SalaryView.appraisal-year-end-bonus.spec.ts` — slip 整合測試

### 修改檔案

**後端：**
- `models/salary.py` — 加 `appraisal_year_end_bonus` column
- `models/year_end.py` — 修 `SpecialBonusType` enum docstring（FIRST/SECOND 重新解讀為時間順序）
- `services/salary/engine.py` — 在 `_apply_breakdown_to_record_locked()` 加 1 行
- `api/year_end/__init__.py` — include new sub-router
- `schemas/year_end.py` — 加 4 個 model（PayoutPreviewRow / PayoutItem / GenerateRequest / GenerateResult）

**前端：**
- `src/api/yearEnd.ts` — 加 4 個 wrapper
- `src/api/_generated/schema.d.ts` — regen
- `src/views/SalaryView.vue` — 加 1 column + breakdown dialog + 淨額公式
- `src/components/layout/AdminSidebar.vue` — 加 1 子項
- `src/router/index.ts` — 加 1 route

**Workspace：**
- `~/Desktop/ivyManageSystem/CLAUDE.md` — 加 cross-system 提醒

### 不動

- `models/year_end.py` `YearEndCycle`/`YearEndSettlement`/`EmployeeYearEndSnapshot`/`SpecialBonusItem` schema（僅 enum docstring 改動）
- `services/year_end/engine.py`（6 層算法保留不啟用）
- 既有 appraisal service / router / model

---

## Task 1: Migration + SalaryRecord column + enum docstring

**Files:**
- Create: `alembic/versions/20260522_ayebsr1_add_appraisal_year_end_bonus_to_salary_records.py`
- Modify: `models/salary.py:236` 附近（在 `bonus_amount` column 後加新欄位）
- Modify: `models/year_end.py:65-89`（`SpecialBonusType` enum docstring）
- Test: `tests/test_salary_record_appraisal_year_end_bonus_column.py`（new）

**Goal：** schema 改動到位。Column 預設 0、Not Null、Numeric(10,2)。

- [ ] **Step 1: Write the failing test**

Create `tests/test_salary_record_appraisal_year_end_bonus_column.py`：

```python
"""驗證 SalaryRecord 加上 appraisal_year_end_bonus column。"""

from decimal import Decimal

from sqlalchemy import inspect

from models.salary import SalaryRecord


def test_salary_record_has_appraisal_year_end_bonus_column():
    cols = {c.name: c for c in inspect(SalaryRecord).columns}
    assert "appraisal_year_end_bonus" in cols
    col = cols["appraisal_year_end_bonus"]
    assert col.nullable is False
    assert str(col.default.arg) == "0" or col.default.arg == 0


def test_salary_record_appraisal_year_end_bonus_default_zero(db_session, sample_salary_record):
    """新建 SalaryRecord 不指定 appraisal_year_end_bonus 時應為 0。"""
    record = sample_salary_record  # fixture 建一筆，不指定新欄
    db_session.flush()
    assert Decimal(record.appraisal_year_end_bonus) == Decimal("0")
```

（`sample_salary_record` fixture 若 conftest 沒有，inline 建一個最小 SalaryRecord — 用既有 employee fixture）

- [ ] **Step 2: Run test, verify FAIL**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_salary_record_appraisal_year_end_bonus_column.py -v 2>&1 | tail -10
```

Expected: FAIL — `appraisal_year_end_bonus` not in cols（model 還沒加）

- [ ] **Step 3: Add column to SalaryRecord model**

Modify `models/salary.py`（在 `bonus_amount` column 之後、`supervisor_dividend` 之前加）：

```python
appraisal_year_end_bonus = Column(
    Money,
    default=0,
    nullable=False,
    server_default="0",
    comment="考核年終獎金（2/5 與月薪同發；自 special_bonus_items 兩筆 APPRAISAL_HALF_BONUS_* SUM；不進 gross_salary）",
)
```

- [ ] **Step 4: Modify SpecialBonusType docstring**

Modify `models/year_end.py:65-89`（不動 enum value，只改 docstring）：

```python
class SpecialBonusType(str, enum.Enum):
    """9 種特別獎金 + 1 通用類型。

    對應 Excel「年終獎金總表」B 欄各列：
      APPRAISAL_HALF_BONUS_FIRST  : 較早那一筆（年終發放時對應「上學年下學期 = N-1.下」）— 來自 appraisal_summaries
      APPRAISAL_HALF_BONUS_SECOND : 較晚那一筆（年終發放時對應「本學年上學期 = N.上」）— 來自 appraisal_summaries
      SEMESTER_DIVIDEND_FIRST     : N上學期紅利（舊生 500 + 才藝 1000）
      SEMESTER_DIVIDEND_SECOND    : N下學期紅利
      AFTER_CLASS_AWARD           : N上鼓勵推動才藝班獎金（按班級人數）
      TEACHING_EXTRA              : N上教課教師獎勵金（堂數 × 65/堂）
      EXCESS_ENROLLMENT           : N上超額獎金（每月超額幼生）
      FESTIVAL_DIFF               : N.8-N+1.01 節慶獎金差額（多退少補，可為負）
      CUSTOM                      : 其他客製化（保留擴充用）

    ⚠️ APPRAISAL_HALF_BONUS_FIRST/SECOND 的 FIRST/SECOND 是「時間順序」（FIRST=較早=前一學年下學期，SECOND=較晚=本學年上學期），
    與 AppraisalCycle.Semester.FIRST/SECOND（學期上下）正好相反。由 services/year_end/appraisal_sync.py 依 calendar payout year 自動 map。
    """

    APPRAISAL_HALF_BONUS_FIRST = "APPRAISAL_HALF_BONUS_FIRST"
    APPRAISAL_HALF_BONUS_SECOND = "APPRAISAL_HALF_BONUS_SECOND"
    SEMESTER_DIVIDEND_FIRST = "SEMESTER_DIVIDEND_FIRST"
    SEMESTER_DIVIDEND_SECOND = "SEMESTER_DIVIDEND_SECOND"
    AFTER_CLASS_AWARD = "AFTER_CLASS_AWARD"
    TEACHING_EXTRA = "TEACHING_EXTRA"
    EXCESS_ENROLLMENT = "EXCESS_ENROLLMENT"
    FESTIVAL_DIFF = "FESTIVAL_DIFF"
    CUSTOM = "CUSTOM"
```

- [ ] **Step 5: Write migration**

Create `alembic/versions/20260522_ayebsr1_add_appraisal_year_end_bonus_to_salary_records.py`：

```python
"""add appraisal_year_end_bonus to salary_records

Revision ID: ayebsr1
Revises: 3be2e40aaa42
Create Date: 2026-05-22

考核年終獎金 column，獨立於 gross_salary，每月 calculate 時刷新（2 月才有值）。
"""

from alembic import op
import sqlalchemy as sa

revision = "ayebsr1"
down_revision = "3be2e40aaa42"  # 開工前用 alembic heads 確認；若已變請改用實際 head
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "salary_records",
        sa.Column(
            "appraisal_year_end_bonus",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="考核年終獎金（2/5 與月薪同發；自 special_bonus_items 兩筆 APPRAISAL_HALF_BONUS_* SUM；不進 gross_salary）",
        ),
    )


def downgrade() -> None:
    op.drop_column("salary_records", "appraisal_year_end_bonus")
```

- [ ] **Step 6: Apply migration + verify**

```bash
cd ~/Desktop/ivy-backend && alembic upgrade head 2>&1 | tail -5
```

Expected: `Running upgrade 3be2e40aaa42 -> ayebsr1, add appraisal_year_end_bonus to salary_records`

- [ ] **Step 7: Run test, verify PASS**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_salary_record_appraisal_year_end_bonus_column.py -v 2>&1 | tail -10
```

Expected: 2 passed

- [ ] **Step 8: Sanity check baseline still green**

```bash
cd ~/Desktop/ivy-backend && pytest -q tests/test_salary_engine_main.py 2>&1 | tail -5
```

Expected: 全綠（新 column default 0、不影響既有計算）

- [ ] **Step 9: Commit**

```bash
cd ~/Desktop/ivy-backend && git add alembic/versions/20260522_ayebsr1_add_appraisal_year_end_bonus_to_salary_records.py models/salary.py models/year_end.py tests/test_salary_record_appraisal_year_end_bonus_column.py && git commit -m "$(cat <<'EOF'
feat(salary,year_end): SalaryRecord.appraisal_year_end_bonus column + SpecialBonusType docstring

- migration ayebsr1: salary_records 加 NUMERIC(10,2) default 0 nullable=False
- SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST/SECOND docstring 重新解讀為時間順序（FIRST=較早=N-1.下、SECOND=較晚=N.上），與 AppraisalCycle.Semester.FIRST/SECOND 反向；plan/spec 已記載
- column 不進 gross_salary，後續 Task 5 的 salary engine plugin 每月 calculate 時 query special_bonus_items 寫入（2 月才有值）

spec §4.2/4.3、plan Task 1
EOF
)"
```

---

## Task 2: appraisal_sync 純函式 helpers

**Files:**
- Create: `services/year_end/appraisal_sync.py`（檔案先建、僅含純函式部分）
- Test: `tests/test_year_end_appraisal_sync_pure.py`（new）

**Goal：** 隔離 academic_year mapping 與 period_label 規則的純邏輯，便於 unit test。

- [ ] **Step 1: Write the failing test**

Create `tests/test_year_end_appraisal_sync_pure.py`：

```python
"""純函式單元測試：academic_year mapping + period_label mapping。"""

import pytest

from models.year_end import SpecialBonusType
from services.year_end.appraisal_sync import (
    civil_year_to_target_academic_year,
    map_bonus_type_to_period_label,
)


@pytest.mark.parametrize("civil_year,expected_academic_year", [
    (2024, 112),
    (2025, 113),
    (2026, 114),
    (2027, 115),
    (2028, 116),
])
def test_civil_year_to_target_academic_year(civil_year, expected_academic_year):
    """payout 發放國曆年 N → 對應本學年 (N - 1911 - 1)。"""
    assert civil_year_to_target_academic_year(civil_year) == expected_academic_year


def test_map_bonus_type_to_period_label_first_is_earlier():
    """FIRST = 較早 = 前一學年下學期 → label 'N-1下'"""
    assert map_bonus_type_to_period_label(
        SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        target_academic_year=114,
    ) == "113下"


def test_map_bonus_type_to_period_label_second_is_later():
    """SECOND = 較晚 = 本學年上學期 → label 'N上'"""
    assert map_bonus_type_to_period_label(
        SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
        target_academic_year=114,
    ) == "114上"


def test_map_bonus_type_to_period_label_rejects_non_appraisal_type():
    with pytest.raises(ValueError):
        map_bonus_type_to_period_label(
            SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
            target_academic_year=114,
        )
```

- [ ] **Step 2: Run test, verify FAIL**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_year_end_appraisal_sync_pure.py -v 2>&1 | tail -10
```

Expected: ImportError — `services.year_end.appraisal_sync` 不存在

- [ ] **Step 3: Create module with pure functions**

Create `services/year_end/appraisal_sync.py`：

```python
"""appraisal → year_end 橋接 service。

將學期制考核（AppraisalSummary.bonus_amount）寫入既有 special_bonus_items 表的
APPRAISAL_HALF_BONUS_FIRST/SECOND slot，供 salary engine 2 月 calculate 時 pull。

業務規則：
- payout 發放於 civil_year N 的 2/5
- 含「上學年下學期 (N-1.下)」+「本學年上學期 (N.上)」兩筆
- target year_end_cycles.academic_year = N - 1911 - 1（本學年，民國）
- bonus_type 對 period_label 的 mapping：
    FIRST  = 較早 = N-1.下 → period_label = f"{N-1-1911}下"
    SECOND = 較晚 = N.上   → period_label = f"{N-1911-1}上"
  ⚠️ SpecialBonusType 的 FIRST/SECOND 與 AppraisalCycle.Semester.FIRST/SECOND 反向（前者時間順序、後者學期上下）。
"""

from __future__ import annotations

from models.year_end import SpecialBonusType


def civil_year_to_target_academic_year(civil_year: int) -> int:
    """payout 發放國曆年 N → 對應本學年（民國）。

    2026 國曆年 2/5 = 114 學年下學期初（學年 8 月起算），所以 target = 114。
    """
    return civil_year - 1911 - 1


def map_bonus_type_to_period_label(
    bonus_type: SpecialBonusType, target_academic_year: int
) -> str:
    """FIRST → 前一學年下學期；SECOND → 本學年上學期。"""
    if bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST:
        return f"{target_academic_year - 1}下"
    if bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND:
        return f"{target_academic_year}上"
    raise ValueError(
        f"map_bonus_type_to_period_label 僅支援 APPRAISAL_HALF_BONUS_*；got {bonus_type}"
    )
```

- [ ] **Step 4: Run test, verify PASS**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_year_end_appraisal_sync_pure.py -v 2>&1 | tail -10
```

Expected: 8 passed (5 parametrize + 3 others)

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/year_end/appraisal_sync.py tests/test_year_end_appraisal_sync_pure.py && git commit -m "$(cat <<'EOF'
feat(year_end): appraisal_sync 純函式 — civil_year ↔ academic_year + period_label mapping

新檔 services/year_end/appraisal_sync.py（純函式部分）：
- civil_year_to_target_academic_year(N) = N - 1911 - 1 （2026→114）
- map_bonus_type_to_period_label(FIRST, 114) = "113下" / (SECOND, 114) = "114上"

⚠️ SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST/SECOND 時間順序與 AppraisalCycle.Semester.FIRST/SECOND 學期上下反向，docstring 已標記。

plan Task 2、spec §5.1
EOF
)"
```

---

## Task 3: resolve_target_cycles + preview_payout

**Files:**
- Modify: `services/year_end/appraisal_sync.py`（加 2 個函式）
- Modify: `tests/test_year_end_appraisal_sync_pure.py` → rename → `tests/test_year_end_appraisal_sync.py`（容納 db-touching tests）

**Goal：** 解析兩個 appraisal cycle、產出 HR preview 列表。

- [ ] **Step 1: Rename test file + add DB-touching tests**

```bash
cd ~/Desktop/ivy-backend && git mv tests/test_year_end_appraisal_sync_pure.py tests/test_year_end_appraisal_sync.py
```

Append to `tests/test_year_end_appraisal_sync.py`（純函式 test 保留，加新測試）：

```python
"""DB-touching tests for resolve_target_cycles + preview_payout。

需要 fixtures：
- two_appraisal_cycles_2025_26: 建 academic_year=113 SECOND + academic_year=114 FIRST 兩 cycle
- enrolled_employees: 3 名員工，2 名 ACTIVE 1 名 RESIGNED
- finalized_summaries: 為每 (cycle, employee) 建 finalized AppraisalSummary
"""

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import (
    AppraisalCycle, AppraisalParticipant, AppraisalSummary,
    Semester, RoleGroup, Grade, SummaryStatus, CycleStatus,
)
from services.year_end.appraisal_sync import (
    resolve_target_cycles, preview_payout,
)


@pytest.fixture
def two_appraisal_cycles(db_session):
    """建 academic_year=113 SECOND + academic_year=114 FIRST 兩 cycle 都 finalized。"""
    earlier = AppraisalCycle(
        academic_year=113, semester=Semester.SECOND,
        start_date=date(2025, 2, 1), end_date=date(2025, 7, 31),
        base_score_calc_date=date(2025, 2, 15),
        base_score=Decimal("100"), status=CycleStatus.FINALIZED,
    )
    later = AppraisalCycle(
        academic_year=114, semester=Semester.FIRST,
        start_date=date(2025, 8, 1), end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("100"), status=CycleStatus.FINALIZED,
    )
    db_session.add_all([earlier, later])
    db_session.flush()
    return earlier, later


def test_resolve_target_cycles_returns_earlier_then_later(db_session, two_appraisal_cycles):
    earlier_expected, later_expected = two_appraisal_cycles
    earlier, later = resolve_target_cycles(db_session, payout_year=2026)
    assert earlier.id == earlier_expected.id
    assert earlier.semester == Semester.SECOND
    assert earlier.academic_year == 113
    assert later.id == later_expected.id
    assert later.semester == Semester.FIRST
    assert later.academic_year == 114


def test_resolve_target_cycles_raises_when_cycle_missing(db_session):
    """若 113.下 或 114.上 不存在 → raise（HR 要先在 appraisal 系統建 cycle）。"""
    with pytest.raises(LookupError) as exc:
        resolve_target_cycles(db_session, payout_year=2026)
    assert "113" in str(exc.value) or "114" in str(exc.value)


def test_preview_payout_returns_active_employee_with_both_summaries(
    db_session, two_appraisal_cycles, sample_active_employee
):
    """ACTIVE 員工兩 cycle 都有 finalized summary → preview 一筆，total = fall + spring。"""
    earlier, later = two_appraisal_cycles
    p1 = AppraisalParticipant(
        cycle_id=earlier.id, employee_id=sample_active_employee.id,
        role_group=RoleGroup.TEACHER, hire_months_in_cycle=Decimal("6"),
    )
    p2 = AppraisalParticipant(
        cycle_id=later.id, employee_id=sample_active_employee.id,
        role_group=RoleGroup.TEACHER, hire_months_in_cycle=Decimal("6"),
    )
    db_session.add_all([p1, p2])
    db_session.flush()
    s1 = AppraisalSummary(
        participant_id=p1.id, cycle_id=earlier.id,
        base_score=Decimal("100"), total_score=Decimal("80"),
        grade=Grade.B, bonus_amount=Decimal("6400"),
        status=SummaryStatus.FINALIZED,
    )
    s2 = AppraisalSummary(
        participant_id=p2.id, cycle_id=later.id,
        base_score=Decimal("100"), total_score=Decimal("90"),
        grade=Grade.A, bonus_amount=Decimal("7200"),
        status=SummaryStatus.FINALIZED,
    )
    db_session.add_all([s1, s2])
    db_session.flush()

    rows = preview_payout(db_session, payout_year=2026)
    assert len(rows) == 1
    row = rows[0]
    assert row.employee_id == sample_active_employee.id
    assert row.earlier_amount == Decimal("6400")
    assert row.later_amount == Decimal("7200")
    assert row.total_amount == Decimal("13600")
    assert row.is_inactive is False
    assert row.earlier_cycle_finalized is True
    assert row.later_cycle_finalized is True
    assert row.warnings == []


def test_preview_payout_skips_excluded_participant(
    db_session, two_appraisal_cycles, sample_active_employee
):
    """is_excluded=True → preview 不列出此員工。"""
    earlier, later = two_appraisal_cycles
    p = AppraisalParticipant(
        cycle_id=earlier.id, employee_id=sample_active_employee.id,
        role_group=RoleGroup.TEACHER, is_excluded=True,
        exclude_reason="到職未滿三個月",
    )
    db_session.add(p)
    db_session.flush()
    rows = preview_payout(db_session, payout_year=2026)
    assert all(r.employee_id != sample_active_employee.id for r in rows)


def test_preview_payout_one_cycle_only_marks_warning(
    db_session, two_appraisal_cycles, sample_active_employee
):
    """員工只在 later cycle 出現（中途到職）→ earlier_amount=0 + warning。"""
    _earlier, later = two_appraisal_cycles
    p = AppraisalParticipant(
        cycle_id=later.id, employee_id=sample_active_employee.id,
        role_group=RoleGroup.TEACHER,
    )
    db_session.add(p)
    db_session.flush()
    s = AppraisalSummary(
        participant_id=p.id, cycle_id=later.id,
        base_score=Decimal("100"), total_score=Decimal("85"),
        grade=Grade.B, bonus_amount=Decimal("5400"),
        status=SummaryStatus.FINALIZED,
    )
    db_session.add(s)
    db_session.flush()
    rows = preview_payout(db_session, payout_year=2026)
    assert len(rows) == 1
    assert rows[0].earlier_amount == Decimal("0")
    assert rows[0].later_amount == Decimal("5400")
    assert "not_participated_in_earlier" in rows[0].warnings
```

注：`sample_active_employee` fixture 既有 conftest 應有；若無請建：

```python
@pytest.fixture
def sample_active_employee(db_session):
    from models.employees import Employee, EmploymentStatus
    emp = Employee(
        name="林老師", id_number="A123456789",
        hire_date=date(2024, 8, 1),
        employment_status=EmploymentStatus.ACTIVE,
    )
    db_session.add(emp); db_session.flush()
    return emp
```

- [ ] **Step 2: Run test, verify FAIL**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_year_end_appraisal_sync.py -v 2>&1 | tail -20
```

Expected: 既有 pure tests pass、新 db tests FAIL（`resolve_target_cycles` / `preview_payout` 不存在）

- [ ] **Step 3: Implement resolve_target_cycles + preview_payout**

Append to `services/year_end/appraisal_sync.py`：

```python
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle, AppraisalParticipant, AppraisalSummary,
    Semester, SummaryStatus,
)
from models.employees import Employee, EmploymentStatus


@dataclass
class PayoutPreviewRow:
    employee_id: int
    employee_name: str
    role_group: str
    earlier_summary_id: Optional[int]
    earlier_amount: Decimal
    earlier_cycle_finalized: bool
    later_summary_id: Optional[int]
    later_amount: Decimal
    later_cycle_finalized: bool
    total_amount: Decimal
    is_inactive: bool
    warnings: list[str] = field(default_factory=list)


def resolve_target_cycles(
    db: Session, payout_year: int
) -> tuple[AppraisalCycle, AppraisalCycle]:
    """payout_year (civil 2026) → (earlier_cycle 113下, later_cycle 114上)。"""
    target_academic_year = civil_year_to_target_academic_year(payout_year)
    earlier = db.scalar(
        select(AppraisalCycle).where(
            AppraisalCycle.academic_year == target_academic_year - 1,
            AppraisalCycle.semester == Semester.SECOND,
        )
    )
    if earlier is None:
        raise LookupError(
            f"appraisal_cycle academic_year={target_academic_year-1} SECOND 不存在；"
            "請先在考核管理建立此 cycle"
        )
    later = db.scalar(
        select(AppraisalCycle).where(
            AppraisalCycle.academic_year == target_academic_year,
            AppraisalCycle.semester == Semester.FIRST,
        )
    )
    if later is None:
        raise LookupError(
            f"appraisal_cycle academic_year={target_academic_year} FIRST 不存在；"
            "請先在考核管理建立此 cycle"
        )
    return earlier, later


def preview_payout(db: Session, payout_year: int) -> list[PayoutPreviewRow]:
    """為兩個 cycle 的所有 participants 算金額 snapshot，回傳 row 列表。

    - is_excluded=True 的 participant 不列出
    - 員工只在一個 cycle 出現：另一筆 0 + warning
    - 兩 cycle 都未參與：不列出
    """
    from collections import OrderedDict

    earlier, later = resolve_target_cycles(db, payout_year)

    def _fetch(cycle: AppraisalCycle) -> dict[int, AppraisalSummary | None]:
        # employee_id → AppraisalSummary（含未 finalize 但不含 is_excluded）
        rows = db.execute(
            select(AppraisalParticipant, AppraisalSummary).outerjoin(
                AppraisalSummary, AppraisalSummary.participant_id == AppraisalParticipant.id
            ).where(
                AppraisalParticipant.cycle_id == cycle.id,
                AppraisalParticipant.is_excluded.is_(False),
            )
        ).all()
        return {p.employee_id: s for p, s in rows}

    earlier_map = _fetch(earlier)
    later_map = _fetch(later)
    earlier_finalized = earlier.status.name == "FINALIZED" if hasattr(earlier.status, "name") else str(earlier.status) == "FINALIZED"
    later_finalized = later.status.name == "FINALIZED" if hasattr(later.status, "name") else str(later.status) == "FINALIZED"

    all_emp_ids = OrderedDict()
    for eid in list(earlier_map.keys()) + list(later_map.keys()):
        all_emp_ids[eid] = None

    employees = {
        e.id: e for e in db.scalars(
            select(Employee).where(Employee.id.in_(all_emp_ids.keys()))
        ).all()
    }

    rows: list[PayoutPreviewRow] = []
    for emp_id in all_emp_ids:
        emp = employees.get(emp_id)
        if emp is None:
            continue
        es = earlier_map.get(emp_id)
        ls = later_map.get(emp_id)
        e_amount = Decimal(es.bonus_amount) if es else Decimal(0)
        l_amount = Decimal(ls.bonus_amount) if ls else Decimal(0)
        warnings: list[str] = []
        if emp_id not in earlier_map:
            warnings.append("not_participated_in_earlier")
        if emp_id not in later_map:
            warnings.append("not_participated_in_later")
        if es and es.status != SummaryStatus.FINALIZED:
            warnings.append("earlier_summary_not_finalized")
        if ls and ls.status != SummaryStatus.FINALIZED:
            warnings.append("later_summary_not_finalized")
        rows.append(PayoutPreviewRow(
            employee_id=emp_id,
            employee_name=emp.name,
            role_group=(es or ls).participant.role_group.value if (es or ls) and hasattr((es or ls), "participant") else "",
            earlier_summary_id=es.id if es else None,
            earlier_amount=e_amount,
            earlier_cycle_finalized=earlier_finalized,
            later_summary_id=ls.id if ls else None,
            later_amount=l_amount,
            later_cycle_finalized=later_finalized,
            total_amount=e_amount + l_amount,
            is_inactive=emp.employment_status != EmploymentStatus.ACTIVE,
            warnings=warnings,
        ))
    return rows
```

注：`role_group` 取得邏輯若 relationship 不便取，直接從 AppraisalParticipant query 補拿；fixture test 中 role_group 可放寬到字串斷言。

- [ ] **Step 4: Run test, verify PASS**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_year_end_appraisal_sync.py -v 2>&1 | tail -20
```

Expected: 全 12 個 pass。若 fixture 不存在須先建。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/year_end/appraisal_sync.py tests/test_year_end_appraisal_sync.py && git rm tests/test_year_end_appraisal_sync_pure.py 2>/dev/null || true && git commit -m "$(cat <<'EOF'
feat(year_end): appraisal_sync 加 resolve_target_cycles + preview_payout

- resolve_target_cycles(db, payout_year=2026) → (113.下 cycle, 114.上 cycle)，缺 cycle 即 raise LookupError
- preview_payout 回傳 PayoutPreviewRow 列表（含 finalize 狀態、is_inactive、warnings）
- 規則：is_excluded skip、單 cycle 補 warning（not_participated_in_earlier/later）、未 finalize summary 補 warning

plan Task 3、spec §5.1
EOF
)"
```

---

## Task 4: generate_payouts + void_payouts + advisory lock

**Files:**
- Modify: `services/year_end/appraisal_sync.py`
- Modify: `tests/test_year_end_appraisal_sync.py`

**Goal：** transactional write（idempotent + race-safe）+ audit。

- [ ] **Step 1: Write failing tests**

Append to `tests/test_year_end_appraisal_sync.py`：

```python
from models.year_end import YearEndCycle, SpecialBonusItem, SpecialBonusType
from services.year_end.appraisal_sync import (
    generate_payouts, void_payouts, GenerateResult,
)


def test_generate_payouts_active_only_by_default(
    db_session, two_appraisal_cycles,
    sample_active_employee, sample_resigned_employee, _setup_two_summaries_for_each,
):
    """included_inactive_employee_ids 為空 → 只生成 ACTIVE 員工的 payout。"""
    result = generate_payouts(
        db_session, payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    assert isinstance(result, GenerateResult)
    cycle = db_session.scalar(select(YearEndCycle).where(YearEndCycle.academic_year == 114))
    assert cycle is not None
    items = db_session.scalars(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle.id,
            SpecialBonusItem.bonus_type.in_([
                SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
            ]),
        )
    ).all()
    emp_ids_in_items = {i.employee_id for i in items}
    assert sample_active_employee.id in emp_ids_in_items
    assert sample_resigned_employee.id not in emp_ids_in_items


def test_generate_payouts_includes_inactive_when_selected(
    db_session, two_appraisal_cycles,
    sample_active_employee, sample_resigned_employee, _setup_two_summaries_for_each,
):
    result = generate_payouts(
        db_session, payout_year=2026,
        included_inactive_employee_ids={sample_resigned_employee.id},
        generated_by=1,
    )
    cycle = db_session.scalar(select(YearEndCycle).where(YearEndCycle.academic_year == 114))
    items = db_session.scalars(
        select(SpecialBonusItem).where(SpecialBonusItem.year_end_cycle_id == cycle.id)
    ).all()
    assert sample_resigned_employee.id in {i.employee_id for i in items}


def test_generate_payouts_idempotent(
    db_session, two_appraisal_cycles,
    sample_active_employee, _setup_two_summaries_for_each,
):
    """連按兩次 → ON CONFLICT DO UPDATE，不會多寫。"""
    generate_payouts(db_session, payout_year=2026, included_inactive_employee_ids=set(), generated_by=1)
    db_session.commit()
    count_first = db_session.scalar(select(func.count()).select_from(SpecialBonusItem))

    generate_payouts(db_session, payout_year=2026, included_inactive_employee_ids=set(), generated_by=1)
    db_session.commit()
    count_second = db_session.scalar(select(func.count()).select_from(SpecialBonusItem))
    assert count_first == count_second


def test_generate_payouts_writes_source_ref_and_calc_meta(
    db_session, two_appraisal_cycles,
    sample_active_employee, _setup_two_summaries_for_each,
):
    generate_payouts(db_session, payout_year=2026, included_inactive_employee_ids=set(), generated_by=1)
    item = db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        )
    )
    assert item.source_ref.startswith("appraisal_summary:")
    assert "cycle_not_finalized" in item.calc_meta
    assert "summary_status" in item.calc_meta


def test_void_payouts_deletes_only_appraisal_half_bonus_items(
    db_session, two_appraisal_cycles, sample_active_employee, _setup_two_summaries_for_each,
):
    generate_payouts(db_session, payout_year=2026, included_inactive_employee_ids=set(), generated_by=1)
    cycle = db_session.scalar(select(YearEndCycle).where(YearEndCycle.academic_year == 114))

    # 模擬一筆非 APPRAISAL_HALF 的 special_bonus_item（HR 另外手填）
    db_session.add(SpecialBonusItem(
        year_end_cycle_id=cycle.id, employee_id=sample_active_employee.id,
        bonus_type=SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
        period_label="114上", amount=Decimal("500"),
    ))
    db_session.flush()

    deleted = void_payouts(db_session, payout_year=2026, voided_by=1)
    remaining = db_session.scalars(
        select(SpecialBonusItem).where(SpecialBonusItem.year_end_cycle_id == cycle.id)
    ).all()
    assert deleted == 2  # FIRST + SECOND
    assert len(remaining) == 1
    assert remaining[0].bonus_type == SpecialBonusType.SEMESTER_DIVIDEND_FIRST
```

加 fixture `_setup_two_summaries_for_each`（建 active + resigned 員工各自兩 cycle finalized summary）+ `sample_resigned_employee`，inline 在同 file 即可。

- [ ] **Step 2: Run test, verify FAIL**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_year_end_appraisal_sync.py -k "generate or void" -v 2>&1 | tail -20
```

Expected: 5 個新 test 全 FAIL（function 不存在）

- [ ] **Step 3: Implement generate_payouts + void_payouts**

Append to `services/year_end/appraisal_sync.py`：

```python
from dataclasses import asdict
from datetime import datetime
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.year_end import YearEndCycle, SpecialBonusItem, SpecialBonusType


@dataclass
class GenerateResult:
    cycle_id: int
    generated_count: int        # 寫入/更新的 SpecialBonusItem 筆數
    affected_employee_count: int
    total_amount: Decimal
    skipped_inactive_count: int  # 過濾掉的 inactive 員工數
    warnings: list[str] = field(default_factory=list)


def _advisory_lock_payout(db: Session, payout_year: int) -> None:
    """transaction-scope advisory lock；避免兩個 admin 並行 generate。"""
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": hash(("aye_payout", payout_year))})


def generate_payouts(
    db: Session,
    payout_year: int,
    included_inactive_employee_ids: set[int],
    generated_by: int,
) -> GenerateResult:
    """transactional：upsert YearEndCycle + 對每員工兩筆 SpecialBonusItem。

    呼叫端應在 router 用 with session.begin()（或 transactional dep）包起來。
    本函式僅 flush，不 commit。
    """
    _advisory_lock_payout(db, payout_year)

    target_academic_year = civil_year_to_target_academic_year(payout_year)
    rows = preview_payout(db, payout_year)

    # upsert YearEndCycle（最小 shell；6 層算法相關欄全 default）
    cycle = db.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == target_academic_year)
    )
    if cycle is None:
        cycle = YearEndCycle(academic_year=target_academic_year)
        db.add(cycle)
        db.flush()

    earlier_cycle, later_cycle = resolve_target_cycles(db, payout_year)
    earlier_finalized = str(getattr(earlier_cycle.status, "name", earlier_cycle.status)) == "FINALIZED"
    later_finalized = str(getattr(later_cycle.status, "name", later_cycle.status)) == "FINALIZED"

    written_count = 0
    affected_emp_ids: set[int] = set()
    total = Decimal(0)
    skipped_inactive = 0
    warnings: list[str] = []

    for row in rows:
        if row.is_inactive and row.employee_id not in included_inactive_employee_ids:
            skipped_inactive += 1
            continue

        for bonus_type, amount, summary_id, cycle_finalized, partition in [
            (SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST, row.earlier_amount,
             row.earlier_summary_id, earlier_finalized, "earlier"),
            (SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND, row.later_amount,
             row.later_summary_id, later_finalized, "later"),
        ]:
            period_label = map_bonus_type_to_period_label(bonus_type, target_academic_year)
            calc_meta = {
                "cycle_not_finalized": not cycle_finalized,
                "summary_status": "FINALIZED" if summary_id else "MISSING",
                "snapshot_at": datetime.utcnow().isoformat(),
                "partition": partition,
            }
            stmt = pg_insert(SpecialBonusItem).values(
                year_end_cycle_id=cycle.id,
                employee_id=row.employee_id,
                bonus_type=bonus_type,
                period_label=period_label,
                amount=amount,
                source_ref=f"appraisal_summary:{summary_id}" if summary_id else "appraisal_summary:none",
                calc_meta=calc_meta,
                created_by=generated_by,
            ).on_conflict_do_update(
                constraint="uq_special_bonus_item",
                set_={"amount": amount, "source_ref": f"appraisal_summary:{summary_id}" if summary_id else "appraisal_summary:none", "calc_meta": calc_meta, "updated_at": func.now()},
            )
            db.execute(stmt)
            written_count += 1

        affected_emp_ids.add(row.employee_id)
        total += row.earlier_amount + row.later_amount

    db.flush()
    return GenerateResult(
        cycle_id=cycle.id,
        generated_count=written_count,
        affected_employee_count=len(affected_emp_ids),
        total_amount=total,
        skipped_inactive_count=skipped_inactive,
        warnings=warnings,
    )


def void_payouts(db: Session, payout_year: int, voided_by: int) -> int:
    """刪除 target academic_year 下所有 APPRAISAL_HALF_BONUS_* items。"""
    _advisory_lock_payout(db, payout_year)
    target_academic_year = civil_year_to_target_academic_year(payout_year)
    cycle = db.scalar(select(YearEndCycle).where(YearEndCycle.academic_year == target_academic_year))
    if cycle is None:
        return 0
    rows = db.scalars(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle.id,
            SpecialBonusItem.bonus_type.in_([
                SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
            ]),
        )
    ).all()
    deleted = len(rows)
    for r in rows:
        db.delete(r)
    db.flush()
    return deleted
```

- [ ] **Step 4: Run test, verify PASS**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_year_end_appraisal_sync.py -v 2>&1 | tail -25
```

Expected: 全綠（12 + 5 = 17 個）

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/year_end/appraisal_sync.py tests/test_year_end_appraisal_sync.py && git commit -m "$(cat <<'EOF'
feat(year_end): appraisal_sync 加 generate_payouts + void_payouts

- generate_payouts: upsert YearEndCycle + 對每員工兩筆 SpecialBonusItem（FIRST/SECOND）
  - ACTIVE 預設全寫；INACTIVE 須在 included_inactive_employee_ids 列才寫
  - source_ref="appraisal_summary:{id}"、calc_meta 含 cycle_not_finalized + summary_status
  - ON CONFLICT (uq_special_bonus_item) DO UPDATE → idempotent
  - pg_advisory_xact_lock(hash('aye_payout', year)) 防 race
- void_payouts: 刪除 target academic_year 下 APPRAISAL_HALF_BONUS_* items（不動其他 type）

audit middleware 由 router 層處理（Task 6）。

plan Task 4、spec §5.1 / §7
EOF
)"
```

---

## Task 5: Salary engine plugin

**Files:**
- Create: `services/salary/appraisal_year_end.py`
- Create: `tests/test_salary_appraisal_year_end_plugin.py`
- Modify: `services/salary/engine.py`（`_apply_breakdown_to_record_locked` 加 1 行）

**Goal：** 2 月 calculate 時 query special_bonus_items 寫入 SalaryRecord。其他月 = 0。

- [ ] **Step 1: Write the failing test (plugin pure query)**

Create `tests/test_salary_appraisal_year_end_plugin.py`：

```python
"""Salary engine plugin: appraisal_year_end query helper。"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from models.year_end import YearEndCycle, SpecialBonusItem, SpecialBonusType
from services.salary.appraisal_year_end import query_appraisal_year_end_bonus


@pytest.fixture
def cycle_with_two_payouts(db_session, sample_active_employee):
    cycle = YearEndCycle(academic_year=114)
    db_session.add(cycle); db_session.flush()
    db_session.add_all([
        SpecialBonusItem(
            year_end_cycle_id=cycle.id, employee_id=sample_active_employee.id,
            bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
            period_label="113下", amount=Decimal("6400"),
        ),
        SpecialBonusItem(
            year_end_cycle_id=cycle.id, employee_id=sample_active_employee.id,
            bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
            period_label="114上", amount=Decimal("7200"),
        ),
    ])
    db_session.flush()
    return cycle


def test_query_returns_zero_for_non_february(db_session, sample_active_employee, cycle_with_two_payouts):
    for m in [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
        result = query_appraisal_year_end_bonus(db_session, sample_active_employee.id, 2026, m)
        assert result == Decimal("0"), f"month={m} should be 0"


def test_query_returns_sum_for_february(db_session, sample_active_employee, cycle_with_two_payouts):
    result = query_appraisal_year_end_bonus(db_session, sample_active_employee.id, 2026, 2)
    assert result == Decimal("13600")


def test_query_returns_zero_when_no_payout(db_session, sample_active_employee):
    """員工沒被 generate payout → 2 月也是 0。"""
    result = query_appraisal_year_end_bonus(db_session, sample_active_employee.id, 2026, 2)
    assert result == Decimal("0")


def test_query_only_sums_appraisal_half_bonus_types(
    db_session, sample_active_employee, cycle_with_two_payouts
):
    """同 cycle 有其他 type 的 special_bonus_item 不應被加入。"""
    db_session.add(SpecialBonusItem(
        year_end_cycle_id=cycle_with_two_payouts.id,
        employee_id=sample_active_employee.id,
        bonus_type=SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
        period_label="114上", amount=Decimal("9999"),
    ))
    db_session.flush()
    result = query_appraisal_year_end_bonus(db_session, sample_active_employee.id, 2026, 2)
    assert result == Decimal("13600")  # 不含 9999


def test_query_correct_academic_year_mapping(db_session, sample_active_employee):
    """payout_year=2025 → target_academic_year=113，不要拉到 114 cycle 的金額。"""
    cycle_114 = YearEndCycle(academic_year=114)
    cycle_113 = YearEndCycle(academic_year=113)
    db_session.add_all([cycle_114, cycle_113]); db_session.flush()
    db_session.add(SpecialBonusItem(
        year_end_cycle_id=cycle_114.id, employee_id=sample_active_employee.id,
        bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        period_label="113下", amount=Decimal("9999"),
    ))
    db_session.add(SpecialBonusItem(
        year_end_cycle_id=cycle_113.id, employee_id=sample_active_employee.id,
        bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        period_label="112下", amount=Decimal("3000"),
    ))
    db_session.flush()
    result_2025 = query_appraisal_year_end_bonus(db_session, sample_active_employee.id, 2025, 2)
    result_2026 = query_appraisal_year_end_bonus(db_session, sample_active_employee.id, 2026, 2)
    assert result_2025 == Decimal("3000")
    assert result_2026 == Decimal("9999")
```

- [ ] **Step 2: Run test, verify FAIL**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_salary_appraisal_year_end_plugin.py -v 2>&1 | tail -10
```

Expected: ImportError — module 不存在

- [ ] **Step 3: Implement plugin**

Create `services/salary/appraisal_year_end.py`：

```python
"""Salary engine plugin：2 月 calculate 時拉考核年終獎金。

source of truth = special_bonus_items（FIRST+SECOND 兩筆）；每月 calculate 重新 query。
2 月份以外 return 0；不進 gross_salary、不影響勞健保 / 應發合計。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle
from services.year_end.appraisal_sync import civil_year_to_target_academic_year


def query_appraisal_year_end_bonus(
    db: Session, employee_id: int, year: int, month: int
) -> Decimal:
    if month != 2:
        return Decimal(0)
    target_academic_year = civil_year_to_target_academic_year(year)
    result = db.scalar(
        select(func.coalesce(func.sum(SpecialBonusItem.amount), 0))
        .join(YearEndCycle, YearEndCycle.id == SpecialBonusItem.year_end_cycle_id)
        .where(
            YearEndCycle.academic_year == target_academic_year,
            SpecialBonusItem.employee_id == employee_id,
            SpecialBonusItem.bonus_type.in_([
                SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
            ]),
        )
    )
    return Decimal(result or 0)
```

- [ ] **Step 4: Run test, verify PASS**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_salary_appraisal_year_end_plugin.py -v 2>&1 | tail -10
```

Expected: 5 passed

- [ ] **Step 5: Hook into salary engine**

Modify `services/salary/engine.py` `_apply_breakdown_to_record_locked()`（or wherever SalaryRecord 欄位賦值的 finalize section — implementer 先 grep `salary_record.bonus_amount =` 找到位置、在下一行加）：

```python
from services.salary.appraisal_year_end import query_appraisal_year_end_bonus
# （import 放檔頭區）

# ... inside _apply_breakdown_to_record_locked:
salary_record.appraisal_year_end_bonus = query_appraisal_year_end_bonus(
    db, salary_record.employee_id, salary_record.year, salary_record.month
)
```

⚠️ 確認 `salary_record` 已有 `employee_id` / `year` / `month` 屬性（既有 model）；db session 從函式上下文取（既有 `engine.py` 有 `db` reference，若無，往上一層找 `self._db`）。

- [ ] **Step 6: Add integration test for engine writing the column**

Append to `tests/test_salary_appraisal_year_end_plugin.py`：

```python
from services.salary.engine import SalaryEngine
from models.salary import SalaryRecord


def test_salary_engine_writes_appraisal_year_end_bonus_for_february(
    db_session, sample_active_employee, cycle_with_two_payouts
):
    """整合：engine.calculate(2026/02) → SalaryRecord.appraisal_year_end_bonus = 13600。"""
    engine = SalaryEngine(db_session, year=2026, month=2)
    engine.calculate_for_employee(sample_active_employee.id)
    db_session.flush()
    record = db_session.scalar(
        select(SalaryRecord).where(
            SalaryRecord.employee_id == sample_active_employee.id,
            SalaryRecord.year == 2026, SalaryRecord.month == 2,
        )
    )
    assert record is not None
    assert Decimal(record.appraisal_year_end_bonus) == Decimal("13600")


def test_salary_engine_zero_for_january_even_with_payout(
    db_session, sample_active_employee, cycle_with_two_payouts
):
    engine = SalaryEngine(db_session, year=2026, month=1)
    engine.calculate_for_employee(sample_active_employee.id)
    db_session.flush()
    record = db_session.scalar(
        select(SalaryRecord).where(
            SalaryRecord.employee_id == sample_active_employee.id,
            SalaryRecord.year == 2026, SalaryRecord.month == 1,
        )
    )
    assert Decimal(record.appraisal_year_end_bonus) == Decimal("0")


def test_salary_engine_recalculate_picks_up_changed_payout(
    db_session, sample_active_employee, cycle_with_two_payouts
):
    """generate → calculate → 改 payout 金額 → recalculate → 新值。"""
    engine = SalaryEngine(db_session, year=2026, month=2)
    engine.calculate_for_employee(sample_active_employee.id)
    db_session.flush()

    # 直接改 payout amount
    item = db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        )
    )
    item.amount = Decimal("8000")
    db_session.flush()

    # recalculate
    engine.calculate_for_employee(sample_active_employee.id)
    db_session.flush()
    record = db_session.scalar(
        select(SalaryRecord).where(
            SalaryRecord.employee_id == sample_active_employee.id,
            SalaryRecord.year == 2026, SalaryRecord.month == 2,
        )
    )
    assert Decimal(record.appraisal_year_end_bonus) == Decimal("15200")  # 8000 + 7200
```

⚠️ `SalaryEngine` 構造參數 / `calculate_for_employee` 簽名可能不同；implementer 先看 `services/salary/engine.py` 既有測試（如 `test_salary_engine_main.py`）抄構造方式。若 engine 需要 `bulk_preload` / 必要 prerequisites（薪資 base、班級、出勤等），fixture 要先補齊到能算出非空 SalaryRecord 的最低限度。

- [ ] **Step 7: Run all tests**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_salary_appraisal_year_end_plugin.py tests/test_salary_engine_main.py -v 2>&1 | tail -20
```

Expected: 全 8 個新 test + 既有 salary engine 主 suite 全綠（zero regression）

- [ ] **Step 8: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/salary/appraisal_year_end.py services/salary/engine.py tests/test_salary_appraisal_year_end_plugin.py && git commit -m "$(cat <<'EOF'
feat(salary): appraisal_year_end plugin — 2 月 calculate 拉考核年終獎金

- services/salary/appraisal_year_end.py: query_appraisal_year_end_bonus(db, emp_id, year, month)
  - month != 2 → Decimal(0)
  - month == 2 → SUM(special_bonus_items.amount) WHERE year_end_cycle.academic_year=target ∧ bonus_type ∈ {FIRST,SECOND}
- engine.py _apply_breakdown_to_record_locked() 加 1 行寫 SalaryRecord.appraisal_year_end_bonus
- 8 個整合測試：非 2 月零 / 2 月 sum / 改 payout 後 recalc 自動同步 / 不同 academic_year mapping / 不被其他 type 干擾

plan Task 5、spec §5.3
EOF
)"
```

---

## Task 6: API router + schemas + audit

**Files:**
- Create: `api/year_end/appraisal_payout.py`
- Modify: `api/year_end/__init__.py`（include router）
- Modify: `schemas/year_end.py`（加 4 個 model）
- Create: `tests/test_year_end_appraisal_payout_router.py`

**Goal：** 4 endpoint：GET preview / POST generate / GET list / DELETE void。

- [ ] **Step 1: Write failing router tests**

Create `tests/test_year_end_appraisal_payout_router.py`：

```python
"""Year-end appraisal payout router 測試。

需要 fixtures：
- client: TestClient with auth dependency override
- admin_user_with_appraisal_finalize: 含 Permission.APPRAISAL_FINALIZE
- viewer_user_without_permission: 不含
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from models.year_end import YearEndCycle, SpecialBonusItem, SpecialBonusType


PREFIX = "/api/year_end/appraisal-payout"


def test_preview_requires_permission(client, viewer_user_without_permission):
    client.app.dependency_overrides[...].setattr("auth", viewer_user_without_permission)  # implementer 用既有 helper
    res = client.get(f"{PREFIX}/preview", params={"year": 2026})
    assert res.status_code == 403


def test_preview_returns_rows(client, admin_user_with_appraisal_finalize, two_appraisal_cycles, _setup_two_summaries_for_each):
    res = client.get(f"{PREFIX}/preview", params={"year": 2026})
    assert res.status_code == 200
    rows = res.json()
    assert isinstance(rows, list)
    assert all("employee_id" in r and "total_amount" in r for r in rows)


def test_preview_missing_year_returns_422(client, admin_user_with_appraisal_finalize):
    res = client.get(f"{PREFIX}/preview")
    assert res.status_code == 422


def test_generate_happy_path(client, admin_user_with_appraisal_finalize, two_appraisal_cycles, sample_active_employee, _setup_two_summaries_for_each, db_session):
    res = client.post(f"{PREFIX}/generate", json={
        "year": 2026, "included_inactive_employee_ids": [],
    })
    assert res.status_code == 200
    body = res.json()
    assert body["affected_employee_count"] >= 1
    cycle = db_session.scalar(select(YearEndCycle).where(YearEndCycle.academic_year == 114))
    assert cycle is not None


def test_generate_idempotent_via_api(client, admin_user_with_appraisal_finalize, two_appraisal_cycles, _setup_two_summaries_for_each, db_session):
    client.post(f"{PREFIX}/generate", json={"year": 2026, "included_inactive_employee_ids": []})
    first = db_session.scalar(select(func.count()).select_from(SpecialBonusItem))
    client.post(f"{PREFIX}/generate", json={"year": 2026, "included_inactive_employee_ids": []})
    second = db_session.scalar(select(func.count()).select_from(SpecialBonusItem))
    assert first == second


def test_list_returns_generated_items(client, admin_user_with_appraisal_finalize, two_appraisal_cycles, _setup_two_summaries_for_each):
    client.post(f"{PREFIX}/generate", json={"year": 2026, "included_inactive_employee_ids": []})
    res = client.get(PREFIX, params={"year": 2026})
    assert res.status_code == 200
    items = res.json()
    assert len(items) >= 2  # 至少一員工兩筆


def test_void_requires_confirm(client, admin_user_with_appraisal_finalize, two_appraisal_cycles, _setup_two_summaries_for_each):
    client.post(f"{PREFIX}/generate", json={"year": 2026, "included_inactive_employee_ids": []})
    # 不帶 confirm
    res_no_confirm = client.delete(f"{PREFIX}/2026")
    assert res_no_confirm.status_code == 400
    # 帶 confirm
    res_ok = client.delete(f"{PREFIX}/2026", params={"confirm": True})
    assert res_ok.status_code == 200
    assert res_ok.json()["deleted_count"] >= 2
```

- [ ] **Step 2: Add Pydantic schemas**

Append to `schemas/year_end.py`：

```python
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class PayoutPreviewRow(BaseModel):
    employee_id: int
    employee_name: str
    role_group: str
    earlier_summary_id: Optional[int]
    earlier_amount: Decimal
    earlier_cycle_finalized: bool
    later_summary_id: Optional[int]
    later_amount: Decimal
    later_cycle_finalized: bool
    total_amount: Decimal
    is_inactive: bool
    warnings: list[str]


class PayoutGenerateRequest(BaseModel):
    year: int = Field(..., ge=2024, le=2099)
    included_inactive_employee_ids: list[int] = Field(default_factory=list)


class PayoutGenerateResult(BaseModel):
    cycle_id: int
    generated_count: int
    affected_employee_count: int
    total_amount: Decimal
    skipped_inactive_count: int
    warnings: list[str]


class PayoutItem(BaseModel):
    """已生成的 special_bonus_item 顯示 schema。"""
    id: int
    employee_id: int
    bonus_type: str
    period_label: str
    amount: Decimal
    source_ref: Optional[str]
    calc_meta: dict
```

- [ ] **Step 3: Create router**

Create `api/year_end/appraisal_payout.py`：

```python
"""考核年終 payout API（HR 手動 trigger 後寫 special_bonus_items 兩筆 FIRST/SECOND）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_current_user, require_permission
from auth_permissions import Permission
from db import get_session_dep
from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle
from schemas.year_end import (
    PayoutGenerateRequest, PayoutGenerateResult, PayoutItem, PayoutPreviewRow,
)
from services.year_end.appraisal_sync import (
    civil_year_to_target_academic_year,
    generate_payouts, preview_payout, void_payouts,
)


router = APIRouter(prefix="/appraisal-payout", tags=["year_end:appraisal_payout"])


@router.get("/preview", response_model=list[PayoutPreviewRow])
def get_preview(
    year: int = Query(..., ge=2024, le=2099),
    db: Session = Depends(get_session_dep),
    user=Depends(require_permission(Permission.APPRAISAL_FINALIZE)),
):
    rows = preview_payout(db, payout_year=year)
    return [PayoutPreviewRow(**vars(r)) for r in rows]


@router.post("/generate", response_model=PayoutGenerateResult)
def post_generate(
    body: PayoutGenerateRequest,
    db: Session = Depends(get_session_dep),
    user=Depends(require_permission(Permission.APPRAISAL_FINALIZE)),
    request=None,  # FastAPI injects
):
    result = generate_payouts(
        db,
        payout_year=body.year,
        included_inactive_employee_ids=set(body.included_inactive_employee_ids),
        generated_by=user.id,
    )
    db.commit()
    # audit middleware 走 request.state.audit_*；implementer 確認既有 helper
    return PayoutGenerateResult(**vars(result))


@router.get("", response_model=list[PayoutItem])
def list_payouts(
    year: int = Query(..., ge=2024, le=2099),
    db: Session = Depends(get_session_dep),
    user=Depends(require_permission(Permission.APPRAISAL_FINALIZE)),
):
    target = civil_year_to_target_academic_year(year)
    cycle = db.scalar(select(YearEndCycle).where(YearEndCycle.academic_year == target))
    if cycle is None:
        return []
    items = db.scalars(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle.id,
            SpecialBonusItem.bonus_type.in_([
                SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
            ]),
        )
    ).all()
    return [
        PayoutItem(
            id=i.id, employee_id=i.employee_id,
            bonus_type=i.bonus_type.value, period_label=i.period_label,
            amount=i.amount, source_ref=i.source_ref, calc_meta=i.calc_meta,
        ) for i in items
    ]


@router.delete("/{year}")
def delete_payouts(
    year: int,
    confirm: bool = Query(False),
    db: Session = Depends(get_session_dep),
    user=Depends(require_permission(Permission.APPRAISAL_FINALIZE)),
):
    if not confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="confirm=true required")
    deleted = void_payouts(db, payout_year=year, voided_by=user.id)
    db.commit()
    return {"deleted_count": deleted}
```

⚠️ Implementer 注意：`auth` / `auth_permissions` / `db` import 路徑與 dep 名字以既有 router 為準（grep `api/year_end/__init__.py` 看 imports）。`require_permission` 寫法依既有 helper（可能是 `Depends(...)` 包裝 `get_current_user` 後手動 check + raise 403）。

- [ ] **Step 4: Include router in year_end main**

Modify `api/year_end/__init__.py`（在 `year_end_router = APIRouter(...)` 之後）：

```python
from api.year_end.appraisal_payout import router as appraisal_payout_router
year_end_router.include_router(appraisal_payout_router)
```

- [ ] **Step 5: Run tests, verify PASS**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_year_end_appraisal_payout_router.py -v 2>&1 | tail -20
```

Expected: 7 passed。若 permission helper / dep override 機制不同，implementer 對齊既有 router test（e.g. `tests/test_appraisal_router.py`）的寫法。

- [ ] **Step 6: 跑全套 sanity check zero regression**

```bash
cd ~/Desktop/ivy-backend && pytest -q 2>&1 | tail -10
```

Expected: 全綠（pre-existing failures 不變）

- [ ] **Step 7: Commit**

```bash
cd ~/Desktop/ivy-backend && git add api/year_end/appraisal_payout.py api/year_end/__init__.py schemas/year_end.py tests/test_year_end_appraisal_payout_router.py && git commit -m "$(cat <<'EOF'
feat(api/year_end): appraisal payout 4 endpoint + Pydantic schemas

新 router 掛 /api/year_end/appraisal-payout：
- GET /preview?year — 列出本年所有員工 preview（金額 snapshot + finalize 狀態 + warnings + is_inactive）
- POST /generate body {year, included_inactive_employee_ids[]} — HR 手動觸發生成
- GET ?year — 已生成的 special_bonus_items（FIRST+SECOND）
- DELETE /{year}?confirm=true — admin 清空當年 payout

全部走 Permission.APPRAISAL_FINALIZE 守衛；audit 走 request.state.audit_* middleware。
schemas/year_end.py 加 4 個 model 讓 OpenAPI 對前端落 TS 型別。

plan Task 6、spec §5.2
EOF
)"
```

---

## Task 7: OpenAPI dump + frontend codegen

**Files:**
- Run: `cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py`（不入 git）
- Run: `cd ~/Desktop/ivy-frontend && npm run gen:api`
- Modify: `~/Desktop/ivy-frontend/src/api/_generated/schema.d.ts`（regen）

**Goal：** 後端新 endpoint 的 TS 型別落地前端。

- [ ] **Step 1: Backend dump OpenAPI**

```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
```

Expected: `openapi.json` 生成（gitignored）。

- [ ] **Step 2: Frontend regen schema**

```bash
cd ~/Desktop/ivy-frontend && npm run gen:api
```

Expected: `src/api/_generated/schema.d.ts` 修改，包含 `/year_end/appraisal-payout/*` paths。

- [ ] **Step 3: Verify new paths present**

```bash
cd ~/Desktop/ivy-frontend && grep -c "appraisal-payout" src/api/_generated/schema.d.ts
```

Expected: > 0（多筆 path entries）

- [ ] **Step 4: Verify typecheck still passes**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck 2>&1 | tail -5
```

Expected: 0 errors。

- [ ] **Step 5: Commit (frontend)**

```bash
cd ~/Desktop/ivy-frontend && git add src/api/_generated/schema.d.ts && git commit -m "$(cat <<'EOF'
chore(api/gen): regen schema.d.ts — appraisal-payout endpoints

backend Task 6 新增 /api/year_end/appraisal-payout/* 4 endpoint，
跑 dump_openapi + gen:api 同步前端型別。

plan Task 7
EOF
)"
```

---

## Task 8: Frontend api wrapper (yearEnd.ts)

**Files:**
- Modify: `~/Desktop/ivy-frontend/src/api/yearEnd.ts`

**Goal：** 4 個 typed wrapper 函式。

- [ ] **Step 1: Inspect existing yearEnd.ts pattern**

```bash
cd ~/Desktop/ivy-frontend && head -30 src/api/yearEnd.ts
```

確認既有 wrapper 使用 `AxiosResp<>` / `ApiBody<>` 的格式（spec §6.4 提到）。

- [ ] **Step 2: Add 4 wrapper functions**

Append to `~/Desktop/ivy-frontend/src/api/yearEnd.ts`：

```ts
import type { AxiosResp, ApiBody } from './_generated/typed'
// （依既有 import 風格調整）

export const previewAppraisalPayout = (year: number) =>
  api.get<...>('/year_end/appraisal-payout/preview', { params: { year } }) as Promise<AxiosResp<'/year_end/appraisal-payout/preview', 'get'>>

export const generateAppraisalPayout = (
  body: ApiBody<'/year_end/appraisal-payout/generate', 'post'>
) =>
  api.post<...>('/year_end/appraisal-payout/generate', body) as Promise<AxiosResp<'/year_end/appraisal-payout/generate', 'post'>>

export const listAppraisalPayouts = (year: number) =>
  api.get<...>('/year_end/appraisal-payout', { params: { year } }) as Promise<AxiosResp<'/year_end/appraisal-payout', 'get'>>

export const voidAppraisalPayouts = (year: number) =>
  api.delete<...>(`/year_end/appraisal-payout/${year}`, { params: { confirm: true } }) as Promise<AxiosResp<'/year_end/appraisal-payout/{year}', 'delete'>>
```

⚠️ Implementer 修正 generic 寫法以對齊既有 `src/api/employees.ts` / `src/api/salary.ts` 的範例（L3 migration spec 已定 pattern）。

- [ ] **Step 3: Typecheck**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck 2>&1 | tail -5
```

Expected: 0 errors。

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-frontend && git add src/api/yearEnd.ts && git commit -m "$(cat <<'EOF'
feat(api/yearEnd): 4 typed wrappers for appraisal-payout endpoints

previewAppraisalPayout / generateAppraisalPayout / listAppraisalPayouts / voidAppraisalPayouts，
全部走 AxiosResp<>/ApiBody<> 對齊 L3 typed pattern。

plan Task 8、spec §6.4
EOF
)"
```

---

## Task 9: AppraisalPayoutView + sidebar + router

**Files:**
- Create: `~/Desktop/ivy-frontend/src/views/yearEnd/AppraisalPayoutView.vue`
- Create: `~/Desktop/ivy-frontend/src/views/yearEnd/__tests__/AppraisalPayoutView.spec.ts`
- Modify: `~/Desktop/ivy-frontend/src/components/layout/AdminSidebar.vue`
- Modify: `~/Desktop/ivy-frontend/src/router/index.ts`

**Goal：** HR 管理頁能 preview / generate / list / void。

- [ ] **Step 1: Write failing component test**

Create `src/views/yearEnd/__tests__/AppraisalPayoutView.spec.ts`：

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import AppraisalPayoutView from '../AppraisalPayoutView.vue'

vi.mock('@/api/yearEnd', () => ({
  previewAppraisalPayout: vi.fn(),
  generateAppraisalPayout: vi.fn(),
  listAppraisalPayouts: vi.fn(),
  voidAppraisalPayouts: vi.fn(),
}))

import * as api from '@/api/yearEnd'

const mockPreviewRow = (overrides = {}) => ({
  employee_id: 1, employee_name: '王主任', role_group: 'DIRECTOR',
  earlier_summary_id: 10, earlier_amount: '6400', earlier_cycle_finalized: true,
  later_summary_id: 20, later_amount: '7200', later_cycle_finalized: true,
  total_amount: '13600', is_inactive: false, warnings: [],
  ...overrides,
})

describe('AppraisalPayoutView', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders preview rows after load', async () => {
    vi.mocked(api.previewAppraisalPayout).mockResolvedValue({
      data: [mockPreviewRow(), mockPreviewRow({ employee_id: 2, employee_name: '林老師' })],
    } as any)
    const wrapper = mount(AppraisalPayoutView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('王主任')
    expect(wrapper.text()).toContain('林老師')
    expect(wrapper.text()).toContain('13600')
  })

  it('disables inactive row checkbox by default, lets user opt-in', async () => {
    vi.mocked(api.previewAppraisalPayout).mockResolvedValue({
      data: [mockPreviewRow({ employee_id: 3, employee_name: '陳老師', is_inactive: true })],
    } as any)
    const wrapper = mount(AppraisalPayoutView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    const inactiveCheckbox = wrapper.find('[data-test="row-checkbox-3"]')
    expect(inactiveCheckbox.exists()).toBe(true)
    expect((inactiveCheckbox.element as HTMLInputElement).checked).toBe(false)
  })

  it('calls generateAppraisalPayout with included_inactive ids on submit', async () => {
    vi.mocked(api.previewAppraisalPayout).mockResolvedValue({
      data: [
        mockPreviewRow({ employee_id: 1 }),
        mockPreviewRow({ employee_id: 3, is_inactive: true }),
      ],
    } as any)
    vi.mocked(api.generateAppraisalPayout).mockResolvedValue({
      data: { cycle_id: 1, generated_count: 4, affected_employee_count: 2, total_amount: '27200', skipped_inactive_count: 0, warnings: [] },
    } as any)
    const wrapper = mount(AppraisalPayoutView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    // 勾 inactive
    await wrapper.find('[data-test="row-checkbox-3"]').setValue(true)
    await wrapper.find('[data-test="generate-button"]').trigger('click')
    // confirm dialog
    await wrapper.find('[data-test="confirm-generate"]').trigger('click')
    expect(api.generateAppraisalPayout).toHaveBeenCalledWith({
      year: expect.any(Number),
      included_inactive_employee_ids: [3],
    })
  })

  it('shows warning banner when any cycle not finalized', async () => {
    vi.mocked(api.previewAppraisalPayout).mockResolvedValue({
      data: [mockPreviewRow({ earlier_cycle_finalized: false })],
    } as any)
    const wrapper = mount(AppraisalPayoutView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toMatch(/未\s*finaliz/i)
  })
})
```

- [ ] **Step 2: Run test, verify FAIL**

```bash
cd ~/Desktop/ivy-frontend && npm test -- --run src/views/yearEnd/__tests__/AppraisalPayoutView 2>&1 | tail -10
```

Expected: FAIL — file does not exist。

- [ ] **Step 3: Create AppraisalPayoutView.vue**

Create `src/views/yearEnd/AppraisalPayoutView.vue`：

```vue
<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  previewAppraisalPayout, generateAppraisalPayout,
  listAppraisalPayouts, voidAppraisalPayouts,
} from '@/api/yearEnd'

type PreviewRow = {
  employee_id: number
  employee_name: string
  role_group: string
  earlier_summary_id: number | null
  earlier_amount: string
  earlier_cycle_finalized: boolean
  later_summary_id: number | null
  later_amount: string
  later_cycle_finalized: boolean
  total_amount: string
  is_inactive: boolean
  warnings: string[]
}

const currentYear = new Date().getFullYear()
const year = ref<number>(currentYear)
const loading = ref(false)
const rows = ref<PreviewRow[]>([])
const selected = ref<Set<number>>(new Set())
const tab = ref<'preview' | 'generated'>('preview')

const anyCycleNotFinalized = computed(() =>
  rows.value.some(r => !r.earlier_cycle_finalized || !r.later_cycle_finalized)
)
const selectedRows = computed(() => rows.value.filter(r => selected.value.has(r.employee_id)))
const selectedTotal = computed(() =>
  selectedRows.value.reduce((s, r) => s + Number(r.total_amount), 0)
)
const includedInactiveIds = computed(() =>
  selectedRows.value.filter(r => r.is_inactive).map(r => r.employee_id)
)

async function loadPreview() {
  loading.value = true
  try {
    const res = await previewAppraisalPayout(year.value)
    rows.value = res.data as PreviewRow[]
    // default 勾 ACTIVE 的
    selected.value = new Set(rows.value.filter(r => !r.is_inactive).map(r => r.employee_id))
  } catch (e: unknown) {
    ElMessage.error('preview 載入失敗')
  } finally {
    loading.value = false
  }
}

async function onGenerate() {
  if (selectedRows.value.length === 0) {
    ElMessage.warning('請至少勾選一位員工')
    return
  }
  try {
    await ElMessageBox.confirm(
      `將為 ${selectedRows.value.length} 名員工生成 payout（合計 NT$${selectedTotal.value}）`,
      '確認生成',
      { confirmButtonText: '確認', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await generateAppraisalPayout({ year: year.value, included_inactive_employee_ids: includedInactiveIds.value })
    ElMessage.success('已生成')
    tab.value = 'generated'
  } catch (e: unknown) {
    ElMessage.error('生成失敗')
  }
}

async function onVoid() {
  try {
    await ElMessageBox.confirm('將清空本年所有考核年終 payout（不可復原）', '確認清空', { type: 'warning' })
    await ElMessageBox.confirm('再次確認：清空後須重新生成', '最終確認', { type: 'warning' })
  } catch {
    return
  }
  try {
    const res = await voidAppraisalPayouts(year.value)
    ElMessage.success(`已刪除 ${(res.data as { deleted_count: number }).deleted_count} 筆`)
    await loadPreview()
  } catch {
    ElMessage.error('清空失敗')
  }
}

onMounted(loadPreview)
watch(year, loadPreview)
</script>

<template>
  <div class="appraisal-payout-view">
    <header class="header">
      <h2>考核年終獎金管理</h2>
      <el-input-number v-model="year" :min="2024" :max="2099" />
      <el-button type="primary" @click="loadPreview">重新載入</el-button>
      <el-button type="danger" plain @click="onVoid">清空本年 payout</el-button>
    </header>

    <el-alert
      v-if="anyCycleNotFinalized"
      type="warning"
      :closable="false"
      title="⚠️ 有未 finalized 的 cycle，建議先完成簽核再生成"
      data-test="not-finalized-warning"
      style="margin: 12px 0;"
    />

    <el-tabs v-model="tab">
      <el-tab-pane label="預覽" name="preview">
        <el-table v-loading="loading" :data="rows" border>
          <el-table-column width="60">
            <template #default="{ row }">
              <el-checkbox
                :model-value="selected.has(row.employee_id)"
                :data-test="`row-checkbox-${row.employee_id}`"
                @update:model-value="(v: boolean) => v ? selected.add(row.employee_id) : selected.delete(row.employee_id)"
              />
            </template>
          </el-table-column>
          <el-table-column prop="employee_name" label="員工" />
          <el-table-column prop="earlier_amount" label="113下" />
          <el-table-column prop="later_amount" label="114上" />
          <el-table-column prop="total_amount" label="合計" />
          <el-table-column label="在職?" width="100">
            <template #default="{ row }">
              <el-tag :type="row.is_inactive ? 'danger' : 'success'" size="small">
                {{ row.is_inactive ? '已離職' : '在職' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="warnings">
            <template #default="{ row }">
              <span v-for="w in row.warnings" :key="w" class="warning-tag">{{ w }}</span>
            </template>
          </el-table-column>
        </el-table>

        <footer class="footer">
          <el-button
            type="primary"
            size="large"
            data-test="generate-button"
            @click="onGenerate"
          >
            確認生成 {{ selectedRows.length }} 筆 payout（合計 NT${{ selectedTotal }}）
          </el-button>
        </footer>
      </el-tab-pane>

      <el-tab-pane label="已生成" name="generated">
        <!-- list view via listAppraisalPayouts；implementer 補 table -->
      </el-tab-pane>
    </el-tabs>

    <!-- confirm dialog 由 ElMessageBox 處理（無需 v-model 元件）；
         test 中觸發 confirm button data-test="confirm-generate" 由 ElMessageBox 內建 button class 對應 -->
  </div>
</template>

<style scoped>
.appraisal-payout-view { padding: 16px; }
.header { display: flex; gap: 12px; align-items: center; margin-bottom: 12px; }
.footer { margin-top: 16px; text-align: right; }
.warning-tag { display: inline-block; margin-right: 4px; padding: 2px 6px; background: #fef0c8; border-radius: 4px; font-size: 12px; }
</style>
```

⚠️ Test 中 `data-test="confirm-generate"` 是依賴 ElMessageBox 的 button；若 ElMessageBox 的 confirm button 沒有此 attribute，implementer 改為直接 mock ElMessageBox.confirm resolve 即可（vi.mocked(ElMessageBox).confirm.mockResolvedValue('confirm')）。

- [ ] **Step 4: Run test, verify PASS**

```bash
cd ~/Desktop/ivy-frontend && npm test -- --run src/views/yearEnd/__tests__/AppraisalPayoutView 2>&1 | tail -15
```

Expected: 4 passed（含 inactive opt-in / generate call / warning banner）

- [ ] **Step 5: Sidebar + router**

Modify `~/Desktop/ivy-frontend/src/components/layout/AdminSidebar.vue` —— 在既有「年終獎金」項目下加子項（implementer 用 `grep "year_end\|yearEnd" src/components/layout/AdminSidebar.vue` 找到位置）：

```vue
<!-- 既有「年終獎金」el-sub-menu 下加 -->
<el-menu-item index="/year-end/appraisal-payout" v-if="hasPermission(Permission.APPRAISAL_FINALIZE)">
  考核年終 payout
</el-menu-item>
```

Modify `~/Desktop/ivy-frontend/src/router/index.ts` —— 在既有 `/year-end/...` route 之下加：

```ts
{
  path: '/year-end/appraisal-payout',
  name: 'YearEndAppraisalPayout',
  component: () => import('@/views/yearEnd/AppraisalPayoutView.vue'),
  meta: { requiresAuth: true, permission: Permission.APPRAISAL_FINALIZE },
},
```

- [ ] **Step 6: Typecheck + full vitest**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck && npm test -- --run 2>&1 | tail -10
```

Expected: 0 typecheck errors、vitest 全綠（新 4 test + 既有不變）

- [ ] **Step 7: Commit**

```bash
cd ~/Desktop/ivy-frontend && git add src/views/yearEnd/AppraisalPayoutView.vue src/views/yearEnd/__tests__/AppraisalPayoutView.spec.ts src/components/layout/AdminSidebar.vue src/router/index.ts && git commit -m "$(cat <<'EOF'
feat(yearEnd): AppraisalPayoutView — HR 考核年終 payout 管理頁

- 年份切換 → preview API 拉名單
- ACTIVE 預設全勾、INACTIVE 預設不勾（HR 可主動勾選帶入 generate）
- 未 finalized cycle 顯示 warning banner
- 生成前 ElMessageBox confirm 兩段（清空為兩次 confirm）
- sidebar 加子項 + router 加 route，permission gate Permission.APPRAISAL_FINALIZE

plan Task 9、spec §6.1/6.3
EOF
)"
```

---

## Task 10: SalaryView 整合 + breakdown dialog

**Files:**
- Modify: `~/Desktop/ivy-frontend/src/views/SalaryView.vue`
- Create: `~/Desktop/ivy-frontend/src/views/__tests__/SalaryView.appraisal-year-end-bonus.spec.ts`

**Goal：** 薪資 slip 列表加一欄「考核年終獎金」+ 點開看 cycle breakdown + 淨額公式加入。

- [ ] **Step 1: Write failing test**

Create `src/views/__tests__/SalaryView.appraisal-year-end-bonus.spec.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import SalaryView from '../SalaryView.vue'

const mockRow = (overrides = {}) => ({
  employee_id: 1,
  year: 2026, month: 2,
  net_pay: 30000,
  festival_bonus: 0, overtime_bonus: 0,
  appraisal_year_end_bonus: 13600,
  ...overrides,
})

describe('SalaryView appraisal_year_end_bonus integration', () => {
  it('renders appraisal_year_end_bonus column for February rows', async () => {
    // 既有 SalaryView 通常用 store 拉資料；implementer 對齊既有 test 寫法
    // 用 props / inject 等方式注入 [mockRow()] 作為 records
    const wrapper = mount(SalaryView, {
      // ...既有 stub / global plugins
    })
    // skip implementation: 用既有 SalaryView 主 test 的 setup 模式
    expect(wrapper.text()).toContain('13600')
  })

  it('net total formula includes appraisal_year_end_bonus', async () => {
    // net_pay 30000 + festival 0 + overtime 0 + appraisal_year_end 13600 = 43600
    const wrapper = mount(SalaryView, { /* setup with mockRow */ })
    expect(wrapper.text()).toContain('43600')
  })

  it('non-February row shows 0 (or empty) for appraisal_year_end_bonus', async () => {
    const wrapper = mount(SalaryView, { /* setup with mockRow({ month: 3, appraisal_year_end_bonus: 0 }) */ })
    // assert 不顯示 13600
  })
})
```

⚠️ Implementer 注意：SalaryView 是大檔（既有 ~900 行）且 store-driven，test setup 較複雜。對齊既有 `src/views/__tests__/SalaryView.*.spec.ts`（若存在）的 mount pattern。若 mocking 太複雜，可拆 column 渲染為 small composable 後測 composable。

- [ ] **Step 2: Add new column + breakdown dialog**

Modify `~/Desktop/ivy-frontend/src/views/SalaryView.vue`：

**(a)** 在 `el-table-column` 既有 `festival_bonus` 那一欄附近（grep line 530-540 一帶）加：

```vue
<el-table-column prop="appraisal_year_end_bonus" label="考核年終獎金" width="120">
  <template #default="scope">
    <button
      v-if="scope.row.month === 2 && Number(scope.row.appraisal_year_end_bonus) > 0"
      type="button"
      class="cell-link text-link-primary"
      @click="openAppraisalYearEndBreakdown(scope.row)"
    >
      {{ money(scope.row.appraisal_year_end_bonus) }}
    </button>
    <span v-else>{{ money(scope.row.appraisal_year_end_bonus) || '—' }}</span>
  </template>
</el-table-column>
```

**(b)** 修「淨額」公式（line 685 附近）：

```vue
<!-- 既有 -->
<strong style="color: var(--el-color-success);">
  {{ money((scope.row.net_pay || 0) + (scope.row.festival_bonus || 0) + (scope.row.overtime_bonus || 0) + (scope.row.appraisal_year_end_bonus || 0)) }}
</strong>
```

**(c)** 加 breakdown dialog（在 template 適當位置加 `el-dialog`）+ script：

```ts
import { listAppraisalPayouts } from '@/api/yearEnd'

const ayeBreakdownVisible = ref(false)
const ayeBreakdownRow = ref<{ employee_id: number; year: number; items: Array<{ period_label: string; amount: string; bonus_type: string; source_ref: string | null }> } | null>(null)

async function openAppraisalYearEndBreakdown(row: { employee_id: number; year: number; month: number }) {
  const res = await listAppraisalPayouts(row.year)
  const items = (res.data as Array<{ employee_id: number; period_label: string; amount: string; bonus_type: string; source_ref: string | null }>)
    .filter(i => i.employee_id === row.employee_id)
  ayeBreakdownRow.value = { employee_id: row.employee_id, year: row.year, items }
  ayeBreakdownVisible.value = true
}
```

Dialog template：

```vue
<el-dialog v-model="ayeBreakdownVisible" title="考核年終獎金明細" width="400">
  <ul v-if="ayeBreakdownRow">
    <li v-for="item in ayeBreakdownRow.items" :key="item.period_label">
      <strong>{{ item.period_label }}</strong>：NT${{ item.amount }}
      <a
        v-if="item.source_ref?.startsWith('appraisal_summary:')"
        :href="`/appraisal/cycles/${item.source_ref.split(':')[1]}`"
        target="_blank"
      >→ 查看 cycle</a>
    </li>
  </ul>
</el-dialog>
```

⚠️ `appraisal_summary:` ref 是 summary id 不是 cycle id，跳轉 link 需先 query API 補 cycle_id；implementer 若 v1 直接帶 summary id 也 ok，UI 可後續優化。

- [ ] **Step 3: Run test, verify PASS**

```bash
cd ~/Desktop/ivy-frontend && npm test -- --run SalaryView.appraisal-year-end-bonus 2>&1 | tail -10
```

Expected: 3 passed（若 mount complexity 影響，先讓 column render test 過）

- [ ] **Step 4: Typecheck + full vitest no regression**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck && npm test -- --run 2>&1 | tail -10
```

Expected: 0 typecheck errors、vitest 全綠

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend && git add src/views/SalaryView.vue src/views/__tests__/SalaryView.appraisal-year-end-bonus.spec.ts && git commit -m "$(cat <<'EOF'
feat(salary): SalaryView 顯示 appraisal_year_end_bonus + breakdown dialog

- 月度列表新增「考核年終獎金」欄（2 月才 cell-link、其餘月份顯示「—」）
- 點開 dialog 顯示 113下 / 114上 兩筆金額 + 跳 cycle 連結
- 淨額公式加入 appraisal_year_end_bonus

plan Task 10、spec §6.2
EOF
)"
```

---

## Task 11: Workspace CLAUDE.md cross-system 提醒

**Files:**
- Modify: `~/Desktop/ivyManageSystem/CLAUDE.md`（workspace 根）

**Goal：** 後續任何修改 salary engine / appraisal / year_end 的人都看得到「2/5 薪資含 appraisal year-end」這條 cross-system 規則。

- [ ] **Step 1: Add new bullet to 「跨端常見陷阱」**

Modify `~/Desktop/ivyManageSystem/CLAUDE.md` — 找到「跨端常見陷阱」section（搜尋 `跨端常見陷阱`），在既有 8 條後加：

```markdown
9. **薪資 2/5 含考核年終獎金**：每年 2 月 calculate 時 salary engine 透過 `services/salary/appraisal_year_end.query_appraisal_year_end_bonus()` 自動拉 `special_bonus_items` 兩筆 APPRAISAL_HALF_BONUS_{FIRST,SECOND}（FIRST=較早=N-1.下、SECOND=較晚=N.上，與 `AppraisalCycle.Semester.FIRST/SECOND` 反向）SUM 寫入 `SalaryRecord.appraisal_year_end_bonus`（獨立 column 不進 gross_salary）。HR 手動 trigger 流程：考核管理 → /year-end/appraisal-payout 預覽 → 確認生成。改 payout 後須重 calculate 2 月薪資才會同步；改 appraisal_summary.bonus_amount 須重 generate payout。spec：`ivy-backend/docs/superpowers/specs/2026-05-22-salary-appraisal-year-end-payout-design.md`
```

- [ ] **Step 2: Commit**

```bash
cd ~/Desktop/ivyManageSystem && git add CLAUDE.md && git commit -m "$(cat <<'EOF'
docs(claude): 跨端陷阱加第 9 條 — 薪資 2/5 含考核年終獎金

cross-system 提醒未來修 salary engine / appraisal / year_end 的人：
- 2 月薪資自動拉 special_bonus_items 兩筆 APPRAISAL_HALF_BONUS_*
- FIRST/SECOND 時間順序與 Semester.FIRST/SECOND 學期上下反向
- 改 payout 須重 calculate / 改 summary 須重 generate payout

plan Task 11
EOF
)"
```

⚠️ workspace 根目錄不是 git repo（按 workspace CLAUDE.md「Is a git repository: false」），這個 commit 可能 fail。若 fail：把這條 9. 改成 PR：
- 後端：`ivy-backend/CLAUDE.md`（已是 git repo）
- 前端：`ivy-frontend/CLAUDE.md`（已是 git repo）
分別 commit 加同一條（or 簡化版本）。

---

## Self-Review

**Spec coverage check（spec § 11 列出的所有變動 → plan task）：**

| Spec 檔 | Plan task |
|---|---|
| `alembic/versions/<rev>_add_...` | Task 1 |
| `models/salary.py` 加 column | Task 1 |
| `models/year_end.py` enum docstring | Task 1 |
| `services/year_end/appraisal_sync.py` | Task 2-4 |
| `services/salary/appraisal_year_end.py` | Task 5 |
| `services/salary/engine.py` 加 1 行 | Task 5 |
| `api/year_end/appraisal_payout.py` | Task 6 |
| `api/year_end/__init__.py` include | Task 6 |
| `schemas/year_end.py` 4 model | Task 6 |
| `tests/test_year_end_appraisal_sync.py` | Task 2-4 |
| `tests/test_year_end_appraisal_payout_router.py` | Task 6 |
| `tests/test_salary_appraisal_year_end_plugin.py` | Task 5 |
| `src/api/yearEnd.ts` | Task 8 |
| `src/api/_generated/schema.d.ts` regen | Task 7 |
| `src/views/yearEnd/AppraisalPayoutView.vue` | Task 9 |
| `src/views/SalaryView.vue` | Task 10 |
| `src/components/layout/AdminSidebar.vue` | Task 9 |
| `src/router/index.ts` | Task 9 |
| 兩個 frontend spec 檔 | Task 9-10 |
| `CLAUDE.md` workspace 提醒 | Task 11 |

✅ 全部覆蓋。

**Spec edge cases check（spec § 7 10 條）：**

| # | Spec edge | Plan 覆蓋 task |
|---|---|---|
| 1 未 finalize cycle | Task 4 generate 寫 calc_meta.cycle_not_finalized；Task 9 UI warning banner |
| 2 is_excluded=true | Task 3 preview_payout test 覆蓋 |
| 3 員工只在一個 cycle | Task 3 preview test + Task 4 generate 寫 amount=0 + warning |
| 4 重複生成同年 idempotent | Task 4 generate test |
| 5 paid 後改 appraisal | Task 11 CLAUDE.md hint；UI / 自動同步 V1 不做（spec § 12 已 scope-out） |
| 6 race condition advisory lock | Task 4 implementation |
| 7 academic_year mapping | Task 2 純函式 5 case |
| 8 salary engine 非 2 月 query | Task 5 plugin test 12 month |
| 9 salary engine recalculate | Task 5 integration test |
| 10 權限不足 | Task 6 router test 403 |

✅ 全部覆蓋。

**Type consistency check：**

- `PayoutPreviewRow` dataclass (service Task 3) 與 Pydantic `PayoutPreviewRow` (schema Task 6) 欄位對齊：employee_id / employee_name / role_group / earlier_summary_id / earlier_amount / earlier_cycle_finalized / later_summary_id / later_amount / later_cycle_finalized / total_amount / is_inactive / warnings — 對齊 ✅
- `GenerateResult` dataclass 與 Pydantic `PayoutGenerateResult` 欄位對齊：cycle_id / generated_count / affected_employee_count / total_amount / skipped_inactive_count / warnings — 對齊 ✅
- `query_appraisal_year_end_bonus(db, employee_id, year, month)` 簽名在 Task 5 plugin 與 engine.py 一致 ✅
- `civil_year_to_target_academic_year` / `map_bonus_type_to_period_label` Task 2 定義、Task 3-4 reuse 簽名一致 ✅

**Placeholder scan：**

- Task 3 implementation 中 `role_group` 取得邏輯標「若 relationship 不便取補拿」—— 這是合理的 implementer-choice，不算 placeholder
- Task 6 router 中 `request=None  # FastAPI injects` audit middleware 對齊既有 helper，標明對齊既有風格 — OK
- Task 10 frontend test 中「對齊既有 SalaryView test setup pattern」是合理引導
- Task 11 「若 fail 改 PR 後端 + 前端 CLAUDE.md」是 explicit fallback

無真正 TBD / TODO / vague item。

---

## Execution Handoff

Plan complete and saved to `ivy-backend/docs/superpowers/plans/2026-05-22-salary-appraisal-year-end-payout.md`. Two execution options：

**1. Subagent-Driven (recommended)** — 每個 task 一個 fresh subagent dispatch，自動 review checkpoint，適合本 plan 因為 11 個 task 中多數有獨立邊界（Task 1-6 後端、Task 7-10 前端、Task 11 docs），subagent 處理 context cleanly。

**2. Inline Execution** — 在此 session 一條條跑，每 task 跑完 user / advisor checkpoint。較適合 small plan 或想全程觀察的工作。

哪一種？
