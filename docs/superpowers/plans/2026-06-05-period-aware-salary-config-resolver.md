# 期間感知設定解析（Period-Aware Salary Config Resolver）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓薪資引擎依「正在結算的月份所屬年度」解析設定（費率/級距/獎金/底薪），修掉歷史重算套錯年度設定的隱性 bug，並在缺該年度設定時 fail-loud。

**Architecture:** 新增單一 `resolve_config(model, year)`（年度欄位 + 最高 version 解析、缺則 raise），取代散落的 `is_active + id.desc()`（當期）與 `created_at <= 月底`（歷史，`_select_active_at`）兩套查詢。所有薪資計算都已包在 `config_for_month(year, month)` → `_apply_configs_for_month` → `_select_active_at` 內，故只改 `_select_active_at` 一處即修好三種設定的歷史解析。`PositionSalaryConfig`/`AttendancePolicy` 補 `config_year` 欄位。年終 builder（民國學年映射 R2）本計畫**不含**，待業主確認。

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, pytest（SQLite in-memory fixture `test_db_session`）。

**Spec:** `docs/superpowers/specs/2026-06-05-period-aware-salary-config-resolver-design.md`

**前置（執行者必讀）:**
- fail-loud 只在 **DB-backed 的 `config_for_month` 路徑**（歷史重算 engine.py:3019 / bulk engine.py:4002）觸發；`load_from_db=False` 的純記憶體單元測試不經過 `_select_active_at`，故不會大規模破壞既有 engine 測試。
- `config_year` / `rate_year` / `effective_year` 皆為**西元**（已確認 `api/config/bonus.py:243`）。
- 開工前在 worktree 內 `git branch --show-current` 確認分支；本專案 main 常有平行 session WIP，務必在乾淨的 feature 分支上做。

---

## Task 1: 新增 config_resolver 模組（純函式，可獨立測試）

**Files:**
- Create: `services/salary/config_resolver.py`
- Test: `tests/test_config_resolver.py`

- [ ] **Step 1: 寫 failing 測試**

```python
# tests/test_config_resolver.py
import pytest

from models.database import BonusConfig, InsuranceRate, InsuranceBracket
from services.salary.config_resolver import (
    resolve_config,
    resolve_brackets,
    PayrollConfigMissingError,
)


def test_resolve_config_picks_requested_year_not_latest(test_db_session):
    """頭號 bug：DB 同時有 2026/2027，查 2026 必須回 2026（不是最新的 2027）。"""
    s = test_db_session
    s.add(BonusConfig(config_year=2026, version=1, head_teacher_ab=2000))
    s.add(BonusConfig(config_year=2027, version=1, head_teacher_ab=9999))
    s.flush()
    row = resolve_config(s, BonusConfig, 2026, year_col="config_year")
    assert row.config_year == 2026
    assert row.head_teacher_ab == 2000


def test_resolve_config_picks_highest_version_within_year(test_db_session):
    s = test_db_session
    s.add(BonusConfig(config_year=2026, version=1, head_teacher_ab=2000))
    s.add(BonusConfig(config_year=2026, version=2, head_teacher_ab=2500))
    s.flush()
    row = resolve_config(s, BonusConfig, 2026, year_col="config_year")
    assert row.version == 2
    assert row.head_teacher_ab == 2500


def test_resolve_config_missing_year_raises(test_db_session):
    s = test_db_session
    with pytest.raises(PayrollConfigMissingError) as exc:
        resolve_config(s, BonusConfig, 2099, year_col="config_year")
    assert exc.value.year == 2099
    assert exc.value.config_type == "BonusConfig"


def test_resolve_config_works_for_insurance_rate(test_db_session):
    s = test_db_session
    s.add(InsuranceRate(rate_year=2026, version=1, supplementary_health_rate=0.0211))
    s.flush()
    row = resolve_config(s, InsuranceRate, 2026, year_col="rate_year")
    assert row.rate_year == 2026


def test_resolve_brackets_returns_all_rows_for_year(test_db_session):
    s = test_db_session
    s.add(InsuranceBracket(effective_year=2026, amount=27470, labor_employee=1,
                           labor_employer=2, health_employee=3, health_employer=4, pension=5))
    s.add(InsuranceBracket(effective_year=2026, amount=28800, labor_employee=1,
                           labor_employer=2, health_employee=3, health_employer=4, pension=5))
    s.flush()
    rows = resolve_brackets(s, 2026)
    assert len(rows) == 2
    assert rows[0]["amount"] <= rows[1]["amount"]  # 依 amount 升冪


def test_resolve_brackets_missing_year_raises(test_db_session):
    s = test_db_session
    with pytest.raises(PayrollConfigMissingError):
        resolve_brackets(s, 2099)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_config_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.salary.config_resolver'`

- [ ] **Step 3: 寫最小實作**

```python
# services/salary/config_resolver.py
"""薪資設定的期間感知解析（period-aware config resolver）。

取代散落在 engine.py / insurance_service.py 的 `is_active + id.desc()`（當期）與
`created_at <= 月底`（歷史）兩套查詢。一律以設定表的「年度欄位 + 最高 version」解析，
讓年中訂正回溯套用整年；該年度無設定列即 fail-loud。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PayrollConfigMissingError(Exception):
    """指定年度的薪資設定列不存在（fail-loud）。

    caller（API 層 / bulk 預檢）應接住此例外並回可讀訊息，要求行政先建立該年度設定。
    """

    def __init__(self, config_type: str, year: int):
        self.config_type = config_type
        self.year = year
        super().__init__(
            f"找不到 {year} 年度的「{config_type}」設定，"
            f"請先於設定頁建立該年度設定後再結算。"
        )


def resolve_config(session, model, year: int, *, year_col: str, version_col: str = "version"):
    """回傳該年度「最高 version」的設定列；無列則 raise PayrollConfigMissingError。

    Args:
        session: SQLAlchemy session。
        model: ORM 設定 model class（須有 year_col、version_col、id 欄位）。
        year: 西元年度。
        year_col: 年度欄位名（如 'config_year' / 'rate_year'）。
        version_col: 同年內排序用欄位（預設 'version'）。
    """
    row = (
        session.query(model)
        .filter(getattr(model, year_col) == year)
        .order_by(getattr(model, version_col).desc(), model.id.desc())
        .first()
    )
    if row is None:
        raise PayrollConfigMissingError(model.__name__, year)
    return row


def resolve_brackets(session, year: int) -> list[dict]:
    """回傳該年度全部投保級距（依 amount 升冪）；無列則 raise。

    brackets 一年一組（多列），故與 resolve_config（回單列）分開。回傳 dict list
    對齊 InsuranceService.table 既有結構。
    """
    from models.database import InsuranceBracket

    rows = (
        session.query(InsuranceBracket)
        .filter(InsuranceBracket.effective_year == year)
        .order_by(InsuranceBracket.amount.asc())
        .all()
    )
    if not rows:
        raise PayrollConfigMissingError("InsuranceBracket", year)
    return [
        {
            "amount": r.amount,
            "labor_employee": r.labor_employee,
            "labor_employer": r.labor_employer,
            "health_employee": r.health_employee,
            "health_employer": r.health_employer,
            "pension": r.pension,
        }
        for r in rows
    ]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_config_resolver.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add services/salary/config_resolver.py tests/test_config_resolver.py
git commit -m "feat(salary): 新增期間感知設定 resolver（年度+version 解析、缺則 fail-loud）"
```

---

## Task 2: PositionSalaryConfig / AttendancePolicy 補 config_year + migration backfill

**Files:**
- Modify: `models/config.py`（PositionSalaryConfig ~L323、AttendancePolicy ~L29）
- Create: `alembic/versions/<rev>_add_config_year_to_position_and_attendance.py`
- Test: `tests/test_config_year_columns.py`

- [ ] **Step 1: 寫 failing 測試**

```python
# tests/test_config_year_columns.py
from models.database import PositionSalaryConfig, AttendancePolicy


def test_position_salary_config_has_config_year(test_db_session):
    s = test_db_session
    s.add(PositionSalaryConfig(config_year=2026, head_teacher_a=39240))
    s.flush()
    row = s.query(PositionSalaryConfig).first()
    assert row.config_year == 2026


def test_attendance_policy_has_config_year(test_db_session):
    s = test_db_session
    s.add(AttendancePolicy(config_year=2026, festival_bonus_months=3))
    s.flush()
    row = s.query(AttendancePolicy).first()
    assert row.config_year == 2026
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_config_year_columns.py -v`
Expected: FAIL — `TypeError: 'config_year' is an invalid keyword argument for PositionSalaryConfig`

- [ ] **Step 3: 在 models/config.py 加欄位**

`PositionSalaryConfig`（在 `id = Column(...)` 之後、`head_teacher_a` 之前插入）：

```python
    config_year = Column(
        Integer, nullable=False, default=0, comment="適用年度（西元）"
    )
```

`AttendancePolicy`（在 `id` 之後、`version` 之前插入）：

```python
    config_year = Column(
        Integer, nullable=False, default=0, comment="適用年度（西元）"
    )
```

> 註：model 端 `default=0` 僅供測試 create_all 用；prod 由 migration backfill 成真實年度（見 Step 4）。

- [ ] **Step 4: 寫 migration（先取 head）**

先取目前 head：

```bash
python -m alembic heads
```

把上一行印出的 revision 填入下方 `down_revision`。建立檔案
`alembic/versions/cfgyear01_add_config_year.py`：

```python
"""add config_year to position_salary_configs and attendance_policies

Revision ID: cfgyear01
Revises: <貼上 alembic heads 的結果>
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa

revision = "cfgyear01"
down_revision = "<貼上 alembic heads 的結果>"
branch_labels = None
depends_on = None

# backfill 用：本 migration 上線時的當前年度（point-in-time 常數）
_CURRENT_YEAR = 2026


def upgrade():
    # position_salary_configs：先 nullable 加欄 → backfill → 設 NOT NULL
    op.add_column(
        "position_salary_configs",
        sa.Column("config_year", sa.Integer(), nullable=True),
    )
    op.execute(
        f"UPDATE position_salary_configs SET config_year = {_CURRENT_YEAR} "
        "WHERE config_year IS NULL"
    )
    op.alter_column("position_salary_configs", "config_year", nullable=False)
    op.create_index(
        "ix_position_salary_config_year",
        "position_salary_configs",
        ["config_year", "version"],
    )

    # attendance_policies：有 effective_date 者用其年份，否則用當前年度
    op.add_column(
        "attendance_policies",
        sa.Column("config_year", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE attendance_policies "
        "SET config_year = EXTRACT(YEAR FROM effective_date)::int "
        "WHERE effective_date IS NOT NULL AND config_year IS NULL"
    )
    op.execute(
        f"UPDATE attendance_policies SET config_year = {_CURRENT_YEAR} "
        "WHERE config_year IS NULL"
    )
    op.alter_column("attendance_policies", "config_year", nullable=False)
    op.create_index(
        "ix_attendance_policy_config_year",
        "attendance_policies",
        ["config_year", "version"],
    )


def downgrade():
    op.drop_index("ix_attendance_policy_config_year", table_name="attendance_policies")
    op.drop_column("attendance_policies", "config_year")
    op.drop_index("ix_position_salary_config_year", table_name="position_salary_configs")
    op.drop_column("position_salary_configs", "config_year")
```

- [ ] **Step 5: 跑測試 + migration roundtrip 確認通過**

Run: `python -m pytest tests/test_config_year_columns.py -v`
Expected: PASS（2 passed）

Run: `python -m alembic heads`
Expected: 單一 head = `cfgyear01`

- [ ] **Step 6: Commit**

```bash
git add models/config.py alembic/versions/cfgyear01_add_config_year.py tests/test_config_year_columns.py
git commit -m "feat(config): PositionSalaryConfig/AttendancePolicy 加 config_year + backfill migration"
```

---

## Task 3: 改寫 `_select_active_at` 走 resolver（頭號 bug 修正）

**Files:**
- Modify: `services/salary/engine.py:478-511`（`_select_active_at`）
- Test: `tests/test_salary_config_period_resolution.py`（新）
- Test: `tests/test_salary_consistency_fixes.py`（重寫既有 `test_swap_uses_version_active_at_month_end`）

- [ ] **Step 1: 寫 failing 回歸測試（頭號 bug 證據）**

```python
# tests/test_salary_config_period_resolution.py
import pytest

from models.database import BonusConfig, InsuranceRate
from services.salary.engine import SalaryEngine
from services.salary.config_resolver import PayrollConfigMissingError


def _seed_bonus(s, year, head_ab):
    s.add(BonusConfig(config_year=year, version=1, head_teacher_ab=head_ab))


def test_select_active_at_resolves_by_year_not_latest(test_db_session):
    """同時有 2026/2027 BonusConfig，解析 2026 必須回 2026（舊版用 created_at 會誤回 2027）。"""
    s = test_db_session
    _seed_bonus(s, 2026, 2000)
    _seed_bonus(s, 2027, 9999)
    s.flush()
    row = SalaryEngine._select_active_at(s, BonusConfig, 2026, 1)
    assert row.config_year == 2026
    assert row.head_teacher_ab == 2000


def test_select_active_at_missing_year_raises(test_db_session):
    s = test_db_session
    with pytest.raises(PayrollConfigMissingError):
        SalaryEngine._select_active_at(s, InsuranceRate, 2099, 1)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_salary_config_period_resolution.py -v`
Expected: FAIL — 第一個 assert 回到 2027（舊 created_at 邏輯），第二個不 raise

- [ ] **Step 3: 改寫 `_select_active_at`**

在 `engine.py` 找到 `_select_active_at`（約 L478-511），整段替換為：

```python
    # 設定 model → 年度欄位（西元）對照；period-aware 解析用
    _CONFIG_YEAR_COL_BY_NAME = {
        "InsuranceRate": "rate_year",
        "AttendancePolicy": "config_year",
        "BonusConfig": "config_year",
    }

    @staticmethod
    def _select_active_at(session, model, year: int, month: int):
        """以「該年度最新 version」解析設定（period-aware）。

        取代舊的 `created_at <= 當月最後一日` 邏輯：改用設定表年度欄位 + 最高 version，
        讓年中訂正回溯套用整年；該年度無設定列即 fail-loud（PayrollConfigMissingError）。
        month 參數保留以維持呼叫端介面，不再參與解析。
        Refs: spec 2026-06-05-period-aware-salary-config-resolver-design.md
        """
        from services.salary.config_resolver import resolve_config

        year_col = SalaryEngine._CONFIG_YEAR_COL_BY_NAME[model.__name__]
        return resolve_config(session, model, year, year_col=year_col)
```

> 註：呼叫端（engine.py:590/599/606）的 `if rate is not None:` 等守衛保留即可——resolver 改為「命中或 raise」，永不回 None，守衛變恆真但無害，raise 會正常往上傳到 `config_for_month` 外層。
>
> **spec §5.1 偏離說明（baseline 路徑刻意不改，已精確驗證各 calc 路徑）**：
> `_load_config_from_db_locked`（engine.py:733/750/767 的 `is_active + id.desc()`）**維持不變**。
> 各計算路徑對 config_for_month 的使用實況（已逐一 grep 驗證）：
> - **持久化金流路徑**：bulk `process_bulk_salary_calculation`（4002）、歷史重算（3019）**都包**
>   config_for_month → 經本 resolver 依年度解析 → **歷史重算 bug 在此修正**（這正是本計畫目標）。
> - **期間累積** `_compute_period_accrual_totals`（2770）**內部包** config_for_month → 任何路徑（含 simulate/
>   單筆）的累積月份都會經 resolver，故**可能 raise**（見 Task 6 simulate 處理）。
> - **單筆 `process_salary_calculation`（3108）主月** 與 **`/salaries/simulate` 主月**（simulate.py:339
>   直呼 `calculate_salary`）**不包** config_for_month → 主月用 baseline（latest active）。對「當期」而言
>   latest==resolve(當年)為同一列，數字不變；對「歷史」單筆/simulate 主月則維持既有行為（未修，但 simulate
>   為唯讀沙盒、單筆主要用於當期）。
> 結論：baseline 不改對「持久化金流的歷史重算正確性」無影響（該路徑已由 config_for_month 修正）；
> 「歷史 simulate 主月也精確」列為 follow-up（見計畫末）。此為對 spec §5.1 字面「兩路徑都改」的務實、
> 已驗證偏離。
>
> **spec §6 R3（GradeTarget）已滿足，無需獨立 task**：`_apply_configs_for_month`（engine.py:610-637）
> 既有邏輯已用 `bonus_config_id == bonus.id`（解析出的 BonusConfig FK）+ NULL fallback 載入 GradeTarget。
> Task 3 讓 `_select_active_at` 回正確年度的 bonus 後，GradeTarget 自然隨正確 bonus 的 FK 一併取得，
> R3「隨已解析 BonusConfig 一併載入」即成立。

- [ ] **Step 4: 跑新測試確認通過**

Run: `python -m pytest tests/test_salary_config_period_resolution.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 重寫既有 swap 測試（語意改變）**

開 `tests/test_salary_consistency_fixes.py`，找到 `test_swap_uses_version_active_at_month_end`。
舊測試斷言「以 created_at <= 月底 選版本」；新語意是「以年度 + 最高 version 選版本」。
將該測試函式 body 改為驗證新語意（保留同名）：

```python
def test_swap_uses_version_active_at_month_end(test_db_session):
    """設定切換改以『年度 + 最高 version』解析（取代舊 created_at<=月底 語意）。

    同年度多 version → 取最高 version；解析另一年度 → 取該年度版本。
    """
    from models.database import BonusConfig
    from services.salary.engine import SalaryEngine

    s = test_db_session
    s.add(BonusConfig(config_year=2026, version=1, head_teacher_ab=2000))
    s.add(BonusConfig(config_year=2026, version=2, head_teacher_ab=2500))
    s.add(BonusConfig(config_year=2025, version=1, head_teacher_ab=1800))
    s.flush()

    row_2026 = SalaryEngine._select_active_at(s, BonusConfig, 2026, 12)
    assert row_2026.version == 2 and row_2026.head_teacher_ab == 2500

    row_2025 = SalaryEngine._select_active_at(s, BonusConfig, 2025, 12)
    assert row_2025.config_year == 2025 and row_2025.head_teacher_ab == 1800
```

> 若舊測試 import 了已移除的 helper，一併清掉。執行者請先 `git show HEAD:tests/test_salary_consistency_fixes.py` 確認舊 body 再覆蓋。

- [ ] **Step 6: 跑改寫後的測試確認通過**

Run: `python -m pytest tests/test_salary_consistency_fixes.py::test_swap_uses_version_active_at_month_end -v`
Expected: PASS

- [ ] **Step 7: 跑完整薪資 engine 測試抓回歸（重要）**

Run: `python -m pytest tests/ -k "salary or engine or bonus or insurance" -q`
Expected: 全 PASS。若有 DB-backed 測試因「未 seed 該年度設定」而 raise PayrollConfigMissingError，
在該測試的 setup 補上對應年度的 BonusConfig/InsuranceRate 列（這正是 fail-loud 的預期行為）。
**不可**為了讓測試過而把 fail-loud 改回 silent。

- [ ] **Step 8: Commit**

```bash
git add services/salary/engine.py tests/test_salary_config_period_resolution.py tests/test_salary_consistency_fixes.py
git commit -m "fix(salary): 設定解析改用年度+version（修歷史重算套錯年度設定的 bug）"
```

---

## Task 4: 底薪（PositionSalaryConfig）歷史路徑年度化

**Files:**
- Modify: `services/salary/engine.py:83-107`（`load_position_salary_standards`）
- Modify: `services/salary/engine.py`（`_apply_configs_for_month`，約 L581-637，補載入年度底薪）
- Test: `tests/test_salary_config_period_resolution.py`（追加）

- [ ] **Step 1: 寫 failing 測試**

```python
# 追加到 tests/test_salary_config_period_resolution.py
def test_position_standards_resolved_by_year(test_db_session):
    from models.database import PositionSalaryConfig
    from services.salary.engine import load_position_salary_standards

    s = test_db_session
    s.add(PositionSalaryConfig(config_year=2026, version=1, head_teacher_a=39240))
    s.add(PositionSalaryConfig(config_year=2027, version=1, head_teacher_a=99999))
    s.flush()
    std = load_position_salary_standards(s, year=2026)
    assert std["head_teacher_a"] == 39240.0


def test_position_standards_no_year_keeps_latest(test_db_session):
    """year=None 維持舊行為（latest id desc），供年終 builder 等未遷移 caller 用。"""
    from models.database import PositionSalaryConfig
    from services.salary.engine import load_position_salary_standards

    s = test_db_session
    s.add(PositionSalaryConfig(config_year=2026, version=1, head_teacher_a=39240))
    s.add(PositionSalaryConfig(config_year=2027, version=1, head_teacher_a=99999))
    s.flush()
    std = load_position_salary_standards(s)  # 無 year
    assert std["head_teacher_a"] == 99999.0
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_salary_config_period_resolution.py -k position_standards -v`
Expected: FAIL — `load_position_salary_standards() got an unexpected keyword argument 'year'`

- [ ] **Step 3: 改寫 `load_position_salary_standards`**

把 `engine.py:83-107` 的函式整段替換為（加 `year` 參數；有 year 走 resolver、無 year 維持舊 latest 行為）：

```python
def load_position_salary_standards(session, year: int | None = None) -> dict:
    """從 session 讀取職位標準底薪（key: 'head_teacher_b'/… → float|None）。

    - year 指定：走 resolve_config（該年度最新 version；缺則 fail-loud）——薪資引擎歷史/當期路徑用。
    - year=None：維持舊行為（PositionSalaryConfig 最新 id desc）——年終 builder 等未遷移 caller 用。
    director / principal 允許 None（留空表示不套標準，回個人 emp.base_salary）。
    """
    from models.database import PositionSalaryConfig

    if year is not None:
        from services.salary.config_resolver import resolve_config

        pos_cfg = resolve_config(
            session, PositionSalaryConfig, year, year_col="config_year"
        )
    else:
        pos_cfg = (
            session.query(PositionSalaryConfig)
            .order_by(PositionSalaryConfig.id.desc())
            .first()
        )
    standards = {
        k: float(getattr(pos_cfg, k, None) or v) if pos_cfg else float(v)
        for k, v in _POSITION_SALARY_DEFAULTS.items()
    }
    for _role in ("director", "principal"):
        _val = getattr(pos_cfg, _role, None) if pos_cfg else None
        standards[_role] = float(_val) if _val else None
    return standards
```

- [ ] **Step 4a: 把年度底薪載入接進 `_apply_configs_for_month`**

state 欄位確認為 `self._position_salary_standards`（engine.py:379 初始化、809 baseline 載入、
2221/2233/2236/2265 取用）。在 `_apply_configs_for_month`（約 L595，`load_brackets_from_db(year)`
之後）加：

```python
        # 歷史月份重算：以該月所屬年度載入職位標準底薪
        self._position_salary_standards = load_position_salary_standards(session, year=year)
```

- [ ] **Step 4b: 把 `_position_salary_standards` 納入 snapshot/restore（必須，否則洩漏）**

`_position_salary_standards` 目前**不在** `_snapshot_config_state`（L412-450）/`_restore_config_state`
（L454-476）名單，若不補，歷史月份底薪會在離開 config_for_month 後洩漏到 baseline singleton。

在 `_snapshot_config_state` 回傳 dict 內（如 `"position_grade_map": dict(self._position_grade_map),` 之後）加：

```python
            "position_salary_standards": dict(self._position_salary_standards),
```

在 `_restore_config_state` 內（如 `position_grade_map` 還原區塊之後）加：

```python
        if "position_salary_standards" in snapshot:
            self._position_salary_standards = snapshot["position_salary_standards"]
```

- [ ] **Step 4c: 加 snapshot 還原回歸測試**

```python
# 追加到 tests/test_salary_config_period_resolution.py
def test_position_standards_restored_after_config_for_month(test_db_session):
    """歷史月底薪不可洩漏到 baseline：離開 config_for_month 後須還原。"""
    from models.database import PositionSalaryConfig
    from services.salary.engine import SalaryEngine

    s = test_db_session
    s.add(PositionSalaryConfig(config_year=2026, version=1, head_teacher_a=39240))
    s.flush()
    engine = SalaryEngine(load_from_db=False)
    engine._position_salary_standards = {"head_teacher_a": 11111.0}  # baseline 哨兵值
    with engine.config_for_month(s, 2026, 1):
        assert engine._position_salary_standards["head_teacher_a"] == 39240.0
    # 離開後還原成哨兵值（未洩漏歷史月值）
    assert engine._position_salary_standards["head_teacher_a"] == 11111.0
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_salary_config_period_resolution.py -k position_standards -v`
Expected: PASS（3 passed）

- [ ] **Step 6: Commit**

```bash
git add services/salary/engine.py tests/test_salary_config_period_resolution.py
git commit -m "feat(salary): 職位標準底薪歷史路徑依年度解析（year-aware）"
```

---

## Task 5: 投保級距歷史路徑 fail-loud

**Files:**
- Modify: `services/insurance_service.py:781-860`（`load_brackets_from_db` 加 `require_year`）
- Modify: `services/salary/engine.py:595`（歷史路徑改 require_year=True）
- Test: `tests/test_insurance_brackets_db.py`（追加）

- [ ] **Step 1: 寫 failing 測試**

```python
# 追加到 tests/test_insurance_brackets_db.py
import pytest
from services.salary.config_resolver import PayrollConfigMissingError


def test_load_brackets_require_year_raises_when_missing(test_db_session):
    """require_year=True 且該年度無級距 → fail-loud（不再 silent fallback 到 hardcode/前一年）。"""
    from services.insurance_service import InsuranceService

    svc = InsuranceService()
    with pytest.raises(PayrollConfigMissingError):
        svc.load_brackets_from_db(2099, require_year=True)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_insurance_brackets_db.py -k require_year -v`
Expected: FAIL — `load_brackets_from_db() got an unexpected keyword argument 'require_year'`

- [ ] **Step 3: 改 `load_brackets_from_db` 簽名與「無該年度列」分支**

把簽名改為：

```python
    def load_brackets_from_db(
        self, year: int | None = None, *, strict: bool = False, require_year: bool = False
    ) -> bool:
```

在「該年度無資料」分支（現約 L810，`if not rows:` 內）最前面加 require_year 短路：

```python
                if not rows:
                    if require_year:
                        from services.salary.config_resolver import (
                            PayrollConfigMissingError,
                        )

                        raise PayrollConfigMissingError("InsuranceBracket", year)
                    # 既有 silent fallback（前一年 / hardcode）保留給 startup / require_year=False
                    fallback_year = (
                        ...  # 原邏輯不動
```

更新 docstring 說明 require_year（薪資引擎歷史/當期路徑用 True，確保不靜默套到錯年度級距）。

- [ ] **Step 4: 歷史路徑改用 require_year=True**

`engine.py:595`：

```python
        # 歷史月份重算：以該月份所屬年度載入級距表（缺該年度即 fail-loud）
        self.insurance_service.load_brackets_from_db(year, require_year=True)
```

> 注意：baseline 路徑 `_load_config_from_db_locked`（engine.py:743）的 `load_brackets_from_db()`
> **維持不變**（require_year 預設 False），確保 engine bootstrap 不會因缺當年度級距而崩。

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_insurance_brackets_db.py -v`
Expected: 全 PASS（含新測試 + 既有測試不回歸）

- [ ] **Step 6: Commit**

```bash
git add services/insurance_service.py services/salary/engine.py tests/test_insurance_brackets_db.py
git commit -m "feat(salary): 投保級距歷史路徑缺年度即 fail-loud（require_year）"
```

---

## Task 6: Fail-loud 在 API 呈現（422）

**前提（已由 Task 3+5 達成，不需獨立預檢）:** `process_bulk_salary_calculation` 在
per-employee 迴圈前以 `with self.config_for_month(session, year, month)`（engine.py:4002）載入設定，
該 context 進入時即呼叫 `_select_active_at`（Task 3）+ `load_brackets_from_db(year, require_year=True)`
（Task 5）。缺該年度設定 → 在此 raise `PayrollConfigMissingError`，發生在任何 SalaryRecord 寫入與
`session.commit()`（engine.py:4087）**之前**；外層 `except`（engine.py:4090-4093）`session.rollback()`
後 `raise` re-raise → **整批零寫入自動達成**。故 Task 6 只需把例外在 API 層轉成 422。

**Files:**
- Modify: `api/salary/calculate.py`（呼叫 `engine.process_bulk_salary_calculation` 的 L178 / L276 與 handler L199）
- Test: `tests/test_salary_calculate_fail_loud.py`（新）

- [ ] **Step 1: 寫 failing 測試（驗證引擎層整批中止 + 零寫入）**

```python
# tests/test_salary_calculate_fail_loud.py
import pytest
from services.salary.config_resolver import PayrollConfigMissingError
from services.salary.engine import SalaryEngine


def test_bulk_aborts_whole_run_when_year_config_missing(test_db_session):
    """bulk 結算缺該年度設定 → config_for_month 進入時 raise，整批中止、零 SalaryRecord 寫入。

    註：process_bulk_salary_calculation 自管 session（無 session 參數）；test_db_session
    fixture 已 swap 全域 _SessionFactory，故引擎內部 session = 本測試 DB。
    """
    from models.database import SalaryRecord

    engine = SalaryEngine(load_from_db=False)
    # 不 seed 任何 BonusConfig/InsuranceRate/InsuranceBracket（該年度設定全缺）
    with pytest.raises(PayrollConfigMissingError):
        engine.process_bulk_salary_calculation(
            employee_ids=["E001"], year=2099, month=1
        )
    # 整批中止：無任何 SalaryRecord 落帳
    assert test_db_session.query(SalaryRecord).count() == 0
```

> 若 `process_bulk_salary_calculation` 對空/未知 employee_ids 在進入 config_for_month 前提早
> return（短路），改為先 seed 一位最小有效 Employee 再以其 id 呼叫，確保流程行進到 4002。

- [ ] **Step 2: 跑測試確認當前狀態**

Run: `python -m pytest tests/test_salary_calculate_fail_loud.py -v`
Expected: 若 Task 3+5 已合入 → 此測試應已 PASS（引擎層 fail-loud 已生效）。
若尚未 → FAIL，先完成 Task 3+5。本 Task 的真正新增是 Step 3 的 API 422 對應。

- [ ] **Step 3: API 層接住例外回 422**

在 `api/salary/calculate.py`，把呼叫 `engine.process_bulk_salary_calculation(...)` 的兩處
（L178、L276）各自用 try/except 包住轉 422。先在檔案頂部確認已 import（沒有則加）：

```python
from fastapi import HTTPException
from services.salary.config_resolver import PayrollConfigMissingError
```

每個呼叫點改為：

```python
    try:
        bulk_results, errors = engine.process_bulk_salary_calculation(
            ...  # 既有參數原樣保留
        )
    except PayrollConfigMissingError as e:
        raise HTTPException(status_code=422, detail=str(e))
```

> `str(e)` 即 PayrollConfigMissingError 的可讀中文訊息（含年度 + 設定類型）。
> `/salaries/calculate-async`（L330）若在背景 worker 跑，於該 worker 失敗邊界 log + 標記 job
> 失敗（依該檔既有 async 失敗處理慣例），不在此 handler 同步 raise。

- [ ] **Step 3b: `/salaries/simulate` 也要接 422（重要，否則 500）**

simulate 主月雖走 baseline，但其 `engine._compute_period_accrual_totals(session, emp, year, month)`
（simulate.py:325）內部會進 `config_for_month` → 缺累積月份設定時會 raise `PayrollConfigMissingError`。
simulate 在 loadtest 路徑上，未接會變 500。在 `api/salary/simulate.py` 的 `simulate_salary` handler
（L178）把計算區塊（至少涵蓋 L325 的 `_compute_period_accrual_totals` 與 L339 的 `calculate_salary`）
用 try/except 包住轉 422。先確認 import：

```python
from fastapi import HTTPException
from services.salary.config_resolver import PayrollConfigMissingError
```

```python
    try:
        ...  # 既有 _compute_period_accrual_totals / calculate_salary 等計算
    except PayrollConfigMissingError as e:
        raise HTTPException(status_code=422, detail=str(e))
```

> 若 simulate handler 已有外層 try/except（多半有，sandbox 錯誤處理），把
> `except PayrollConfigMissingError` 放在更廣的 `except Exception` **之前**，避免被泛用攔截吞成 500。

- [ ] **Step 4: 加 API 422 整合測試**

```python
# 追加到 tests/test_salary_calculate_fail_loud.py
def test_calculate_endpoint_returns_422_when_config_missing(admin_client, test_db_session):
    """POST /salaries/calculate 缺年度設定 → 422 + 可讀訊息（非 500）。

    註：admin_client 為本專案既有的「已登入 admin」測試 client fixture；
    若 fixture 名不同（grep tests/conftest.py 'client' 對齊），改用對應 fixture。
    """
    resp = admin_client.post("/api/salaries/calculate", json={"year": 2099, "month": 1})
    assert resp.status_code == 422
    assert "2099" in resp.json()["detail"]
```

> 執行者先 `grep -rn "def admin_client\|def authed_client\|TestClient" tests/conftest.py` 對齊
> 既有 client fixture 名稱與登入方式；request body 欄位以 `/salaries/calculate` handler 簽名為準。

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_salary_calculate_fail_loud.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add api/salary/calculate.py api/salary/simulate.py tests/test_salary_calculate_fail_loud.py
git commit -m "feat(salary): 缺年度設定時 calculate/simulate API 回 422（fail-loud 呈現）"
```

---

## Task 7: Gold recon 不位移驗證（驗證 gate，無新功能碼）

**Files:**
- 無修改；僅執行既有 gold/對帳測試確認數字零位移。

- [ ] **Step 1: 找出 gold/對帳測試**

```bash
grep -rln "gold\|recon\|對帳\|義華\|excel" tests/ | grep -i salary
```

- [ ] **Step 2: 跑全套薪資 + 年終測試**

Run: `python -m pytest tests/ -k "salary or year_end or insurance or bonus or gold or recon" -q`
Expected：
- **當期（義華 recon / 當前年度）gold 測試：數字零位移**。若位移 → 確認 migration backfill 是否把當前
  年度設定列正確等同「原 latest-active」值。
- **歷史年度重算的 gold 測試**：若該測試重算的是「latest-config ≠ 該年度 config」的舊年度，數字**可能
  正確地改變**（這正是本計畫修好的 bug，不是回歸）。對這類測試：驗證新數字是「依該年度設定算出的正確值」，
  而非斷言「與舊值相同」；必要時更新該測試的期望值並在 commit message 註明係 bug 修正導致。

- [ ] **Step 3: 跑完整測試套件最終確認**

Run: `python -m pytest tests/ -q`
Expected: 全綠（除既有已知 xfail/skip）。新增的 fail-loud 行為若打到某些 DB-backed 測試，
依 Task 3 Step 7 原則在該測試 setup 補當年度設定列。

- [ ] **Step 4: 確認 alembic 單 head + roundtrip**

Run: `python -m alembic heads`
Expected: 單一 head = `cfgyear01`

- [ ] **Step 5: Commit（若 Step 3 補了測試 setup）**

```bash
git add tests/
git commit -m "test(salary): 補 DB-backed 測試的年度設定 seed（對齊 fail-loud）"
```

---

## 部署備註（合併後、push 前）

- prod 後端目前 Zeabur **SUSPENDED**；`cfgyear01` migration 待 resume 才跑（依 workspace CLAUDE.md §收尾紀律）。
- resume 前確認 prod `position_salary_configs` / `attendance_policies` 既有列會被 backfill 成正確年度（migration 已內建 `_CURRENT_YEAR=2026`；若 resume 時已跨年需調整常數）。
- 無 response schema 變動 → 不需 `gen:api`（fail-loud 只新增 422 + 訊息 body，前端 axios 攔截器已有泛用 4xx displayMessage）。
- CI gate：`alembic-heads`（單 head）、`alembic-roundtrip`（up/down 對稱）、`money-rounding-gate`（本計畫無新 round() 路徑）皆須綠。

## 範圍外 / Follow-up（本計畫不含）

- **年終 builder 4 處**（`settlement_builder`/`after_class_award`/`semester_dividend`/`attendance_deductions`）改走 resolver：依 spec §6 R2，需先業主確認「民國學年 → 西元 config_year」映射；確認前維持現行 `id.desc()`。
- 範圍 C：config 寫入端 `finance_approve + audit` 守衛（`api/config.py`/`api/insurance.py`）。
- A3 後半：期間關帳唯讀（CLOSED 期間禁 SalaryRecord UPDATE）。
- 缺設定 fail-loud 觸發時推 LINE 告警給行政。
- **歷史 simulate / 單筆 `process_salary_calculation` 主月精確化**：目前兩者主月走 baseline（當期正確、
  歷史維持舊行為）。若需「歷史 simulate 主月也依年度精確」，把 simulate.py:339 的 `calculate_salary` 與
  單筆主月計算包進 `config_for_month(session, year, month)`。本計畫範圍只保證「持久化金流的歷史重算」正確
  （bulk/recalc 已包），故此項列為 follow-up。
```
