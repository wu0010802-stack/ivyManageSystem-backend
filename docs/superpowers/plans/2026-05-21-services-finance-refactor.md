# services/finance 抽出 PR1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 finance domain 的 10 個 `services/*.py` + 3 個 `utils/*.py` 搬進新建的 `services/finance/` 子套件，**原位保留 compat shim**，零行為變更、零 import path 改動、零下游檔案受影響。

**Architecture:** 純結構 refactor。`git mv` 13 個檔到 `services/finance/`，原位寫 6 行 shim re-export 公開符號。`services/finance/__init__.py` 必須空（避免 circular import）。所有 50+ import site 維持原路徑，透過 shim 走，PR2（不在此 plan）才掃改 import + 刪 shim。

**Tech Stack:** Python 3 + FastAPI；pytest 後端測試套（baseline 4486 tests）。

**Spec:** `docs/superpowers/specs/2026-05-21-services-finance-refactor-design.md`

**Worktree 慣例**：建議在 `ivy-backend/.claude/worktrees/services-finance-refactor-2026-05-21-backend` 開新 worktree，branch `feat/services-finance-refactor-2026-05-21-backend`。

---

## File Structure

**新建 1 檔**：
- `services/finance/__init__.py`（空檔）

**搬遷 13 檔**（`git mv`）：

| 原路徑 | 新路徑 |
|---|---|
| `services/salary_engine.py` | `services/finance/salary_engine.py` |
| `services/salary_slip.py` | `services/finance/salary_slip.py` |
| `services/salary_job_registry.py` | `services/finance/salary_job_registry.py` |
| `services/salary_field_breakdown.py` | `services/finance/salary_field_breakdown.py` |
| `services/salary_logic_info.py` | `services/finance/salary_logic_info.py` |
| `services/art_teacher_payroll.py` | `services/finance/art_teacher_payroll.py` |
| `services/fee_refund_calculator.py` | `services/finance/fee_refund_calculator.py` |
| `services/monthly_pnl_service.py` | `services/finance/monthly_pnl_service.py` |
| `services/finance_reconciliation_service.py` | `services/finance/finance_reconciliation_service.py` |
| `services/salary_snapshot_service.py` | `services/finance/salary_snapshot_service.py` |
| `utils/finance_cache.py` | `services/finance/finance_cache.py` |
| `utils/finance_guards.py` | `services/finance/finance_guards.py` |
| `utils/salary_access.py` | `services/finance/salary_access.py` |

**新建 13 個 shim**（在原路徑）：每個 shim 5-25 行純 re-export，無邏輯。

**不動**：`main.py`、`alembic/`、所有 `api/*.py`、所有 `tests/*.py`、`services/salary/`、`services/{appraisal,approval,analytics,gov_data,gov_moe,notification,year_end}/`、其他 `utils/*.py`。

---

## Task 0：Pre-flight 驗證 baseline

**Files:** 不動任何檔，只跑驗證。

- [ ] **Step 1: 確認在乾淨的 worktree / branch 上**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
git status
git branch --show-current
git log -1 --oneline
```

Expected：
- `git status` 顯示 clean working tree（如有未 commit 修改，先停下與 user 確認）
- 在 `feat/services-finance-refactor-2026-05-21-backend` branch 上

**若不在該 branch**：依 `superpowers:using-git-worktrees` skill 開新 worktree：
```bash
cd /Users/yilunwu/Desktop/ivy-backend
git worktree add .claude/worktrees/services-finance-refactor-2026-05-21-backend -b feat/services-finance-refactor-2026-05-21-backend main
cd .claude/worktrees/services-finance-refactor-2026-05-21-backend
```
然後在新 worktree 內繼續後續 Task。

- [ ] **Step 2: 確認 13 個目標檔目前未被任何並行 worktree touch**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
for f in services/salary_engine.py services/salary_slip.py services/salary_job_registry.py services/salary_field_breakdown.py services/salary_logic_info.py services/art_teacher_payroll.py services/fee_refund_calculator.py services/monthly_pnl_service.py services/finance_reconciliation_service.py services/salary_snapshot_service.py utils/finance_cache.py utils/finance_guards.py utils/salary_access.py; do
  hits=$(git log main..feat/config-centralization-phase1-2026-05-21-backend main..feat/jwt-secret-rotation-2026-05-21-backend main..feat/scheduler-leader-election-2026-05-21-backend --name-only --pretty=format: 2>/dev/null | sort -u | grep "^$f$" | wc -l | tr -d ' ')
  echo "$f -- worktree touches: $hits"
done
```

Expected：13 行全部 `-- worktree touches: 0`。任何一條 ≠ 0 立刻停下與 user 對齊（spec 假設失效）。

- [ ] **Step 3: 確認 13 檔 `__all__` 狀態**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
for f in services/salary_engine.py services/salary_slip.py services/salary_job_registry.py services/salary_field_breakdown.py services/salary_logic_info.py services/art_teacher_payroll.py services/fee_refund_calculator.py services/monthly_pnl_service.py services/finance_reconciliation_service.py services/salary_snapshot_service.py utils/finance_cache.py utils/finance_guards.py utils/salary_access.py; do
  if grep -q "^__all__" "$f"; then echo "$f: __all__ DEFINED"; else echo "$f: no __all__"; fi
done
```

Expected：只有 `services/salary_engine.py: __all__ DEFINED`，其餘 12 行 `no __all__`。

> **本 plan 的 shim 設計已對應此狀態**：`salary_engine.py` 的 `__all__` 已涵蓋所有 private symbol（含 `_sum_leave_deduction` 等），故所有 13 檔 shim 都採「顯式列 import」寫法（不依賴 `from X import *`），避免任何 `__all__` 邊角案例。

- [ ] **Step 4: 跑 baseline pytest 全套確認綠**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest -x --tb=short 2>&1 | tail -30
```

Expected：`==== N passed, M skipped in X.Xs ====`。若有 fail，記錄 baseline fail 清單（spec §4.1 假設 4486 tests passed；user memory 提到 3 條 pre-existing fail on `test_audit_router` / `test_supabase_storage`，這些可接受）。**baseline 紅的 test 不算我們的責任，但要記錄起來，Task 15 對照確認沒新增紅**。

把 baseline 數字寫入 `.scratch/services_finance_refactor_baseline.txt`：

```bash
mkdir -p /Users/yilunwu/Desktop/ivyManageSystem/.scratch
pytest --tb=no -q 2>&1 | tail -5 > /Users/yilunwu/Desktop/ivyManageSystem/.scratch/services_finance_refactor_baseline.txt
cat /Users/yilunwu/Desktop/ivyManageSystem/.scratch/services_finance_refactor_baseline.txt
```

---

## Task 1：建 `services/finance/` 空子套件

**Files:**
- Create: `services/finance/__init__.py`

- [ ] **Step 1: 建空 `__init__.py`**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
mkdir -p services/finance
```

Write file `services/finance/__init__.py`（**完全空，連 module docstring 都不放**，避免任何副作用觸發）：

```python
```

> **為什麼空**：呼叫者一律走 `from services.finance.<module> import X` 顯式形式。`__init__.py` 不做 auto-import 子模組（避免 circular import：salary_engine 會 import finance_cache 等同套件兄弟檔，若 `__init__` 強制加載順序，會出意外）。

- [ ] **Step 2: 驗證 package 可載入**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
python -c "import services.finance; print('finance pkg ok')"
```

Expected：`finance pkg ok`

- [ ] **Step 3: 暫存（不 commit，最後 Task 17 一次 commit）**

Run:
```bash
git add services/finance/__init__.py
git status --short
```

Expected：`A  services/finance/__init__.py`

---

## Task 2：搬 `services/salary_engine.py` + shim

**Files:**
- Move: `services/salary_engine.py` → `services/finance/salary_engine.py`
- Create: `services/salary_engine.py`（shim）

- [ ] **Step 1: git mv**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/salary_engine.py services/finance/salary_engine.py
git status --short
```

Expected：`R  services/salary_engine.py -> services/finance/salary_engine.py`

- [ ] **Step 2: 寫 shim 到原位**

Write file `services/salary_engine.py`（內容如下）：

```python
"""Compat shim — moved to services.finance.salary_engine. Remove in PR2 (post-merge of 3 parallel worktrees: config-centralization, jwt-secret-rotation, scheduler-leader-election)."""
from services.finance.salary_engine import (  # noqa: F401
    SalaryEngine,
    SalaryBreakdown,
    _compute_hourly_daily_hours,
    _calc_lunch_overlap_hours,
    _calc_daily_hourly_pay,
    get_working_days,
    get_bonus_distribution_month,
    get_meeting_deduction_period_start,
    calc_daily_salary,
    MONTHLY_BASE_DAYS,
    MAX_DAILY_WORK_HOURS,
    HOURLY_OT1_RATE,
    HOURLY_OT2_RATE,
    HOURLY_REGULAR_HOURS,
    HOURLY_OT1_CAP_HOURS,
    LEAVE_DEDUCTION_RULES,
    _prorate_base_salary,
    _prorate_for_period,
    _build_expected_workdays,
    _sum_leave_deduction,
)
```

- [ ] **Step 3: 單檔 import smoke**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
python -c "from services.salary_engine import SalaryEngine, SalaryBreakdown, _sum_leave_deduction, _calc_lunch_overlap_hours, _compute_hourly_daily_hours, get_working_days, MONTHLY_BASE_DAYS; print('salary_engine shim ok')"
python -c "from services.finance.salary_engine import SalaryEngine; print('new path ok')"
```

Expected：兩行皆印 `... ok`。任何 ImportError 立刻停下檢查 shim 名單。

- [ ] **Step 4: git add 暫存**

Run:
```bash
git add services/salary_engine.py services/finance/salary_engine.py
git status --short | head -5
```

Expected：含 `A services/salary_engine.py`（shim）+ `R services/salary_engine.py -> services/finance/salary_engine.py`（git mv 已偵測 rename）。

---

## Task 3：搬 `services/salary_slip.py` + shim

**Files:**
- Move: `services/salary_slip.py` → `services/finance/salary_slip.py`
- Create: `services/salary_slip.py`（shim）

- [ ] **Step 1: git mv**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/salary_slip.py services/finance/salary_slip.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/salary_slip.py`：

```python
"""Compat shim — moved to services.finance.salary_slip. Remove in PR2."""
from services.finance.salary_slip import (  # noqa: F401
    generate_salary_pdf,
    generate_salary_excel,
    generate_salary_all_pdf,
    _build_deduction_rows,
)
```

- [ ] **Step 3: import smoke**

Run:
```bash
python -c "from services.salary_slip import generate_salary_pdf, generate_salary_excel, generate_salary_all_pdf, _build_deduction_rows; from services.finance.salary_slip import generate_salary_pdf as _; print('salary_slip shim ok')"
```

Expected：`salary_slip shim ok`

- [ ] **Step 4: git add**

```bash
git add services/salary_slip.py services/finance/salary_slip.py
```

---

## Task 4：搬 `services/salary_job_registry.py` + shim

**Files:**
- Move: `services/salary_job_registry.py` → `services/finance/salary_job_registry.py`
- Create: `services/salary_job_registry.py`（shim）

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/salary_job_registry.py services/finance/salary_job_registry.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/salary_job_registry.py`：

```python
"""Compat shim — moved to services.finance.salary_job_registry. Remove in PR2."""
from services.finance.salary_job_registry import (  # noqa: F401
    registry,
    SalaryCalcJob,
    ActiveJobExistsError,
    _JOB_TTL_SEC,
    _SalaryJobRegistry,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from services.salary_job_registry import registry, SalaryCalcJob, _SalaryJobRegistry; from services.finance.salary_job_registry import registry as _; print('salary_job_registry shim ok')"
```

Expected：`salary_job_registry shim ok`

- [ ] **Step 4: git add**

```bash
git add services/salary_job_registry.py services/finance/salary_job_registry.py
```

---

## Task 5：搬 `services/salary_field_breakdown.py` + shim

**Files:**
- Move: `services/salary_field_breakdown.py` → `services/finance/salary_field_breakdown.py`
- Create: `services/salary_field_breakdown.py`（shim）

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/salary_field_breakdown.py services/finance/salary_field_breakdown.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/salary_field_breakdown.py`：

```python
"""Compat shim — moved to services.finance.salary_field_breakdown. Remove in PR2."""
from services.finance.salary_field_breakdown import (  # noqa: F401
    build_salary_debug_snapshot,
    build_field_breakdown,
    FIELD_LABELS,
    QUARTERLY_DEDUCTION_MONTHS,
    _calc_attendance_stats,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from services.salary_field_breakdown import build_salary_debug_snapshot, build_field_breakdown, FIELD_LABELS, QUARTERLY_DEDUCTION_MONTHS, _calc_attendance_stats; print('salary_field_breakdown shim ok')"
```

Expected：`salary_field_breakdown shim ok`

- [ ] **Step 4: git add**

```bash
git add services/salary_field_breakdown.py services/finance/salary_field_breakdown.py
```

---

## Task 6：搬 `services/salary_logic_info.py` + shim

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/salary_logic_info.py services/finance/salary_logic_info.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/salary_logic_info.py`：

```python
"""Compat shim — moved to services.finance.salary_logic_info. Remove in PR2."""
from services.finance.salary_logic_info import (  # noqa: F401
    build_salary_logic_info,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from services.salary_logic_info import build_salary_logic_info; print('salary_logic_info shim ok')"
```

Expected：`salary_logic_info shim ok`

- [ ] **Step 4: git add**

```bash
git add services/salary_logic_info.py services/finance/salary_logic_info.py
```

> **特別注意**：`tests/test_salary_logic_endpoint.py:310,313` 字串字面值 `"from services.salary_logic_info import build_salary_logic_info" in src_dev` 是檢查 `api/dev.py` 程式碼字面內容，**該 test 期望舊路徑字串還在**。本 PR1 因 shim 在原位，`api/dev.py:14` 仍是 `from services.salary_logic_info import build_salary_logic_info`，這個 test 不需動。**PR2 改完 import path 後該 test 字串要同步改**，這是 PR2 範疇。

---

## Task 7：搬 `services/art_teacher_payroll.py` + shim

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/art_teacher_payroll.py services/finance/art_teacher_payroll.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/art_teacher_payroll.py`：

```python
"""Compat shim — moved to services.finance.art_teacher_payroll. Remove in PR2."""
from services.finance.art_teacher_payroll import (  # noqa: F401
    compute_total_for_month,
    generate_art_teacher_roster_xlsx,
    list_entries_for_month,
    recompute_entry_amounts,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from services.art_teacher_payroll import compute_total_for_month, generate_art_teacher_roster_xlsx, list_entries_for_month, recompute_entry_amounts; print('art_teacher_payroll shim ok')"
```

Expected：`art_teacher_payroll shim ok`

- [ ] **Step 4: git add**

```bash
git add services/art_teacher_payroll.py services/finance/art_teacher_payroll.py
```

---

## Task 8：搬 `services/fee_refund_calculator.py` + shim

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/fee_refund_calculator.py services/finance/fee_refund_calculator.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/fee_refund_calculator.py`：

```python
"""Compat shim — moved to services.finance.fee_refund_calculator. Remove in PR2."""
from services.finance.fee_refund_calculator import (  # noqa: F401
    calc_enrollment_refund,
    calc_monthly_refund,
    longest_consecutive_workdays,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from services.fee_refund_calculator import calc_enrollment_refund, calc_monthly_refund, longest_consecutive_workdays; print('fee_refund_calculator shim ok')"
```

Expected：`fee_refund_calculator shim ok`

- [ ] **Step 4: git add**

```bash
git add services/fee_refund_calculator.py services/finance/fee_refund_calculator.py
```

---

## Task 9：搬 `services/monthly_pnl_service.py` + shim

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/monthly_pnl_service.py services/finance/monthly_pnl_service.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/monthly_pnl_service.py`：

```python
"""Compat shim — moved to services.finance.monthly_pnl_service. Remove in PR2."""
from services.finance.monthly_pnl_service import (  # noqa: F401
    build_monthly_pnl,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from services.monthly_pnl_service import build_monthly_pnl; print('monthly_pnl_service shim ok')"
```

Expected：`monthly_pnl_service shim ok`

- [ ] **Step 4: git add**

```bash
git add services/monthly_pnl_service.py services/finance/monthly_pnl_service.py
```

---

## Task 10：搬 `services/finance_reconciliation_service.py` + shim

> **注意**：`services/finance_reconciliation_scheduler.py:71` 用 deferred import `from services.finance_reconciliation_service import (...)`，且該 scheduler 被 `config-centralization` worktree 動到。**這檔 scheduler 本身不在我們 13 檔內，shim 兼容它**。

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/finance_reconciliation_service.py services/finance/finance_reconciliation_service.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/finance_reconciliation_service.py`：

```python
"""Compat shim — moved to services.finance.finance_reconciliation_service. Remove in PR2."""
from services.finance.finance_reconciliation_service import (  # noqa: F401
    detect_paid_amount_mismatches,
    format_mismatches_for_line,
    PaidAmountMismatch,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from services.finance_reconciliation_service import detect_paid_amount_mismatches, format_mismatches_for_line, PaidAmountMismatch; print('finance_reconciliation_service shim ok')"
```

Expected：`finance_reconciliation_service shim ok`

- [ ] **Step 4: git add**

```bash
git add services/finance_reconciliation_service.py services/finance/finance_reconciliation_service.py
```

---

## Task 11：搬 `services/salary_snapshot_service.py` + shim

> **注意**：除了 `from services.salary_snapshot_service import ...` 形式，還有呼叫者用 `from services import salary_snapshot_service as snap_svc`（`tests/test_salary_snapshot.py:32`、`api/salary/snapshots.py:21`、`api/salary/__init__.py:70`）。**shim 本身是 module，自動相容 module-form import**，不必額外處理。

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv services/salary_snapshot_service.py services/finance/salary_snapshot_service.py
```

- [ ] **Step 2: 寫 shim**

Write file `services/salary_snapshot_service.py`：

```python
"""Compat shim — moved to services.finance.salary_snapshot_service. Remove in PR2."""
from services.finance.salary_snapshot_service import (  # noqa: F401
    create_month_end_snapshots,
    create_finalize_snapshot,
    create_manual_snapshot,
    diff_with_current,
    get_snapshot_detail,
    list_snapshots,
    run_month_end_snapshots_job,
    SNAPSHOT_TYPES,
    _PAYLOAD_COLUMNS,
    _SNAPSHOT_META_FIELDS,
)
```

- [ ] **Step 3: import smoke（含 module-form）**

```bash
python -c "from services.salary_snapshot_service import create_month_end_snapshots, SNAPSHOT_TYPES; from services import salary_snapshot_service as snap_svc; assert hasattr(snap_svc, 'create_month_end_snapshots'); print('salary_snapshot_service shim ok')"
```

Expected：`salary_snapshot_service shim ok`

- [ ] **Step 4: git add**

```bash
git add services/salary_snapshot_service.py services/finance/salary_snapshot_service.py
```

---

## Task 12：搬 `utils/finance_cache.py` + shim

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv utils/finance_cache.py services/finance/finance_cache.py
```

- [ ] **Step 2: 寫 shim**

Write file `utils/finance_cache.py`：

```python
"""Compat shim — moved to services.finance.finance_cache. Remove in PR2."""
from services.finance.finance_cache import (  # noqa: F401
    invalidate_finance_summary_cache,
    FINANCE_SUMMARY_CACHE_CATEGORY,
    MONTHLY_PNL_CACHE_CATEGORY,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from utils.finance_cache import invalidate_finance_summary_cache, FINANCE_SUMMARY_CACHE_CATEGORY, MONTHLY_PNL_CACHE_CATEGORY; from services.finance.finance_cache import invalidate_finance_summary_cache as _; print('finance_cache shim ok')"
```

Expected：`finance_cache shim ok`

- [ ] **Step 4: git add**

```bash
git add utils/finance_cache.py services/finance/finance_cache.py
```

---

## Task 13：搬 `utils/finance_guards.py` + shim

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv utils/finance_guards.py services/finance/finance_guards.py
```

- [ ] **Step 2: 寫 shim**

Write file `utils/finance_guards.py`：

```python
"""Compat shim — moved to services.finance.finance_guards. Remove in PR2."""
from services.finance.finance_guards import (  # noqa: F401
    has_finance_approve,
    require_finance_approve,
    require_adjustment_reason,
    require_not_self_edit,
    require_not_self_salary_record,
    EMPLOYEE_SALARY_SENSITIVE_FIELDS,
    FINANCE_APPROVAL_THRESHOLD,
    MIN_FINANCE_REASON_LENGTH,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from utils.finance_guards import has_finance_approve, require_finance_approve, require_adjustment_reason, require_not_self_edit, require_not_self_salary_record, FINANCE_APPROVAL_THRESHOLD; print('finance_guards shim ok')"
```

Expected：`finance_guards shim ok`

- [ ] **Step 4: git add**

```bash
git add utils/finance_guards.py services/finance/finance_guards.py
```

---

## Task 14：搬 `utils/salary_access.py` + shim

- [ ] **Step 1: git mv**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git mv utils/salary_access.py services/finance/salary_access.py
```

- [ ] **Step 2: 寫 shim**

Write file `utils/salary_access.py`：

```python
"""Compat shim — moved to services.finance.salary_access. Remove in PR2."""
from services.finance.salary_access import (  # noqa: F401
    can_view_salary_of,
    enforce_full_salary_view,
    enforce_self_or_full_salary,
    has_full_salary_view,
    mask_dict_fields,
    resolve_salary_viewer_employee_id,
    FULL_SALARY_ROLES,
)
```

- [ ] **Step 3: import smoke**

```bash
python -c "from utils.salary_access import can_view_salary_of, enforce_self_or_full_salary, has_full_salary_view, mask_dict_fields, FULL_SALARY_ROLES; print('salary_access shim ok')"
```

Expected：`salary_access shim ok`

- [ ] **Step 4: git add**

```bash
git add utils/salary_access.py services/finance/salary_access.py
```

---

## Task 15：清 `__pycache__` + 跑全套 pytest 對齊 baseline

**Files:** 不動任何 source，只清 cache + 跑驗證。

- [ ] **Step 1: 清三個 `__pycache__`**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
rm -rf services/__pycache__ utils/__pycache__ services/finance/__pycache__
find . -type d -name __pycache__ -path "./services/*" -exec rm -rf {} + 2>/dev/null
find . -type d -name __pycache__ -path "./utils/*" -exec rm -rf {} + 2>/dev/null
echo "pycache cleared"
```

Expected：`pycache cleared`

> **為什麼**：spec §6 風險「舊 .pyc 快取導致 import 走錯」。stale `.pyc` 內藏舊 module path 反射，跑起來會看到「明明 shim 寫對但 import 報莫名錯」。

- [ ] **Step 2: 跑全套 import smoke**

Run:
```bash
python -c "
from services.salary_engine import SalaryEngine, _sum_leave_deduction
from services.salary_slip import generate_salary_pdf
from services.salary_job_registry import registry
from services.salary_field_breakdown import build_salary_debug_snapshot
from services.salary_logic_info import build_salary_logic_info
from services.art_teacher_payroll import compute_total_for_month
from services.fee_refund_calculator import calc_enrollment_refund
from services.monthly_pnl_service import build_monthly_pnl
from services.finance_reconciliation_service import detect_paid_amount_mismatches
from services.salary_snapshot_service import create_month_end_snapshots
from services import salary_snapshot_service as snap_svc
from utils.finance_cache import invalidate_finance_summary_cache
from utils.finance_guards import require_finance_approve
from utils.salary_access import can_view_salary_of
# new paths
from services.finance.salary_engine import SalaryEngine as _SE
from services.finance.finance_cache import invalidate_finance_summary_cache as _ifsc
from services.finance.salary_access import can_view_salary_of as _cvs
print('ALL 13 SHIMS + 3 NEW PATHS OK')
"
```

Expected：`ALL 13 SHIMS + 3 NEW PATHS OK`。任何 ImportError 立刻停下對照 shim 對應 task。

- [ ] **Step 3: 跑全套 pytest，對照 baseline**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest --tb=short 2>&1 | tail -30
```

Expected：與 Task 0 Step 4 baseline `.scratch/services_finance_refactor_baseline.txt` 對照，passed 數一致、fail 名單一致（不允許新增 fail）。

- [ ] **Step 4: 若 fail 數新增，停下排查（不繼續 Task 16）**

如果新出現 fail：
1. 找出新紅的 test name
2. `pytest -x <test_name> -vv --tb=long` 看堆疊
3. 99% 機率是 shim 漏 import 某 symbol，回到對應 Task 的 Step 2 補進 shim
4. 不要 push 不要 commit，先解乾淨

---

## Task 16：Uvicorn 啟動驗證 + 端點 smoke

**Files:** 不動任何 source。

- [ ] **Step 1: 背景啟動 uvicorn**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
# 確保 8088 未被佔
lsof -ti:8088 | xargs kill -9 2>/dev/null
# 背景啟動，log 寫檔
uvicorn main:app --port 8088 > /tmp/services_finance_uvicorn.log 2>&1 &
UVICORN_PID=$!
echo "uvicorn pid: $UVICORN_PID"
sleep 8
```

Expected：uvicorn pid 印出。

- [ ] **Step 2: 確認啟動無 import error / scheduler 全拉起**

Run:
```bash
grep -E "ImportError|ModuleNotFoundError|Traceback" /tmp/services_finance_uvicorn.log | head -10
echo "---scheduler enabled?---"
grep -E "scheduler 已啟用|scheduler enabled|started" /tmp/services_finance_uvicorn.log | head -10
echo "---uvicorn ready?---"
grep -E "Uvicorn running on" /tmp/services_finance_uvicorn.log | head -1
```

Expected：
- 第一個 grep 0 行（無 error）
- 第二個 grep 有若干 scheduler 啟用訊息
- 第三個 grep `Uvicorn running on http://127.0.0.1:8088`

若有 ImportError → 停下，殺 uvicorn，回 Task 15 排查。

- [ ] **Step 3: hit OpenAPI 端點確認 router 數量**

Run:
```bash
curl -s http://localhost:8088/openapi.json | python -c "import json, sys; d = json.load(sys.stdin); print('paths:', len(d['paths']))"
```

Expected：`paths: 554`（或與 main 同步現狀；數字本身不重要，**重點是 200 OK 而非 5xx**）。

- [ ] **Step 4: 殺 uvicorn**

Run:
```bash
lsof -ti:8088 | xargs kill -9 2>/dev/null
echo "uvicorn killed"
```

Expected：`uvicorn killed`

---

## Task 17：Commit + 開 PR

**Files:** 純 git 操作。

- [ ] **Step 1: 檢視 staged 變更**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
git status --short
git diff --staged --stat | tail -20
```

Expected：
- 14 個 added（13 shim + `services/finance/__init__.py`）
- 13 個 renamed
- 沒有任何 modified（main.py / api / tests 一律乾淨）

- [ ] **Step 2: 跑 final sanity**

Run:
```bash
git diff --staged --name-only | grep -E "^(api/|tests/|alembic/|main\.py$)" | wc -l
```

Expected：`0`（不允許動到 api/tests/alembic/main.py）。**若非 0 立刻停下**。

- [ ] **Step 3: Commit（1 個 commit 對應 spec PR1）**

Run:
```bash
git commit -m "$(cat <<'EOF'
refactor(services/finance): extract finance subpackage with compat shims (PR1)

把 finance domain 的 10 個 services + 3 個 utils 搬入新建的 services/finance/。
原位保留 compat shim re-export 公開符號，零 import path 變動、零下游檔案受影響。

搬遷檔（13）：
- services/{salary_engine,salary_slip,salary_job_registry,salary_field_breakdown,
  salary_logic_info,art_teacher_payroll,fee_refund_calculator,monthly_pnl_service,
  finance_reconciliation_service,salary_snapshot_service}.py
- utils/{finance_cache,finance_guards,salary_access}.py

設計與 PR2 預告：docs/superpowers/specs/2026-05-21-services-finance-refactor-design.md
實作計畫：docs/superpowers/plans/2026-05-21-services-finance-refactor.md

PR2（單獨 spec）將在 3 個並行 worktree 全 merge 後掃改 50 個 import site 並砍 shim。
EOF
)"
```

Expected：commit 成功，無 pre-commit hook 紅。

- [ ] **Step 4: 停止並回報 user**

**不要自動 push 也不要自動開 PR**。本 PR1 雖然零行為變更，但 push + PR 涉及共享狀態，依本 repo 慣例（CLAUDE.md「DO NOT push to the remote repository unless the user explicitly asks」）需 user 明確指示。

回報內容：
```
PR1 commit 已建立於 worktree branch feat/services-finance-refactor-2026-05-21-backend：
- 13 檔搬入 services/finance/ + 13 個 compat shim
- pytest baseline 對齊（無新增 fail）
- uvicorn 啟動 + OpenAPI smoke 通過

下一步請選擇：
A) push + gh pr create（我幫你開 PR）
B) merge 進 local main 不 push（按 user 慣例）
C) 暫不動，等 3 個並行 worktree 都 merge 後再評估

如選 A，預備 PR title/body 已在 plan Task 17 Step 4 草稿區，可直接執行。
```

PR title/body 草稿（user 選 A 時用）：

Title：
```
refactor(services/finance): extract subpackage with compat shims (PR1)
```

Body：
```
## Summary
- 建 `services/finance/` 子套件並搬入 10 個 finance services + 3 個 finance utils（共 13 檔）
- 原位保留 compat shim re-export 所有公開符號，**零 import path 變動**
- 不動 main.py / api / tests / alembic，所有既有 import 透過 shim 走

## 為什麼分兩個 PR
今天並行有 3 個 backend worktree（config-centralization / jwt-secret-rotation / scheduler-leader-election）動到 utils/auth、main.py、7 個 scheduler。PR1 13 檔與三者**零交集**，shim 兼容老 import 路徑，可與並行工作和平共存。PR2 等三 worktree merge 完再掃改 50 個 import site + 刪 shim。

## Spec & Plan
- Spec: `docs/superpowers/specs/2026-05-21-services-finance-refactor-design.md`
- Plan: `docs/superpowers/plans/2026-05-21-services-finance-refactor.md`

## Test plan
- [x] 13 個檔 import smoke（舊路徑 + 新路徑）
- [x] pytest 全套對齊 baseline（無新增 fail）
- [x] uvicorn 啟動無 ImportError、scheduler 全拉起
- [x] OpenAPI /openapi.json 200 OK、paths 數量不變

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

---

## 完成定義 (Definition of Done)

- [ ] 13 個檔成功 `git mv` 到 `services/finance/`
- [ ] 13 個 shim 寫對（passed import smoke + 全套 pytest）
- [ ] `services/finance/__init__.py` 空檔
- [ ] pytest 結果與 baseline 對齊（無新增 fail）
- [ ] uvicorn 啟動無 ImportError
- [ ] commit 不動 main.py / api / tests / alembic
- [ ] PR 已開，URL 回給 user

---

## 失敗處理

任何 Task 中失敗：
1. **shim ImportError** → 找到 missing symbol，回對應 Task Step 2 補進 import list
2. **pytest 新增 fail** → 99% 機率是 shim 漏 symbol，看 fail traceback 反推
3. **uvicorn ImportError** → 99% 機率是 lifespan 動態 import 沒涵蓋；補 shim 後重啟 uvicorn
4. **發現某檔被並行 worktree touch** → 立刻停下找 user，spec 假設失效

回滾：`git reset --hard HEAD~1`（commit 前）或 `git revert <commit>`（commit 後）。
