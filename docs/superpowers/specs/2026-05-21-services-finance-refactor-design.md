# services/finance 抽出 — PR1 設計（純搬位 + shim）

- **日期**：2026-05-21
- **Repo**：ivy-backend
- **變更類型**：純結構 refactor，零行為變更
- **PR 拆分**：本 spec 只涵蓋 PR1（搬位 + shim）；PR2（改 import + 刪 shim）另寫
- **承接背景**：CLAUDE.md memory 抓到 services/（81 entries）平面化、utils/（51 entries）混雜，`salary_engine.py` 與 `services/salary/` 並存，`finance_cache/finance_guards/salary_access` 散在 utils 但屬 finance domain

---

## 1. 動機與範圍

### 1.1 問題

`ivy-backend/services/` 與 `ivy-backend/utils/` 兩個目錄都已超過健康規模：

| 目錄 | entries | 既有子套件 | 問題 |
|---|---|---|---|
| `services/` | 81 | 8（analytics, appraisal, approval, gov_data, gov_moe, notification, salary, year_end） | 72 個 `.py` 平鋪。`salary_engine.py` 與 `services/salary/` 子套件命名重疊但職責不同（前者是公開 facade，後者是 18 檔內部實作）。7 個 `*_scheduler.py` 與 domain service 同層 |
| `utils/` | 51 | 0 | 完全平面。其中 `finance_cache.py`、`finance_guards.py`、`salary_access.py` 屬 finance domain，不應留在通用 utils |

長期目標：按 bounded context 拆 `services/finance/`、`services/scheduler/`、`services/parent/`、`services/activity/` 等子套件，以及 `utils/auth/`、`utils/io/`、`utils/excel/` 子套件。

### 1.2 本 PR 範圍（PR1）

**只動「最痛、邊界最清、與並行 worktree 零重疊」的 13 個檔案**，組成 `services/finance/` 子套件。

> **為什麼不一次連 scheduler 一起做？**
> 今天 2026-05-21 並行有 3 個 backend worktree：
> - `feat/config-centralization-phase1-2026-05-21-backend`（動 7 scheduler + 9 utils + main.py）
> - `feat/jwt-secret-rotation-2026-05-21-backend`（動 utils/auth.py、utils/audit.py、services/activity_query_token.py）
> - `feat/scheduler-leader-election-2026-05-21-backend`（動 4 scheduler + main.py）
>
> 如果本 PR 動 7 個 scheduler 或前述任何被 touch 的 utils/auth 等檔，3 個 worktree 都要鬼一樣 rebase。下方 §6.1 列出 13 個全 worktree-clean 的檔。

### 1.3 不在本 PR 範圍

- `services/scheduler/` 子套件（等三 worktree 全 merge 後才動）
- `utils/auth/`、`utils/io/`、`utils/excel/` 子套件（同上）
- 既有 `services/salary/` 子套件（內部實作 18 檔，不動，§2.2 解釋為什麼不收 `salary_engine.py` 進去）
- 改 50 個 import site（PR2 做）
- 刪 13 個 shim（PR2 做）
- 改 `main.py` 任何一行（透過 shim 涵蓋）
- 改 alembic / router / test（同上）

---

## 2. 設計決策

### 2.1 全靠 shim 不動 import

PR1 物理搬 13 檔到新位，**原位留 shim 檔（約 6 行）re-export 所有公開符號**。所有既有 import 路徑（`services.salary_engine`、`utils.finance_cache` 等）繼續可用。

理由：
- **零 import diff**：30+ router、main.py、50+ test、3 個並行 worktree 全部不受影響
- **可逆**：PR1 出狀況直接 revert，零下游清理
- **驗證面收斂**：PR1 只驗「shim 把所有 symbol 暴露對」，PR2 才驗「50 個改完的 import path 跑得起來」

trade-off：PR1 diff 看起來「只搬位置不換呼叫者」很怪，但這是與三個並行工作和平共存的唯一順序。

### 2.2 `salary_engine.py` 不收進 `services/salary/engine.py`

`services/salary/` 已存在完整子套件，18 個檔（engine, breakdown, bulk_preload, constants, deduction, festival, finalize_guard, hourly, insurance_salary, minimum_wage, proration, severance, totals, unused_leave_pay, utils）— 這是**拆解後的內部實作**。`salary_engine.py` 是**公開 facade**，被 17+ 處 import（main.py + 大量 test）。

兩者職責不同；強行合併會：
- 命名衝突（`services/salary/engine.py` 已存在另一檔）
- 必須改 17+ import site（違反 §2.1 零 import diff 原則）
- 違反「facade vs 實作分離」清晰度

決策：`services/finance/salary_engine.py` 與 `services/salary/` 並存。`salary_engine.py` 依然 import `services/salary/` 內部模組（既有架構不動）。

### 2.3 `services/finance/__init__.py` 必須空

不在 `__init__.py` 做任何 `from .salary_engine import ...`，避免：
- Circular import：`salary_engine` 可能 import `finance_cache` 等同套件其他檔
- 載入順序敏感性：副作用提早觸發
- 違反「按需 import」原則

呼叫者用 `from services.finance.salary_engine import X` 顯式取，不用 `from services.finance import X`。

### 2.4 shim 寫法（顯式 re-export）

每個 shim 6 行內，pattern：

```python
"""Compat shim — moved to services.finance.<name>. Remove in PR2 (post-merge of 3 parallel worktrees)."""
from services.finance.<name> import *  # noqa: F401, F403
from services.finance.<name> import (  # explicit re-export for tests / private symbols
    _private_symbol_1,
    _private_symbol_2,
)
```

- `import *` 帶所有 public name（含 `SalaryEngine`、`SalaryBreakdown` 等）
- 顯式 import private name（`_sum_leave_deduction` 等被 test 用）— `*` 不帶 `_` 開頭名稱
- 不發 deprecation warning：PR2 一輪即砍，warning 會污染 log 與 test 輸出

對於以 `from services import salary_snapshot_service as snap_svc` 形式 import 的呼叫者，shim 本身仍是 module，自動相容。

---

## 3. 影響盤點

### 3.1 13 個搬遷檔（全 worktree-clean，已驗證）

驗證指令：
```bash
for f in <13 files>; do
  git log main..feat/config-centralization-... main..feat/jwt-secret-rotation-... \
         main..feat/scheduler-leader-election-... --name-only --pretty=format: \
  | sort -u | grep "^$f$" | wc -l
done
# 全為 0
```

| 原路徑 | 新路徑 | 公開符號（shim re-export 範圍） |
|---|---|---|
| `services/salary_engine.py` | `services/finance/salary_engine.py` | `SalaryEngine, SalaryBreakdown, MONTHLY_BASE_DAYS, get_working_days, _sum_leave_deduction, _calc_lunch_overlap_hours, _compute_hourly_daily_hours` |
| `services/salary_slip.py` | `services/finance/salary_slip.py` | `generate_salary_pdf, generate_salary_excel, generate_salary_all_pdf, _build_deduction_rows` |
| `services/salary_job_registry.py` | `services/finance/salary_job_registry.py` | `registry, SalaryCalcJob, _SalaryJobRegistry` |
| `services/salary_field_breakdown.py` | `services/finance/salary_field_breakdown.py` | `build_salary_debug_snapshot, _calc_attendance_stats`（外加 `api/salary/detail.py:25` 與 `api/salary/__init__.py:64` 用括號 import 的 symbols，plan 階段補完整列表） |
| `services/salary_logic_info.py` | `services/finance/salary_logic_info.py` | `build_salary_logic_info` |
| `services/art_teacher_payroll.py` | `services/finance/art_teacher_payroll.py` | `compute_total_for_month`（外加 `api/art_teacher_payroll.py:21` 用括號 import 的 symbols） |
| `services/fee_refund_calculator.py` | `services/finance/fee_refund_calculator.py` | `api/fees/refunds.py:24` 與 `tests/test_fee_refund_calculator.py:7` 用括號 import — plan 階段列完整 |
| `services/monthly_pnl_service.py` | `services/finance/monthly_pnl_service.py` | `build_monthly_pnl` |
| `services/finance_reconciliation_service.py` | `services/finance/finance_reconciliation_service.py` | `tests/test_finance_reconciliation.py:27` 與 `services/finance_reconciliation_scheduler.py:71` 用括號 import — plan 階段列完整 |
| `services/salary_snapshot_service.py` | `services/finance/salary_snapshot_service.py` | `create_month_end_snapshots`（外加 `api/salary/__init__.py:152` 用括號 import） |
| `utils/finance_cache.py` | `services/finance/finance_cache.py` | `invalidate_finance_summary_cache` |
| `utils/finance_guards.py` | `services/finance/finance_guards.py` | `has_finance_approve, require_finance_approve, require_adjustment_reason` |
| `utils/salary_access.py` | `services/finance/salary_access.py` | `can_view_salary_of, enforce_self_or_full_salary, has_full_salary_view, mask_dict_fields` |

> **註**：標「plan 階段補完整列表」的 4 檔（`salary_field_breakdown` / `art_teacher_payroll` / `fee_refund_calculator` / `finance_reconciliation_service`）— grep 用單行 `from X import` 抓 symbol，括號跨行 import 沒抓到完整名單，且部分檔有未被任何呼叫者 import 但屬 module-level public 的 helper。**implementation plan 必須先跑 AST 掃描**（`ast.parse → ast.Assign / FunctionDef / ClassDef，篩 module-level 不以 `_` 開頭者 + 已知被 import 的 `_` private name`）抽出完整 export 清單再寫 shim。design 階段不掃因為這是機械步驟，且最後 §4.1 自動化驗證涵蓋（任何漏的 symbol pytest 會立刻紅）。

### 3.2 並行 worktree 不受影響的論證

下表列 3 worktree 動到的 services/utils 檔，無一在本 PR 13 檔之內：

| Worktree | 動到的 services/ | 動到的 utils/ | 與本 PR 13 檔交集 |
|---|---|---|---|
| config-centralization-phase1 | 7 scheduler + activity_query_token + activity_service + approval/cross_type_offset + geocoding + recruitment_ivykids_sync + recruitment_market_intelligence | auth, cookie, errors, rate_limit, request_ip, security_headers, sentry_init, storage, supabase_storage | ∅ |
| jwt-secret-rotation | activity_query_token | audit, auth | ∅ |
| scheduler-leader-election | 4 scheduler | — | ∅ |

13 檔搬走後，老路徑保留 shim，三個 worktree 的 import 行（如 `services/finance_reconciliation_scheduler.py:71` 的 `from services.finance_reconciliation_service import ...`）走 shim，零行為差異。

### 3.3 既有 sub-package 不動

| 子套件 | 不動原因 |
|---|---|
| `services/salary/` | §2.2 已說明：是 facade 的內部實作，不合併 |
| `services/appraisal/` | 與 finance 無關 |
| `services/approval/` | 含 cross_type_offset，被 config-centralization 動到 |
| `services/analytics/` | 與 finance 無關 |
| `services/gov_data/`, `services/gov_moe/` | 政府報表，獨立邊界 |
| `services/notification/` | 通知，獨立邊界 |
| `services/year_end/` | 年終，與 finance 高度相關但暫不收（PR1 範圍守住），下批可考慮併入 finance/ 或保留獨立 |

---

## 4. 驗證計畫

### 4.1 自動化

1. `pytest -x` 全套（後端 main 約 4486 tests）— 既有測試已透過 shim 涵蓋
2. `python -c "from services.salary_engine import SalaryEngine; from utils.finance_cache import invalidate_finance_summary_cache; from utils.finance_guards import require_finance_approve; from utils.salary_access import can_view_salary_of; print('shim ok')"` smoke
3. `python -c "from services.finance.salary_engine import SalaryEngine; print('new path ok')"` smoke
4. `python -m py_compile services/*.py utils/*.py services/finance/*.py` — 確保零 syntax error
5. `grep -rn "services\.salary_engine\b" --include="*.py" | grep -v __pycache__ | grep -v "services/finance/" | wc -l` — **PR1 期望仍 > 0**（舊 import path 透過 shim 還活著），這是檢查 PR1 沒有意外改 import；PR2 完成後同一條 grep 期望 = 0

### 4.2 手動 / 啟動驗證

1. `cd ivy-backend && uvicorn main:app --reload` 啟動，確認 lifespan startup 5 個 scheduler 正常拉起（透過 shim）、`init_salary_services(...)` 成功（main.py 14 行 `from services.salary_engine import SalaryEngine` 走 shim）
2. `curl http://localhost:8088/openapi.json | jq '.paths | keys | length'` 確認 router 數量不變（554）
3. 抽 3 個典型端點 hit 一次：`/api/employees/me` / `/api/salaries/simulate` / `/api/reports/monthly-pnl`

### 4.3 不做的驗證

- 不跑 e2e（純結構 refactor，行為零變更）
- 不跑 loadtest（無效能變化）
- 不跑前端（純後端，且後端 OpenAPI surface 不變）

---

## 5. PR2 預告（單獨 spec）

**前置條件**：本 PR1 已 merge + 3 個並行 worktree（config-centralization、jwt-secret-rotation、scheduler-leader-election）全 merge 進 main。

**PR2 範圍**：
1. `grep -rl "services\.salary_engine\b\|services\.salary_slip\b\|...其他 13 個舊路徑..."` 找出所有 import site（預估約 50 個檔，含 main.py、router、test、scheduler）
2. 一次性改完所有 import path 到新路徑
3. 刪除 13 個 shim 檔
4. 跑 §4.1/§4.2 同一套驗證

**為什麼分兩個 PR？**
- PR1 與並行工作並存（13 檔零交集 + shim 兼容）
- PR2 需要全部 main 對齊後才能掃 import path，否則改不完
- 拆兩個 PR 讓 reviewer 各只看一件事（搬位 vs 改呼叫）

---

## 6. 風險與緩解

| 風險 | 緩解 |
|---|---|
| shim 漏 re-export 某 private symbol → test 紅 | 顯式列 private name（已抓 `_sum_leave_deduction` 等）；plan 階段用 AST 補完 4 個括號 import 檔 |
| `services/finance/__init__.py` 副作用觸發 circular | §2.3 強制空 init |
| 舊 `.pyc` 快取導致 import 走錯 | 規範 step：刪 `services/__pycache__/`、`utils/__pycache__/`、`services/finance/__pycache__/` |
| `from services import salary_snapshot_service as snap_svc` 這種 module-form import 失效 | shim 本身仍是 module，自動相容（已驗證 import 形式） |
| 某 router 用 deferred import（function 內）導致 shim runtime 才報錯 | §4.2 啟動驗證 + hit 3 個端點覆蓋 |
| 13 檔中某檔定有 `__all__`，shim 的 `from X import *` 漏掉 `__all__` 外的 public symbol | implementation plan 第一步：`grep -l "^__all__" services/{salary_engine,salary_slip,...}.py utils/{finance_cache,finance_guards,salary_access}.py` 期望 0 行；若任一檔有 `__all__`，該檔 shim 改為逐 symbol 顯式 `from X import (a, b, c, ...)` |
| 三 worktree 的某一個在 PR1 review 期間 merge，改到 main.py 某 import 我這支沒同步 | 13 檔零交集，main.py 本 PR 完全不動，三 worktree 各自 merge 順序不影響 PR1 |
| PR2 找漏 import path | PR2 完成後加一條 grep CI guard：`grep -rEn "services\.salary_engine\b\|..." --include="*.py" | grep -v services/finance/` 期望 0 行 |

---

## 7. 失敗回滾

PR1 任何階段出問題：`git revert <commit>` 即可。零下游清理。

PR2 出問題：`git revert` 後 shim 仍在 main，老 import path 仍可用，無服務中斷。

---

## 8. 後續批次（不在本 spec）

依下表優先序（取決於並行工作 merge 順序）：

| 批次 | 範圍 | 阻擋條件 |
|---|---|---|
| PR2 | 改 import + 刪 shim（本系列） | 3 worktree merge + PR1 merge |
| Batch 2 | `services/scheduler/` 收 7 scheduler | scheduler-leader-election + config-centralization merge |
| Batch 3 | `utils/auth/` 收 auth/cookie/audit/rate_limit | jwt-secret-rotation + config-centralization merge |
| Batch 4 | `utils/io/`、`utils/excel/`、`utils/pdf/` | 無 |
| Batch 5 | `services/parent/`、`services/activity/`、`services/portal/` | 各自 domain 並行工作盤點 |

每批各自走一輪 brainstorm → spec → plan → 兩 PR（搬位 / 改 import）流程，避免一次大爆炸。
