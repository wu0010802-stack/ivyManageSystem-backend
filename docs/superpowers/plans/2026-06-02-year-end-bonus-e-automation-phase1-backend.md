# 年終獎金 E 化重構 階段 1（後端）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把年終獎金 6 步引擎從「只有測試呼叫」接線成 production：跨員工自動拉專案資料 → 跑 `compute_settlement` → upsert `year_end_settlements`，取代「匯入手 key Excel」，並把考核獎金併入年終、簡化為兩關簽核。

**Architecture:** 復用既有純函式引擎 `services/year_end/engine.py` 與 6 張表；新增 `enrollment_rates.py`（在籍/達成率）與 `settlement_builder.py`（資料蒐集 + 編排 + upsert）兩層；修正 `appraisal_sync` 取的考核學期；移除 salary engine 的 2 月考核 pull（考核改由年終發放）；新增 3 個 API 端點；簽核改 DRAFT→會計→老闆兩關。

**Tech Stack:** FastAPI、SQLAlchemy 2.0（Mapped）、PostgreSQL、Alembic、pytest、Decimal（全程 HALF_UP）。

**對應 spec：** `docs/superpowers/specs/2026-06-01-year-end-bonus-e-automation-phase1-design.md`（6 個決策見 §10）

**驗收金標準：** 用 `114年年終經營績效.xls` 的數字，系統算出的 settlement 逐人吻合（容差 ≤1 元）。鎖定 case：蔡宜倩 total=40106.71、林姿妙 total=35036.92、郭玟秀 payable=14632.35。

---

## File Structure

| 檔案 | 責任 |
|---|---|
| `services/year_end/enrollment_rates.py`（new）| 在籍人數（嚴格 filter）→ 全校達成率、班級經營績效 純查詢 |
| `services/year_end/settlement_builder.py`（new）| 蒐集每員工引擎輸入 + 編排 `compute_settlement` + upsert snapshot/settlement |
| `services/year_end/appraisal_sync.py`（修改）| 決策②：考核學期改前學年上+下；docstring 清理 |
| `services/salary/engine.py`（修改）| 決策⑥B：移除 2 月考核 pull |
| `services/salary/appraisal_year_end.py`（修改）| 標 deprecated（保留函式避免 import 爆） |
| `api/year_end/__init__.py`（修改）| +3 端點（build-settlements / grid / manual-patch）；簽核改兩關 |
| `schemas/year_end.py`（修改）| +build/grid/manual schema |
| `tests/test_year_end_enrollment_rates.py`（new）| Task 1 |
| `tests/test_year_end_settlement_builder.py`（new）| Task 2/3/8 |
| `tests/test_year_end_appraisal_refactor.py`（new）| Task 4/5 |
| `tests/test_year_end_grid_api.py`（new）| Task 6/7 |

---

## Task 1: `enrollment_rates.py` — 在籍/達成率純查詢

**Files:**
- Create: `services/year_end/enrollment_rates.py`
- Test: `tests/test_year_end_enrollment_rates.py`

決策③：在籍判定用**嚴格條件**（排除已退學 / lifecycle 非 active），統一於本檔，**不**直接用 `services/student_enrollment.count_students_active_on`（它只看 enrollment/graduation 日期、不排退學）。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_year_end_enrollment_rates.py
from datetime import date
from decimal import Decimal
from services.year_end import enrollment_rates as er


def test_school_achievement_rate_excludes_withdrawn(db_session, make_student):
    # 目標 160，basis date 在籍 2 人、退學 1 人（withdrawal 在 basis 前）
    make_student(enrollment_date=date(2025, 8, 1))            # 在籍
    make_student(enrollment_date=date(2025, 8, 1))            # 在籍
    make_student(enrollment_date=date(2025, 8, 1),
                 withdrawal_date=date(2025, 9, 1),
                 lifecycle_status="withdrawn")                # 已退學，不算
    rate = er.school_achievement_rate(db_session, date(2025, 9, 15), target=160)
    # 2/160*100 = 1.25
    assert rate == Decimal("1.25")


def test_class_performance_rate_avg_over_months(db_session, make_student):
    # 編制 12；某班 6 個月底在園分別 14,14,14,14,13,14 → 平均 13.833 / 12 *100 = 115.28
    ...
    rate = er.class_performance_rate(
        db_session, classroom_id=cls.id,
        month_ends=[date(2025,8,31),date(2025,9,30),date(2025,10,31),
                    date(2025,11,30),date(2025,12,31),date(2026,1,31)],
        head_count_target=12,
    )
    assert rate == Decimal("115.28")
```

> 註：`make_student` fixture 若不存在，用既有 `tests/conftest.py` 的學生建構慣例補；`lifecycle_status` 欄位名以 `models/student.py` 實際為準（搜 `lifecycle_status`）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_year_end_enrollment_rates.py -v`
Expected: FAIL（module 不存在 / function 未定義）

- [ ] **Step 3: 實作**

```python
# services/year_end/enrollment_rates.py
"""年終達成率純查詢：在籍嚴格 filter（排除已退學）→ 全校達成率、班級經營績效。

決策③：不沿用 student_enrollment.count_students_active_on（只看入學/畢業日期，
不排退學）。本檔統一在籍判定 = 入學日 <= D <= (畢業日 or ∞) AND 未退學 AND lifecycle active。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import and_, or_, func, select
from sqlalchemy.orm import Session

from models.student import Student  # 以實際路徑為準

_Q2 = Decimal("0.01")


def _q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(_Q2, rounding=ROUND_HALF_UP)


def _enrolled_filter(d: date):
    return and_(
        or_(Student.enrollment_date.is_(None), Student.enrollment_date <= d),
        or_(Student.graduation_date.is_(None), Student.graduation_date >= d),
        or_(Student.withdrawal_date.is_(None), Student.withdrawal_date > d),
        Student.lifecycle_status == "active",   # 值以 enum 實際為準
    )


def count_enrolled_on(db: Session, d: date, classroom_id: int | None = None) -> int:
    stmt = select(func.count(Student.id)).where(_enrolled_filter(d))
    if classroom_id is not None:
        stmt = stmt.where(Student.classroom_id == classroom_id)
    return int(db.scalar(stmt) or 0)


def school_achievement_rate(db: Session, basis_date: date, target: int) -> Decimal:
    if target <= 0:
        return Decimal("0.00")
    actual = count_enrolled_on(db, basis_date)
    return _q2(Decimal(actual) / Decimal(target) * 100)


def class_performance_rate(
    db: Session, classroom_id: int, month_ends: list[date], head_count_target: int
) -> Decimal:
    """班級經營績效 = 各月底在園平均 / 編制 ×100。
    月底各班在園用 classroom_at_month_end resolver（轉班歷史優先），
    避免直接用 Student.classroom_id 失準。"""
    if head_count_target <= 0 or not month_ends:
        return Decimal("0.00")
    from services.gov_moe.monthly_calculator import classroom_at_month_end  # 復用既有 resolver
    counts: list[int] = []
    for me in month_ends:
        n = 0
        for (sid,) in db.execute(select(Student.id).where(_enrolled_filter(me))):
            if classroom_at_month_end(db, sid, me) == classroom_id:
                n += 1
        counts.append(n)
    avg = Decimal(sum(counts)) / Decimal(len(counts))
    return _q2(avg / Decimal(head_count_target) * 100)
```

> ⚠️ 效能：`class_performance_rate` 逐生逐月呼 resolver 為 O(學生×月)。階段 1 可接受（單次試算、~150 生×6 月）；若慢，改用 `monthly_enrollment_snapshots`（`models/gov_moe.py`）快取。先求正確。

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_year_end_enrollment_rates.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/year_end/enrollment_rates.py tests/test_year_end_enrollment_rates.py
git commit -m "feat(year-end): 在籍嚴格 filter + 全校達成率/班級經營績效純查詢"
```

---

## Task 2: `settlement_builder.py` 輸入蒐集 helpers

**Files:**
- Create: `services/year_end/settlement_builder.py`
- Test: `tests/test_year_end_settlement_builder.py`（本 Task 先加 helper 測試）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_year_end_settlement_builder.py
from decimal import Decimal
from services.year_end import settlement_builder as sb


def test_festival_base_lookup_by_role(db_session, make_bonus_config):
    # 決策④：節慶為角色基數查表（單筆），非全年加總
    make_bonus_config(head_teacher_ab=2000, principal_festival=6500)
    assert sb.festival_base_for_role(db_session, "head_teacher_ab") == Decimal("2000")
    assert sb.festival_base_for_role(db_session, "principal") == Decimal("6500")


def test_hire_months_full_year(make_employee):
    emp = make_employee(hire_date=None, resign_date=None)  # 在職滿年
    assert sb.compute_hire_months(emp, cycle_start, cycle_end) == Decimal("12")


def test_hire_months_partial(make_employee):
    # 到職 10 個月 → 10
    emp = make_employee(hire_date=date(2025, 4, 1))
    assert sb.compute_hire_months(emp, date(2025,2,1), date(2026,1,31)) == Decimal("10")


def test_org_achievement_rate_full_vs_partial():
    # full-year：兩學期平均；partial：只取在職那學期
    assert sb.resolve_org_achievement_rate(Decimal("75.6"), Decimal("91.5"),
                                           worked_first=True, worked_second=True) == Decimal("83.6")
    assert sb.resolve_org_achievement_rate(Decimal("75.6"), Decimal("91.5"),
                                           worked_first=False, worked_second=True) == Decimal("91.5")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_year_end_settlement_builder.py -v`
Expected: FAIL

- [ ] **Step 3: 實作 helpers**

```python
# services/year_end/settlement_builder.py
"""年終結算 builder：蒐集每員工引擎輸入 → compute_settlement → upsert。

決策（spec §10）：①基本薪復用 salary engine _resolve_standard_base；
④節慶=角色基數查表；②考核由 appraisal_sync 寫 special_bonus_items 後 SUM。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from models.config import BonusConfig
from services.year_end.engine import (
    PerformanceRates, DeductionBreakdown, compute_settlement,
)

_Q2 = Decimal("0.01")


def _q2(x): return Decimal(x).quantize(_Q2, rounding=ROUND_HALF_UP)


# 角色 → BonusConfig 節慶基數欄位
_FESTIVAL_FIELD = {
    "head_teacher_ab": "head_teacher_ab", "head_teacher_c": "head_teacher_c",
    "assistant_teacher_ab": "assistant_teacher_ab", "assistant_teacher_c": "assistant_teacher_c",
    "principal": "principal_festival", "director": "director_festival",
    "leader": "leader_festival", "driver": "driver_festival",
    "designer": "designer_festival", "admin": "admin_festival",
    "art_teacher": "art_teacher_festival",
}


def festival_base_for_role(db: Session, role_key: str) -> Decimal:
    cfg = db.query(BonusConfig).order_by(BonusConfig.id.desc()).first()
    if cfg is None:
        return Decimal("0")
    field = _FESTIVAL_FIELD.get(role_key)
    val = getattr(cfg, field, 0) if field else 0
    return Decimal(str(val or 0))


def compute_hire_months(emp, cycle_start: date, cycle_end: date) -> Decimal:
    hire = getattr(emp, "hire_date", None)
    resign = getattr(emp, "resign_date", None)
    start = max(hire, cycle_start) if hire else cycle_start
    end = min(resign, cycle_end) if resign else cycle_end
    if end < start:
        return Decimal("0")
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    return Decimal(min(12, max(0, months)))


def resolve_org_achievement_rate(
    first: Decimal, second: Decimal, *, worked_first: bool, worked_second: bool
) -> Decimal:
    vals = []
    if worked_first and first is not None:
        vals.append(Decimal(first))
    if worked_second and second is not None:
        vals.append(Decimal(second))
    if not vals:
        return Decimal("0.0")
    return _q2(sum(vals) / Decimal(len(vals)))
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_year_end_settlement_builder.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/year_end/settlement_builder.py tests/test_year_end_settlement_builder.py
git commit -m "feat(year-end): settlement builder 輸入蒐集 helpers（節慶查表/到職月/機構比率）"
```

---

## Task 3: `build_settlements` 編排 + upsert

**Files:**
- Modify: `services/year_end/settlement_builder.py`
- Modify: `tests/test_year_end_settlement_builder.py`

決策①A：基本薪復用月薪引擎 `_resolve_standard_base`（`services/salary/engine.py:2195`，多數=職位標準、資深特例=個人 `emp.base_salary`），確保年終基本薪 = 月薪底薪、對齊 Excel。

- [ ] **Step 1: 寫失敗測試（端到端鎖 Excel 蔡宜倩）**

```python
def test_build_settlement_matches_excel_tsai(db_session, seed_cycle_114, seed_tsai):
    """蔡宜倩：base=36160, festival=2000, avg=97.0, org=83.6,
       扣款 機構研習-1000 + 遲到-900, special=11062 → total=40106.71"""
    res = sb.build_settlements(db_session, academic_year=114,
                               included_resigned_ids=set(), actor_id=1)
    st = _get_settlement(db_session, cycle_114, emp_tsai)
    assert st.gross_amount == Decimal("37015.20")
    assert st.subtotal_amount == Decimal("30944.71")
    assert st.payable_amount == Decimal("29044.71")
    assert st.total_amount == Decimal("40106.71")


def test_build_idempotent(db_session, seed_cycle_114, seed_tsai):
    sb.build_settlements(db_session, 114, set(), 1)
    sb.build_settlements(db_session, 114, set(), 1)   # 再跑一次
    rows = _all_settlements(db_session, cycle_114)
    assert len(rows) == 1   # 不重複


def test_build_skips_finalized(db_session, seed_cycle_114, seed_tsai):
    sb.build_settlements(db_session, 114, set(), 1)
    _finalize(db_session, cycle_114, emp_tsai)
    # 改 special 後重跑：FINALIZED 那筆不被覆寫
    res = sb.build_settlements(db_session, 114, set(), 1)
    assert res.skipped_finalized == 1
```

> seed fixtures（`seed_cycle_114` 建 cycle+org_settings 兩學期 75.6/91.5+class_targets；`seed_tsai` 建蔡宜倩員工+班導角色+考核 summary 寫好 special_bonus_items）放本測試檔頂部，數字取自 spec 驗收金標準。

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_year_end_settlement_builder.py -k build -v`
Expected: FAIL

- [ ] **Step 3: 實作 `build_settlements`**

```python
# 追加到 settlement_builder.py
from dataclasses import dataclass
from sqlalchemy import func, select, text
from models.year_end import (
    YearEndCycle, OrgYearSettings, ClassEnrollmentTarget,
    EmployeeYearEndSnapshot, YearEndSettlement, YearEndSettlementStatus,
    SpecialBonusItem,
)
from services.year_end import enrollment_rates as er


@dataclass
class BuildResult:
    built: int = 0
    skipped_finalized: int = 0


def _resolve_base_salary(db, emp) -> Decimal:
    """復用月薪引擎底薪解析，保證年終 base = 月薪底薪（決策①A）。"""
    from services.salary.engine import SalaryEngine  # 以實際類別/工廠為準
    eng = SalaryEngine(db)  # 若需注入，改用 main.py 既有工廠取 singleton
    return Decimal(str(eng._resolve_standard_base(emp) or 0))


def build_settlements(db, academic_year, included_resigned_ids, actor_id, *, recompute=True) -> BuildResult:
    # advisory lock 包整段（key = hash('ye_build', academic_year)）
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
               {"k": f"ye_build:{academic_year}"})
    cycle = db.query(YearEndCycle).filter_by(academic_year=academic_year).one()
    org = {o.semester_first: o for o in
           db.query(OrgYearSettings).filter_by(year_end_cycle_id=cycle.id)}
    targets = db.query(ClassEnrollmentTarget).filter_by(year_end_cycle_id=cycle.id).all()
    targets_by_cls = {(t.semester_first, t.classroom_id): t for t in targets}

    result = BuildResult()
    for emp in _participants(db, cycle, included_resigned_ids):
        existing = db.query(YearEndSettlement).filter_by(
            year_end_cycle_id=cycle.id, employee_id=emp.id).one_or_none()
        if existing and existing.status == YearEndSettlementStatus.FINALIZED:
            result.skipped_finalized += 1
            continue

        base = _resolve_base_salary(db, emp)
        role_key = _role_key_of(emp)
        festival = festival_base_for_role(db, role_key)
        hire_months = compute_hire_months(emp, cycle.start_date, cycle.end_date)
        worked_first, worked_second = _worked_semesters(emp, cycle)

        rates = _gather_performance_rates(db, cycle, emp, org, targets_by_cls,
                                          worked_first, worked_second)
        org_rate = resolve_org_achievement_rate(
            org[True].school_achievement_rate if True in org else None,
            org[False].school_achievement_rate if False in org else None,
            worked_first=worked_first, worked_second=worked_second)
        deductions = _gather_deductions(db, cycle, emp)   # 階段1：讀既有手填值（預設 0）
        special_total = db.scalar(
            select(func.coalesce(func.sum(SpecialBonusItem.amount), 0)).where(
                SpecialBonusItem.year_end_cycle_id == cycle.id,
                SpecialBonusItem.employee_id == emp.id)) or 0

        computed = compute_settlement(
            base_salary=base, festival_total=festival, performance_rates=rates,
            org_achievement_rate=org_rate, deductions=deductions,
            hire_months=hire_months, special_bonus_total=Decimal(special_total))

        snap = _upsert_snapshot(db, cycle, emp, base, festival, hire_months)
        _upsert_settlement(db, cycle, emp, snap, rates, org_rate, deductions, computed)
        result.built += 1

    _write_audit(db, actor_id, academic_year, result)
    return result
```

> `_participants` / `_role_key_of` / `_worked_semesters` / `_gather_performance_rates` / `_gather_deductions` / `_upsert_snapshot` / `_upsert_settlement` / `_write_audit` 在同檔實作：
> - `_gather_performance_rates`：階段 1 全校達成率/班級經營績效用 `enrollment_rates`（自動）；班舊生率讀 `ClassEnrollmentTarget.returning_student_rate`（手填）。某對缺值傳 None（引擎自動跳過）。
> - `_gather_deductions`：階段 1 從既有 settlement 手填欄沿用（無則 0），回 `DeductionBreakdown`。
> - `_upsert_settlement`：把 computed 各 step 值 + rates + org_rate + deductions 寫入；status 維持既有或 DRAFT；FINALIZED 不進此函式。
> - `_role_key_of`：員工職務 → `_FESTIVAL_FIELD` key + `_resolve_standard_base` 用的職位 key，沿用月薪引擎 `_resolve_standard_base` 內的對應（避免兩套）。

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_year_end_settlement_builder.py -v`
Expected: PASS（蔡宜倩 40106.71 對上）

- [ ] **Step 5: Commit**

```bash
git add services/year_end/settlement_builder.py tests/test_year_end_settlement_builder.py
git commit -m "feat(year-end): build_settlements 跨員工跑引擎+upsert（idempotent/skip finalized）"
```

---

## Task 4: 修正考核學期（決策②）+ docstring 清理

**Files:**
- Modify: `services/year_end/appraisal_sync.py:80`（`resolve_target_cycles`）
- Modify: `models/year_end.py:79-80`（`SpecialBonusType` docstring）
- Test: `tests/test_year_end_appraisal_refactor.py`

決策②：考核改抓「前一完整學年上+下」。114 年度年終（payout civil 2026）→ academic 113 的 FIRST(上) + SECOND(下)。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_year_end_appraisal_refactor.py
from services.year_end import appraisal_sync as asy


def test_resolve_target_cycles_prev_full_year(db_session, seed_appraisal_cycles):
    # payout 2026 → 抓 academic 113 上 + 113 下（非 113下+114上）
    earlier, later = asy.resolve_target_cycles(db_session, 2026)
    assert (earlier.academic_year, earlier.semester.value) == (113, "FIRST")   # 113上
    assert (later.academic_year, later.semester.value) == (113, "SECOND")      # 113下
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_year_end_appraisal_refactor.py::test_resolve_target_cycles_prev_full_year -v`
Expected: FAIL（現回 113下+114上）

- [ ] **Step 3: 修改 `resolve_target_cycles`**

讀 `services/year_end/appraisal_sync.py:80` 現有實作，改為：target academic year = `civil_year_to_target_academic_year(payout_year) - 1`（114→113），earlier=該學年 FIRST(上)、later=該學年 SECOND(下)。同步調整 `map_period_label`（"113上"/"113下"）與 `generate_payouts` 內 bonus_type↔cycle 對應（FIRST_BONUS←上、SECOND_BONUS←下）。
更新 `models/year_end.py:79-80` docstring：**移除**「FIRST/SECOND 為時間順序、與學期相反」的反轉警語（決策②後 FIRST=上=較早、與學期一致，反轉已不存在）。

- [ ] **Step 4: 跑測試確認通過 + 既有 appraisal 測試不回歸**

Run: `pytest tests/test_year_end_appraisal_refactor.py tests/test_year_end_appraisal_sync.py -v`
Expected: PASS（既有測試若鎖舊 mapping，依決策②更新其 expected）

- [ ] **Step 5: Commit**

```bash
git add services/year_end/appraisal_sync.py models/year_end.py tests/test_year_end_appraisal_refactor.py
git commit -m "fix(year-end): 考核年終改抓前一完整學年上+下（決策②）+ 清理過時 docstring"
```

---

## Task 5: 移除 salary engine 2 月考核 pull（決策⑥B）

**Files:**
- Modify: `services/salary/engine.py:55, 234-237`
- Modify: `services/salary/appraisal_year_end.py`（標 deprecated）
- Test: `tests/test_year_end_appraisal_refactor.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_salary_engine_no_longer_pulls_appraisal(db_session, seed_feb_salary_ctx):
    """2 月 calculate 後 SalaryRecord.appraisal_year_end_bonus 恆 0（考核改走年終）。"""
    rec = run_calculate(db_session, employee_id=emp.id, year=2026, month=2)
    assert rec.appraisal_year_end_bonus == Decimal("0")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_year_end_appraisal_refactor.py::test_salary_engine_no_longer_pulls_appraisal -v`
Expected: FAIL（現會 pull 出非 0）

- [ ] **Step 3: 移除 pull**

`services/salary/engine.py`：刪除 line 55 的 `from services.salary.appraisal_year_end import query_appraisal_year_end_bonus`；line 234-237 改為固定 `salary_record.appraisal_year_end_bonus = Decimal(0)`（保留 column 向後相容，不進 gross）。`services/salary/appraisal_year_end.py` 函式加 deprecation docstring（保留避免他處 import 爆，但不再被 engine 呼叫）。

- [ ] **Step 4: 跑測試確認通過 + salary 套件不回歸**

Run: `pytest tests/test_year_end_appraisal_refactor.py tests/test_salary_appraisal_year_end_plugin.py -v`
Expected: PASS（既有 plugin 測試若驗「2 月帶款」，依決策⑥B 更新為 0 + 註記）

- [ ] **Step 5: Commit**

```bash
git add services/salary/engine.py services/salary/appraisal_year_end.py tests/test_year_end_appraisal_refactor.py
git commit -m "refactor(salary): 移除 2 月考核 pull，考核改由年終發放（決策⑥B）"
```

---

## Task 6: API 端點 — build-settlements / grid / manual-patch

**Files:**
- Modify: `api/year_end/__init__.py`
- Modify: `schemas/year_end.py`
- Test: `tests/test_year_end_grid_api.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_year_end_grid_api.py
def test_build_settlements_endpoint(client, admin_headers, seed_cycle_114):
    r = client.post(f"/api/year_end/cycles/{cycle_114}/build-settlements",
                    json={"included_resigned_employee_ids": []}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["built"] >= 1


def test_grid_endpoint_shape(client, admin_headers, seed_built_114):
    r = client.get(f"/api/year_end/cycles/{cycle_114}/grid", headers=admin_headers)
    rows = r.json()
    assert {"employee_name", "payable_amount", "special_bonuses", "total_amount"} <= rows[0].keys()


def test_manual_patch_recomputes(client, admin_headers, seed_built_114):
    r = client.patch(f"/api/year_end/settlements/{st_id}/manual",
                     json={"deduction_disciplinary": -6000}, headers=admin_headers)
    assert r.status_code == 200
    assert Decimal(r.json()["total_amount"]) == expected_after_minus_6000


def test_build_requires_write_permission(client, readonly_headers, seed_cycle_114):
    r = client.post(f"/api/year_end/cycles/{cycle_114}/build-settlements",
                    json={"included_resigned_employee_ids": []}, headers=readonly_headers)
    assert r.status_code == 403
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_year_end_grid_api.py -v`
Expected: FAIL（端點不存在）

- [ ] **Step 3: 實作端點 + schema**

`schemas/year_end.py` 加：`BuildSettlementsRequest{included_resigned_employee_ids: list[int]}`、`BuildResultOut{built:int, skipped_finalized:int}`、`GridRowOut{employee_id, employee_name, payable_amount, special_bonuses: dict[str,Decimal], total_amount, status}`、`ManualPatchRequest{deduction_disciplinary?:Decimal, excess_amount?:Decimal}`。全部 `response_model=` 標好（前端 OpenAPI 落地）。
`api/year_end/__init__.py` 加 3 端點（權限見下表），呼叫 `settlement_builder.build_settlements` / grid 查詢 / manual patch（改 `deduction_disciplinary` 或 upsert `EXCESS_ENROLLMENT` special_bonus → 重算該員 settlement）。

| Method | Path | Permission |
|---|---|---|
| POST | `/cycles/{id}/build-settlements` | `YEAR_END_WRITE` |
| GET | `/cycles/{id}/grid` | `YEAR_END_READ` |
| PATCH | `/settlements/{id}/manual` | `YEAR_END_WRITE` |

manual patch 重算：改值後對該員重跑 `compute_settlement`（FINALIZED 則 409）。

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_year_end_grid_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/year_end/__init__.py schemas/year_end.py tests/test_year_end_grid_api.py
git commit -m "feat(year-end): build-settlements / grid / manual-patch 端點 + schema"
```

---

## Task 7: 簽核改兩關（會計→老闆）

**Files:**
- Modify: `api/year_end/__init__.py:231`（`sign_accounting`：允許從 DRAFT 直接簽，跳過 supervisor）
- Test: `tests/test_year_end_grid_api.py`

決策：兩關 = `DRAFT → ACCOUNTING_SIGNED（會計）→ FINALIZED（老闆）`。略過 `SUPERVISOR_SIGNED`。

- [ ] **Step 1: 寫失敗測試**

```python
def test_two_gate_signoff(client, accountant_headers, owner_headers, seed_built_114):
    # 會計可從 DRAFT 直接簽（不需先 supervisor）
    r1 = client.post(f"/api/year_end/settlements/{st_id}/sign_accounting", headers=accountant_headers)
    assert r1.status_code == 200 and r1.json()["status"] == "ACCOUNTING_SIGNED"
    r2 = client.post(f"/api/year_end/settlements/{st_id}/finalize", headers=owner_headers)
    assert r2.status_code == 200 and r2.json()["status"] == "FINALIZED"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_year_end_grid_api.py::test_two_gate_signoff -v`
Expected: FAIL（現 sign_accounting 要求先 SUPERVISOR_SIGNED，見 `:246`）

- [ ] **Step 3: 修改 sign_accounting 守衛**

`api/year_end/__init__.py:231-256`：把「非主管已簽」前置條件改為「status == DRAFT or SUPERVISOR_SIGNED」皆可進入 ACCOUNTING_SIGNED（向後相容保留 supervisor 路徑，但不強制）。`finalize_settlement`（:261）維持需 ACCOUNTING_SIGNED。

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_year_end_grid_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/year_end/__init__.py tests/test_year_end_grid_api.py
git commit -m "feat(year-end): 簽核簡化為兩關（會計可從 DRAFT 直接簽→老闆 finalize）"
```

---

## Task 8: 端到端對帳金標準（蔡宜倩 + 林姿妙）

**Files:**
- Modify: `tests/test_year_end_settlement_builder.py`

- [ ] **Step 1: 寫對帳測試（林姿妙 outlier）**

```python
def test_build_settlement_matches_excel_lin(db_session, seed_cycle_114, seed_lin):
    """林姿妙（資深班導）：base=45499（個人含年資，非 36160 職位標準）,
       festival=2000, avg=85.3, org=83.6, 無扣款,
       special=1165（紅利上500+才藝665）→ total=35036.92"""
    sb.build_settlements(db_session, 114, set(), 1)
    st = _get_settlement(db_session, cycle_114, emp_lin)
    assert st.base_salary == Decimal("45499")     # 驗決策①A：資深取個人底薪
    assert st.payable_amount == Decimal("33871.92")
    assert st.total_amount == Decimal("35036.92")
```

- [ ] **Step 2: 跑測試確認失敗 → 實作對齊 → 通過**

Run: `pytest tests/test_year_end_settlement_builder.py -v`
Expected: 先 FAIL（若 base 解析未取個人底薪）→ 修 `_resolve_base_salary` 對齊 → PASS

> 林姿妙必須拿到 45499 而非 36160，驗證決策①A「資深=個人含年資」走得通。若 `_resolve_standard_base` 對其職位回傳職位標準（36160），表示該員需設「強制個人底薪」flag（`employee.py:158`）——在 seed fixture 設好，並在 plan 註記 rollout 時 HR 須確認資深員工此 flag。

- [ ] **Step 3: 跑後端聚焦套件確認零回歸**

Run: `pytest tests/test_year_end_*.py tests/test_salary_*.py -v`
Expected: PASS（相對 main 無新增 fail）

- [ ] **Step 4: Commit**

```bash
git add tests/test_year_end_settlement_builder.py
git commit -m "test(year-end): 端到端對帳 Excel 金標準（蔡宜倩/林姿妙逐人吻合）"
```

---

## 收尾（非 TDD step，rollout 提醒）

- [ ] 更新 workspace `CLAUDE.md` #11：移除 `appraisal_year_end_bonus` 於二代健保累計名單，註明「年終獨立轉帳、補充保費表外」（決策⑥B）。
- [ ] `python scripts/dump_openapi.py` → 前端 `npm run gen:api`（前端計畫用）。
- [ ] rollout 前 USER 確認：資深員工「強制個人底薪」flag 已設；prod 無舊路 2 月考核累計需回算。

---

## Self-Review 註記（已對 spec §3/§5/§10 逐項核對）

- spec §5 七項輸入：base①✅(Task3)、節慶④✅(Task2)、全校達成率✅(Task1)、機構比率✅(Task2)、班級績效✅(Task1)、考核②✅(Task4)、到職✅(Task2)；班舊生率/考勤扣款=階段1手填(Task3 `_gather_*` 沿用)，符合 spec §3.2。
- 決策①-⑥：①Task3/8、②Task4、③Task1、④Task2、⑤=設定承襲屬前端/設定頁（前端計畫）、⑥Task5。
- 未含 frontend（另開計畫）、未含設定頁 copy-from-previous（決策⑤，前端/設定計畫）。
