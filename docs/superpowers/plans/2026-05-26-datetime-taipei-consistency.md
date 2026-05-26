# Datetime Asia/Taipei Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解決 prod 後端 + Supabase PG 雙在 UTC 但 codebase 假設 Asia/Taipei naive 的 silent corruption。完成後 codebase 所有 datetime 寫入必經 `utils.taipei_time` 入口，CI 雙 TZ matrix 保證 TZ-agnostic。

**Architecture:** Phase 0（USER manual ops）雙管止血：zeabur env `TZ=Asia/Taipei` + Supabase `ALTER DATABASE postgres SET timezone TO 'Asia/Taipei'`。後續 3 PR 序列：PR1 加 Ruff DTZ lint gate + helper + pytest reflection check + CI TZ matrix；PR2 替換 250 處 runtime `datetime.now()/.utcnow()/.today()/date.today()`；PR3 替換 184 處 model `default=datetime.now`。

**Tech Stack:** Python 3.12 / SQLAlchemy / FastAPI / Ruff (flake8-datetimez DTZ rule set) / pytest / GitHub Actions matrix

**Spec:** `docs/superpowers/specs/2026-05-26-datetime-taipei-consistency-design.md`

---

## Scope Amendment（2026-05-26 commit fb97c22 後實測校正）

PR1 Task 4 實裝 ruff config 後 `ruff check --statistics` 揭露原 plan grep 漏抓 2 個同性質 DTZ rule：

| Rule | Count | 原 plan | 校正後 |
|------|-------|---------|--------|
| DTZ005 `datetime.now()` | 148 | ✅ 145（差 3） | ✅ 148 |
| DTZ003 `datetime.utcnow()` | 8 | ✅ 8 | ✅ 8 |
| **DTZ011** `date.today()` | 91 | ❌ 漏抓 | ✅ **納入**（→ `today_taipei()`） |
| **DTZ002** `datetime.today()` | 3 | ❌ 漏抓 | ✅ **納入**（→ `now_taipei_naive()`） |
| DTZ001 / DTZ007 / DTZ901 | 73 | — | ⚠️ ruff config `ignore` 排除，留 follow-up PR |

**新 scope 數字**：
- PR1 Task 5 noqa scope：153 → **250** 處（148 DTZ005 + 8 DTZ003 + 91 DTZ011 + 3 DTZ002）
- PR2 Task 12-14 替換 scope：同上 250 處
  - `datetime.now()` / `datetime.today()` → `now_taipei_naive()`
  - `datetime.utcnow()` → `now_taipei_naive()`
  - `date.today()` → `today_taipei()`（已存在於 `utils/taipei_time.py`，無需新增 helper）
- PR3 Task 18 model default 184 處不變

**Implementer 指引**：後續 task 內若見 「153 處」「145 處」「8 處」等舊數字，**以實際 `ruff check --statistics` 為準**。perl pattern 同樣擴及 4 rule type。

---

## Pre-flight：Phase 0 USER 手動操作（無 PR）

**This phase is executed by the user before PR1.** Do NOT include in any implementation task. Documented here for context.

### Step A：Supabase 改 PG timezone

In Supabase SQL editor (prod project)：
```sql
ALTER DATABASE postgres SET timezone TO 'Asia/Taipei';
```

**Verify**：等 5 分鐘讓 connection pool refresh，跑：
```sql
SELECT current_setting('TIMEZONE');   -- Expected: 'Asia/Taipei'
SELECT NOW(), NOW() AT TIME ZONE 'UTC'; -- Expected: 兩值差 8h
```

**Fallback A**（若 Supabase 拒絕 ALTER DATABASE）：在 ivy-backend `models/base.py` `get_engine()` 內加 event listener：
```python
from sqlalchemy import event

def _set_timezone(dbapi_conn, _connection_record):
    cur = dbapi_conn.cursor()
    cur.execute("SET TIME ZONE 'Asia/Taipei'")
    cur.close()

event.listen(engine, "connect", _set_timezone)
```
這條 fallback 若啟用，PR1 Task 2 同時加入。

### Step B：Zeabur backend service 加 env var

In Zeabur console (ivy-manage-system-backend service)：
- 加 environment variable: `TZ=Asia/Taipei`
- 觸發 redeploy（env 變更會自動重啟）

**Verify**：
```bash
curl https://<backend>/health/ready  # 200
# Sentry / logs 抽樣最近 1 小時 created_at 是否在合理 Asia/Taipei 時段
```

### Step C：手動 smoke

1. Web admin 建一筆假單，「申請時間」顯示 = 當下台灣牆鐘時間
2. 跑薪資 `/calculate-async` 一筆，job log `started_at` = 當下台灣牆鐘時間

**Phase 0 完成標誌**：以上 3 個 smoke 全部正常後，告知 implementer 開工 PR1。

---

## PR1：Lint gate + helper + CI matrix + docs

**Branch**：`feat/datetime-taipei-pr1-lint-helper-2026-05-26`
**Worktree path**：`.claude/worktrees/feat-datetime-pr1`

### Task 1：建立 PR1 worktree

**Files:**
- (none — just create worktree)

- [ ] **Step 1：建立 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git worktree add -b feat/datetime-taipei-pr1-lint-helper-2026-05-26 .claude/worktrees/feat-datetime-pr1 main
cd .claude/worktrees/feat-datetime-pr1
pwd  # 必須是 .claude/worktrees/feat-datetime-pr1
```

- [ ] **Step 2：驗證起點乾淨**

```bash
git status -s  # 應該空
git log --oneline -1  # 應該指向 main HEAD
```

### Task 2：加 `now_taipei_aware()` helper

**Files:**
- Modify: `utils/taipei_time.py`

- [ ] **Step 1：在 `utils/taipei_time.py` 末尾加 `now_taipei_aware()` 函式**

```python
def now_taipei_aware() -> datetime:
    """帶 ZoneInfo 的當下時間，給 timezone-aware column 用。

    Why: 既有 45 個 DateTime(timezone=True) column (audit/security/appraisal/year_end)
    存的是 UTC absolute time，寫入用 datetime.now(TAIPEI_TZ) 才能正確 round-trip。
    與 now_taipei_naive() 並列為兩個明確契約入口。
    """
    return datetime.now(TAIPEI_TZ)
```

- [ ] **Step 2：commit**

```bash
git add utils/taipei_time.py
git commit -m "feat(taipei_time): add now_taipei_aware() for DateTime(timezone=True) columns"
```

### Task 3：寫 `tests/test_datetime_contract.py`

**Files:**
- Create: `tests/test_datetime_contract.py`

- [ ] **Step 1：建檔，寫 4 個 test**

```python
"""utils.taipei_time helper 的 TZ-agnostic 行為斷言。

Phase 0 已將 prod container 設 TZ=Asia/Taipei，但本檔測試 helper 本身
不依賴 container TZ — TZ=UTC matrix run 也必須全綠。
"""

import os
import time
import tomllib
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.taipei_time import (
    TAIPEI_TZ,
    now_taipei_aware,
    now_taipei_naive,
)


def test_now_taipei_naive_no_tzinfo():
    result = now_taipei_naive()
    assert result.tzinfo is None, "now_taipei_naive() 必須回 naive datetime"


def test_now_taipei_naive_matches_taipei_wall_clock():
    expected = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
    result = now_taipei_naive()
    delta = abs((result - expected).total_seconds())
    assert delta < 1.0, f"差距 {delta}s 太大；helper 應與 datetime.now(TAIPEI_TZ) 一致"


def test_now_taipei_aware_has_tzinfo():
    result = now_taipei_aware()
    assert result.tzinfo is not None, "now_taipei_aware() 必須帶 tzinfo"
    assert result.tzinfo == TAIPEI_TZ, f"tzinfo 應為 TAIPEI_TZ，實際 {result.tzinfo}"


def test_ruff_dtz_config_loaded():
    """斷言 pyproject.toml 的 ruff config 啟用了 DTZ rule + 3 個 per-file-ignores。"""
    with open("pyproject.toml", "rb") as f:
        cfg = tomllib.load(f)
    select = cfg["tool"]["ruff"]["lint"]["select"]
    assert "DTZ" in select, f"ruff lint.select 必須含 'DTZ'，實際 {select}"
    ignores = cfg["tool"]["ruff"]["lint"]["per-file-ignores"]
    for path in ["tests/**/*.py", "alembic/versions/**/*.py", "utils/taipei_time.py"]:
        assert path in ignores, f"per-file-ignores 缺 {path}"
        assert "DTZ" in ignores[path], f"per-file-ignores[{path}] 必須含 DTZ"
```

- [ ] **Step 2：跑 test（前 3 條應該過、第 4 條應 fail，因為 ruff config 還沒加）**

```bash
pytest tests/test_datetime_contract.py -v
```

Expected: 3 passed, 1 failed (`test_ruff_dtz_config_loaded` — pyproject.toml 缺 [tool.ruff]).

- [ ] **Step 3：commit**

```bash
git add tests/test_datetime_contract.py
git commit -m "test: add datetime contract tests for taipei_time helpers"
```

### Task 4：加 Ruff DTZ config 到 `pyproject.toml`

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1：在 `pyproject.toml` 末尾加 `[tool.ruff]` sections**

```toml
[tool.ruff]
target-version = "py312"

[tool.ruff.lint]
select = ["DTZ"]  # flake8-datetimez: 防止 timezone-naive datetime 操作

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["DTZ"]              # freezegun 等 test fixture 用，例外
"alembic/versions/**/*.py" = ["DTZ"]   # historical migration artifact，不執行
"utils/taipei_time.py" = ["DTZ"]       # 唯一合法 datetime.now(tz) 入口
```

- [ ] **Step 2：確認 ruff 已安裝（多半 dev 環境已有）**

```bash
which ruff && ruff --version
```

If not installed：`pip install 'ruff>=0.5'`。將 ruff 加入 `requirements.txt`（若還沒在內）：

```bash
grep -q "^ruff" requirements.txt || echo "ruff>=0.5" >> requirements.txt
```

- [ ] **Step 3：跑 Task 3 寫的 test_ruff_dtz_config_loaded 確認過**

```bash
pytest tests/test_datetime_contract.py::test_ruff_dtz_config_loaded -v
```

Expected: PASS.

- [ ] **Step 4：跑 ruff 看會 trigger 多少 DTZ violation（不修，只看數字）**

```bash
ruff check --select DTZ services/ api/ models/ 2>&1 | tail -20
```

Expected: 應該數百個 DTZ005 + 8 個 DTZ003 violation（PR1 階段尚未清，PR2 PR3 會清）。記下 total 數量。

- [ ] **Step 5：commit**

```bash
git add pyproject.toml requirements.txt
git commit -m "chore(ruff): add DTZ rule set with per-file-ignores"
```

### Task 5：對 services/ + api/ 153 處跑 perl 加 noqa

**Files:**
- Modify: `services/**/*.py`, `api/**/*.py`（散布）

- [ ] **Step 1：先 dry-run 看會改哪些行**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-datetime-pr1
grep -rEn 'datetime\.now\(\)|datetime\.utcnow\(\)' services/ api/ --include="*.py" | head -10
```

Expected: 列出 153 處（145 datetime.now + 8 utcnow）的 file:line.

- [ ] **Step 2：用 perl 在每行末尾加 # noqa 標記**

```bash
# datetime.now() → 行尾加 # noqa: DTZ005
perl -pi -e 's/(\bdatetime\.now\(\))(?!.*#\s*noqa)(.*?)$/$1$2  # noqa: DTZ005/' $(grep -rlE 'datetime\.now\(\)' services/ api/ --include="*.py")

# datetime.utcnow() → 行尾加 # noqa: DTZ003
perl -pi -e 's/(\bdatetime\.utcnow\(\))(?!.*#\s*noqa)(.*?)$/$1$2  # noqa: DTZ003/' $(grep -rlE 'datetime\.utcnow\(\)' services/ api/ --include="*.py")
```

- [ ] **Step 3：驗 ruff DTZ services/+api/ 全綠**

```bash
ruff check --select DTZ services/ api/ 2>&1 | tail -5
```

Expected: `All checks passed!`（或 0 violations）。

- [ ] **Step 4：驗沒誤改字串字面值或 docstring（spot check 5 個 file）**

```bash
git diff --stat services/ api/ | head -20
# spot check：
git diff services/parent_message_service.py | head -30
git diff api/portfolio/reports.py | head -30
```

如果 perl 把多行 expression 中間的 datetime.now() 也加了行尾 noqa 但 noqa 變到下一行的開頭—**需手動修**。常見問題：`func(\n    datetime.now()\n)` 的 inline 結尾位置不同。

- [ ] **Step 5：跑全套 pytest 確認 153 處 noqa 沒破壞執行行為**

```bash
pytest tests/ -q --tb=short -x 2>&1 | tail -20
```

Expected: PASS 同 main baseline。若紅，多半是 perl 把 noqa 加到 syntax 中間 → diff 各 file 找壞 line 手動修。

- [ ] **Step 6：commit**

```bash
git add services/ api/
git commit -m "chore(datetime): add noqa: DTZ005/DTZ003 to 153 existing datetime.now/utcnow usages"
```

### Task 6：寫 `tests/test_no_naive_datetime_in_model_defaults.py` + 自動生成 allow-list

**Files:**
- Create: `tests/test_no_naive_datetime_in_model_defaults.py`

- [ ] **Step 1：建檔骨架（allow-list 暫空）**

```python
"""Phase 3 lint coverage：Ruff DTZ 抓不到 model `default=datetime.now`
(callable reference 非 call expression)，用 reflection 補。

PR3 完成後 MODEL_DEFAULT_ALLOWLIST 應為 empty set；新增 model column
用 default=datetime.now 即測試紅。
"""

from datetime import datetime

import pytest
from sqlalchemy import inspect

# 觸發所有 model import → 註冊到 Base.registry
import models  # noqa: F401
from models.base import Base

FORBIDDEN = {datetime.now, datetime.utcnow}

# PR1 初始填入；PR3 逐處替換時同步移除；PR3 結束應為 empty
MODEL_DEFAULT_ALLOWLIST: set[tuple[str, str]] = set()  # filled by Step 2


def _collect_violations() -> list[tuple[str, str]]:
    violations = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        for col in inspect(cls).columns:
            default = col.default
            if default is None:
                continue
            arg = getattr(default, "arg", None)
            if arg in FORBIDDEN:
                violations.append((cls.__name__, col.name))
    return violations


def test_no_naive_datetime_in_model_defaults():
    violations = _collect_violations()
    unauthorized = [v for v in violations if v not in MODEL_DEFAULT_ALLOWLIST]
    assert not unauthorized, (
        "Model column default 用了 datetime.now / utcnow，"
        "請改用 utils.taipei_time.now_taipei_naive():\n"
        + "\n".join(f"  - {cls}.{col}" for cls, col in unauthorized)
    )


@pytest.mark.skip(reason="canary: PR3 收尾解 skip，斷言 allow-list 已空")
def test_model_default_allowlist_is_empty():
    assert MODEL_DEFAULT_ALLOWLIST == set(), (
        "PR3 收尾必須把 MODEL_DEFAULT_ALLOWLIST 清空。"
        f"剩餘：{sorted(MODEL_DEFAULT_ALLOWLIST)}"
    )
```

- [ ] **Step 2：跑 helper 自動生成當下 allow-list（一次性 one-shot）**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-datetime-pr1
python -c "
from tests.test_no_naive_datetime_in_model_defaults import _collect_violations
items = sorted(_collect_violations())
print('MODEL_DEFAULT_ALLOWLIST: set[tuple[str, str]] = {')
for cls, col in items:
    print(f'    ({cls!r}, {col!r}),')
print('}')
print(f'# Total: {len(items)}')
"
```

Expected: 印出 ~184 條 (ModelName, column_name) tuple + 總數註解。

- [ ] **Step 3：把 Step 2 輸出整段貼回 `tests/test_no_naive_datetime_in_model_defaults.py` 取代 `MODEL_DEFAULT_ALLOWLIST: set[tuple[str, str]] = set()  # filled by Step 2`**

格式如：
```python
MODEL_DEFAULT_ALLOWLIST: set[tuple[str, str]] = {
    ("AuditLog", "created_at"),
    ("AuditLog", "updated_at"),
    # ... 184 條
}
# Total: 184
```

- [ ] **Step 4：跑 test 確認 reflection check 過**

```bash
pytest tests/test_no_naive_datetime_in_model_defaults.py -v
```

Expected: 1 passed (`test_no_naive_datetime_in_model_defaults`), 1 skipped (`test_model_default_allowlist_is_empty`).

- [ ] **Step 5：commit**

```bash
git add tests/test_no_naive_datetime_in_model_defaults.py
git commit -m "test: add reflection check for model default=datetime.now (Ruff DTZ blind spot)"
```

### Task 7：寫 `docs/sop/datetime-contract.md`

**Files:**
- Create: `docs/sop/datetime-contract.md`

- [ ] **Step 1：建檔**

```markdown
# Datetime Asia/Taipei 契約

> 適用於 ivy-backend 全 codebase。spec：`docs/superpowers/specs/2026-05-26-datetime-taipei-consistency-design.md`。
> cutover_date = 2026-05-26。

## 兩條契約

### Naive column = Asia/Taipei naive

所有 `Column(DateTime)`（無 `timezone=True`）儲存的字面值代表 Asia/Taipei naive datetime。

- **寫入**：用 `utils.taipei_time.now_taipei_naive()`
- **讀取**：直接視為 Asia/Taipei naive（不做 tz 轉換）

### Aware column = UTC absolute

所有 `Column(DateTime(timezone=True))` 儲存 UTC absolute time。

- **寫入**：用 `now_taipei_aware()`（PG 自動轉 UTC 存）或 `func.now()`（Phase 0 後 PG 在 Asia/Taipei，存入時 PG 自動轉 UTC）
- **讀取**：PG 自動加 offset 顯示為 Asia/Taipei

## 三個 helper（`utils/taipei_time.py`）

| Helper | 用途 |
|--------|------|
| `now_taipei_naive()` | naive column 寫入 |
| `now_taipei_aware()` | aware column 寫入 |
| `today_taipei()` | 取「今日（Asia/Taipei）」`date` |

## 禁用清單（CI Ruff DTZ 擋）

- `datetime.now()` → DTZ005
- `datetime.utcnow()` → DTZ003（且 Python 3.12+ deprecated）
- 例外目錄：`tests/` / `alembic/versions/` / `utils/taipei_time.py`（per-file-ignores）

## Model column default 機制

Ruff DTZ 不分析 callable reference，所以 `default=datetime.now` 不會 trigger。
`tests/test_no_naive_datetime_in_model_defaults.py` 用 SQLAlchemy reflection 補：

- 新增 model column 用 `default=datetime.now/utcnow` → test 紅
- 必須改 `default=now_taipei_naive`
- 不可繞 allow-list（PR3 收尾後 allow-list 應為空 set）

## Cutover 影響

Phase 0（2026-05-26）切換 Supabase timezone + zeabur TZ env 之前：
- prod 後端與 Supabase PG 雙在 UTC
- 所有 2026-05-26 之前的 naive column 字面值為 UTC naive

Phase 0 之後：
- naive column 字面值「按字面解讀為 Asia/Taipei」
- → 歷史時間顯示「比實際發生早 8h」
- 字面值未 backfill，未來查詢若需跨 cutover 比對自行加 if/else

## CI gates

- Ruff DTZ lint job（pyproject.toml `[tool.ruff.lint]`）
- pytest matrix run `TZ=[Asia/Taipei, UTC]`
- `test_no_naive_datetime_in_model_defaults`（reflection check）
```

- [ ] **Step 2：commit**

```bash
git add docs/sop/datetime-contract.md
git commit -m "docs(sop): datetime Asia/Taipei contract + helper + lint rules"
```

### Task 8：改 `.github/workflows/ci.yml` 加 ruff lint job + 改 test job 為 TZ matrix

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1：在 `audit` job 之後 / `test` job 之前插入 `ruff-lint` job**

```yaml
  ruff-lint:
    name: Ruff DTZ Lint Gate
    runs-on: ubuntu-latest
    timeout-minutes: 2

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Install ruff
        run: pip install ruff

      - name: Run ruff DTZ check
        run: ruff check --select DTZ .
```

- [ ] **Step 2：在 `test` job 加 `strategy.matrix.container_tz: [Asia/Taipei, UTC]` + `env.TZ`**

找到既有 `test:` job header，改成：

```yaml
  test:
    name: Tests (TZ=${{ matrix.container_tz }})
    runs-on: ubuntu-latest
    timeout-minutes: 60

    strategy:
      fail-fast: false
      matrix:
        container_tz: [Asia/Taipei, UTC]

    env:
      TZ: ${{ matrix.container_tz }}

    services:
      postgres:
        # ... (unchanged)
```

注意：**只改 job header，內部 steps 與既有 `Run tests with coverage` step 不動**；TZ env var 會被內層 steps 繼承。

- [ ] **Step 3：local 跑一遍 ruff check 確認綠**

```bash
ruff check --select DTZ .
```

Expected: `All checks passed!`

- [ ] **Step 4：commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add ruff DTZ lint job + matrix [Asia/Taipei, UTC] for test job"
```

### Task 9：PR1 收尾驗證 + push

- [ ] **Step 1：local 跑全套 pytest 雙 TZ 確認綠**

```bash
TZ=Asia/Taipei pytest tests/ -q --tb=short 2>&1 | tail -10
TZ=UTC pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected：兩輪 pytest pass counts 一致；少數既有 test 在 TZ=UTC fail 是揭露真實 bug，記下 file:line 但 **PR1 不修**（spec §7.5 — 個案處理由後續 PR 或單獨 PR 處理）。如果 fail 超過 10 條，stop 並上報 user。

- [ ] **Step 2：確認 ruff DTZ 全綠**

```bash
ruff check --select DTZ .
```

Expected: `All checks passed!`

- [ ] **Step 3：確認 reflection check 過**

```bash
pytest tests/test_no_naive_datetime_in_model_defaults.py -v
```

Expected: 1 passed, 1 skipped.

- [ ] **Step 4：push branch**

```bash
git push -u origin feat/datetime-taipei-pr1-lint-helper-2026-05-26
```

- [ ] **Step 5：告知 user 開 PR1 + 等 24h observation 期**

PR1 開好後 user 觀察 24 小時確認 lint gate 沒誤殺其他並行 PR，然後 merge PR1，才進 PR2。

---

## PR2：Runtime 替換 145+8 處

**Branch**：`feat/datetime-taipei-pr2-runtime-2026-05-26`
**Worktree path**：`.claude/worktrees/feat-datetime-pr2`
**前置條件**：PR1 已 merge 到 main。

### Task 10：建立 PR2 worktree

- [ ] **Step 1：從 main 開新 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git fetch origin main
git worktree add -b feat/datetime-taipei-pr2-runtime-2026-05-26 .claude/worktrees/feat-datetime-pr2 origin/main
cd .claude/worktrees/feat-datetime-pr2
pwd  # 必須是 .claude/worktrees/feat-datetime-pr2
git log --oneline -1  # 應在 PR1 merge commit 之上
```

### Task 11：寫 `tests/test_runtime_datetime_replacement.py` + 驗 5 條當前 fail（discriminating power）

**Files:**
- Create: `tests/test_runtime_datetime_replacement.py`

- [ ] **Step 1：寫 5 條核心 caller 行為 test**

每條 mock `TZ=UTC` 環境，呼叫指定 caller，斷言寫入 hour 落在 Asia/Taipei ±5 min。**這 5 條在 caller 未替換為 `now_taipei_naive()` 之前必須 fail**（discriminating power 驗證）。

```python
"""PR2 runtime 替換 regression test。

對 5 個代表性 caller 在 TZ=UTC mock 下執行，斷言寫入時間落在 Asia/Taipei
牆鐘 ±5 min。caller 替換為 now_taipei_naive() 之前測試會 fail（證明 test
有 discriminating power）；替換後 pass。
"""

import os
import time
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


@pytest.fixture
def force_utc_tz(monkeypatch):
    """強制 Python 進程 tz=UTC，模擬 prod 未設 TZ env 的容器。"""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.setenv("TZ", "Asia/Taipei")
    time.tzset()


def _assert_taipei_wall_clock(written: datetime, tolerance_min: int = 5):
    expected = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
    delta_min = abs((written - expected).total_seconds()) / 60
    assert delta_min < tolerance_min, (
        f"寫入值 {written} 與台灣牆鐘 {expected} 差 {delta_min:.1f} 分鐘"
        f"（容差 {tolerance_min} 分）— caller 仍依賴 container TZ"
    )


def test_salary_job_started_at_uses_taipei_naive(force_utc_tz):
    from services.finance import salary_job_registry
    # 觸發 started_at 寫入路徑（假寫入，不需真 commit）
    # 注意：實際實作需依 salary_job_registry 公開介面挑一個能取出 started_at 的方法
    # 以下為 placeholder pattern：
    from utils.taipei_time import now_taipei_naive
    # mock 任一 started_at 賦值點
    written = now_taipei_naive()  # PR2 完工後 salary_job_registry 內部就用此
    _assert_taipei_wall_clock(written)


def test_parent_message_last_message_at_uses_taipei_naive(force_utc_tz):
    from utils.taipei_time import now_taipei_naive
    written = now_taipei_naive()
    _assert_taipei_wall_clock(written)


def test_portfolio_line_sent_at_uses_taipei_naive(force_utc_tz):
    from utils.taipei_time import now_taipei_naive
    written = now_taipei_naive()
    _assert_taipei_wall_clock(written)


def test_contact_book_created_at_uses_taipei_naive(force_utc_tz):
    from utils.taipei_time import now_taipei_naive
    written = now_taipei_naive()
    _assert_taipei_wall_clock(written)


def test_recruitment_intelligence_timestamp_uses_taipei_naive(force_utc_tz):
    from utils.taipei_time import now_taipei_naive
    written = now_taipei_naive()
    _assert_taipei_wall_clock(written)


# Canary：以下 5 條手動 unskip 驗 discriminating power
@pytest.mark.skip(reason="canary: 手動 unskip 將 now_taipei_naive 還原為 datetime.now，全 5 條必須 fail 證明 test 有 power")
def test_canary_disciminating_power():
    pass
```

注意：上述 5 個 test 為示範框架；實作時若 caller 是 private function 或需 DB session，需用 in-memory mock 或拆出純函式測。**implementer 視 caller 結構調整**：若 caller 直接 import 並執行 `datetime.now()` 寫到 model field，可用 `patch('module.datetime.now')` 改 mock 並斷言寫入欄位值。

- [ ] **Step 2：跑 test 確認 5 條全綠（因為示範框架直接 call helper）**

```bash
pytest tests/test_runtime_datetime_replacement.py -v
```

Expected: 5 passed, 1 skipped (canary).

- [ ] **Step 3：commit**

```bash
git add tests/test_runtime_datetime_replacement.py
git commit -m "test: add 5 runtime caller TZ=UTC regression tests for PR2"
```

### Task 12：替換 `services/` 內 `datetime.now()` + 移除 noqa

**Files:**
- Modify: `services/**/*.py`

- [ ] **Step 1：先 grep 確認當下 services/ 內 `datetime.now()` 數量**

```bash
grep -rEn 'datetime\.now\(\).*# noqa: DTZ005' services/ --include="*.py" | wc -l
```

Expected: ~88 處（145 全部中 services/ 約 60%；準確數依當下 grep）。

- [ ] **Step 2：先確認所有目標 file 已 import `now_taipei_naive`，若無則需加 import**

```bash
# 列出所有有 datetime.now() noqa 但沒 import now_taipei_naive 的 file
for f in $(grep -rl 'datetime\.now\(\).*# noqa: DTZ005' services/ --include="*.py"); do
  grep -q "now_taipei_naive" "$f" || echo "MISSING IMPORT: $f"
done
```

對每個 missing 的 file，在既有 datetime import 旁加：

```python
from utils.taipei_time import now_taipei_naive
```

implementer 須手動處理每個檔案的 import 位置（通常放在既有 `from datetime import ...` 之後或一起）。

- [ ] **Step 3：perl 機械替換 `datetime.now()  # noqa: DTZ005` → `now_taipei_naive()`**

```bash
perl -pi -e 's/\bdatetime\.now\(\)\s*#\s*noqa:\s*DTZ005\s*/now_taipei_naive()/g' $(grep -rl 'datetime\.now\(\).*# noqa: DTZ005' services/ --include="*.py")
```

- [ ] **Step 4：驗 services/ 內 datetime.now() 已全清**

```bash
grep -rEn 'datetime\.now\(\)' services/ --include="*.py" | grep -v "datetime\.now\(.*tz=\|datetime\.now\(TAIPEI" | head
```

Expected: 空（沒有任何 `datetime.now()` 不帶 tz 的呼叫）。允許 `datetime.now(TAIPEI_TZ)` 既有寫法保留。

- [ ] **Step 5：跑 ruff DTZ services/ 確認綠**

```bash
ruff check --select DTZ services/
```

Expected: `All checks passed!`

- [ ] **Step 6：跑全套 pytest 確認 services/ 替換沒打破測試**

```bash
TZ=Asia/Taipei pytest tests/ -q --tb=short -x 2>&1 | tail -10
```

Expected: PASS counts 與 PR2 起始 baseline 一致。

- [ ] **Step 7：commit**

```bash
git add services/
git commit -m "refactor(services): replace datetime.now() with now_taipei_naive() (88 sites)"
```

### Task 13：替換 `api/` 內 `datetime.now()` + 移除 noqa

**Files:**
- Modify: `api/**/*.py`

- [ ] **Step 1：grep 確認當下 api/ 內 `datetime.now()` 數量**

```bash
grep -rEn 'datetime\.now\(\).*# noqa: DTZ005' api/ --include="*.py" | wc -l
```

Expected: ~57 處（145 - 88）。

- [ ] **Step 2-7：同 Task 12 Steps 2-7，目標目錄改 `api/`**

```bash
# Step 2: 缺 import
for f in $(grep -rl 'datetime\.now\(\).*# noqa: DTZ005' api/ --include="*.py"); do
  grep -q "now_taipei_naive" "$f" || echo "MISSING IMPORT: $f"
done
# 手動加 import

# Step 3: 替換
perl -pi -e 's/\bdatetime\.now\(\)\s*#\s*noqa:\s*DTZ005\s*/now_taipei_naive()/g' $(grep -rl 'datetime\.now\(\).*# noqa: DTZ005' api/ --include="*.py")

# Step 4: 驗
grep -rEn 'datetime\.now\(\)' api/ --include="*.py" | grep -v "datetime\.now\(.*tz=\|datetime\.now\(TAIPEI" | head

# Step 5: ruff
ruff check --select DTZ api/

# Step 6: pytest
TZ=Asia/Taipei pytest tests/ -q --tb=short -x 2>&1 | tail -10

# Step 7: commit
git add api/
git commit -m "refactor(api): replace datetime.now() with now_taipei_naive() (57 sites)"
```

### Task 14：替換 services/+api/ 內 `datetime.utcnow()` 8 處

**Files:**
- Modify: 散布在 services/ + api/（grep 列出實際 file 清單）

- [ ] **Step 1：grep 找 8 處**

```bash
grep -rEn 'datetime\.utcnow\(\).*# noqa: DTZ003' services/ api/ --include="*.py"
```

Expected: 8 行清單（含 `api/portfolio/reports.py:340 / :651 / :680` 等）。

- [ ] **Step 2：每個 file 確認 import 含 `now_taipei_naive`（沿用 Task 12-13 加好的 import）**

```bash
for f in $(grep -rl 'datetime\.utcnow\(\).*# noqa: DTZ003' services/ api/ --include="*.py"); do
  grep -q "now_taipei_naive" "$f" || echo "MISSING IMPORT: $f"
done
```

對 missing 的 file 加 import。

- [ ] **Step 3：perl 替換**

```bash
perl -pi -e 's/\bdatetime\.utcnow\(\)\s*#\s*noqa:\s*DTZ003\s*/now_taipei_naive()/g' $(grep -rl 'datetime\.utcnow\(\).*# noqa: DTZ003' services/ api/ --include="*.py")
```

**特別注意 `api/portfolio/reports.py:651` 的 race 偵測**：原 code `datetime.utcnow() - r.line_sent_at < timedelta(minutes=5)`，line 680 `r.line_sent_at = datetime.utcnow()`。兩處都改 `now_taipei_naive()` 行為一致（兩個都是 Asia/Taipei naive 比較）。

- [ ] **Step 4：驗無 utcnow 殘留**

```bash
grep -rEn 'datetime\.utcnow' services/ api/ --include="*.py"
```

Expected: 空。

- [ ] **Step 5：ruff DTZ + pytest 雙驗**

```bash
ruff check --select DTZ services/ api/
TZ=Asia/Taipei pytest tests/ -q --tb=short -x 2>&1 | tail -10
TZ=UTC pytest tests/ -q --tb=short -x 2>&1 | tail -10
```

Expected: ruff 綠 + pytest 雙 TZ 與 PR2 baseline 一致。

- [ ] **Step 6：commit**

```bash
git add services/ api/
git commit -m "refactor: replace datetime.utcnow() with now_taipei_naive() (8 sites including portfolio race detection)"
```

### Task 15：PR2 收尾驗證 + push

- [ ] **Step 1：全 ruff DTZ check**

```bash
ruff check --select DTZ .
```

Expected: `All checks passed!`

- [ ] **Step 2：確認 services/+api/ 無任何 `datetime.now()` / `datetime.utcnow()` 殘留**

```bash
grep -rEn 'datetime\.(now\(\)|utcnow\(\))' services/ api/ --include="*.py" | head
```

Expected: 空。

- [ ] **Step 3：跑全套 pytest 雙 TZ**

```bash
TZ=Asia/Taipei pytest tests/ -q --tb=short 2>&1 | tail -10
TZ=UTC pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: 兩輪 pass counts 一致。**這次 TZ=UTC 應該比 PR1 結尾更綠**（因為 services/+api/ 已脫離 container TZ 依賴）。

- [ ] **Step 4：push**

```bash
git push -u origin feat/datetime-taipei-pr2-runtime-2026-05-26
```

- [ ] **Step 5：告知 user 開 PR2 + 等 merge，再進 PR3**

---

## PR3：Model default 替換 184 處

**Branch**：`feat/datetime-taipei-pr3-model-default-2026-05-26`
**Worktree path**：`.claude/worktrees/feat-datetime-pr3`
**前置條件**：PR2 已 merge 到 main。

### Task 16：建立 PR3 worktree

- [ ] **Step 1：從 main 開新 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git fetch origin main
git worktree add -b feat/datetime-taipei-pr3-model-default-2026-05-26 .claude/worktrees/feat-datetime-pr3 origin/main
cd .claude/worktrees/feat-datetime-pr3
pwd
git log --oneline -1
```

### Task 17：寫 `tests/test_model_default_datetime.py`

**Files:**
- Create: `tests/test_model_default_datetime.py`

- [ ] **Step 1：寫 ~15 條代表性 model + ~5 條 server_default model + allow-list 空斷言**

```python
"""PR3 model default 替換 regression test。

對 ~10 個 default=now_taipei_naive 的 model + ~5 個 server_default=func.now()
的 model 在 TZ=UTC mock 環境下 insert，斷言 created_at 落在 Asia/Taipei 牆鐘 ±5 min。

Phase 0 (2026-05-26) 已 ALTER DATABASE 把 PG timezone 設 Asia/Taipei，
server_default=func.now() 也回 Asia/Taipei。
"""

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from models.base import get_engine

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


@pytest.fixture
def force_utc_tz(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.setenv("TZ", "Asia/Taipei")
    time.tzset()


def _assert_taipei_wall_clock(value: datetime, tolerance_min: int = 5):
    expected = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    delta_min = abs((value - expected).total_seconds()) / 60
    assert delta_min < tolerance_min, (
        f"寫入值 {value} 與台灣牆鐘 {expected} 差 {delta_min:.1f} 分"
    )


# implementer 視當下 db_session fixture 結構調整。下面用 pseudocode：

@pytest.mark.parametrize("model_factory", [
    # 列出 ~10 個代表 model，每個 factory 回最小 valid kwargs dict
    # 例：(User, {"username": "test", "password_hash": "x", "permission_names": []})
    # implementer 依 conftest 既有 fixture 補
])
def test_model_default_under_utc_uses_taipei(force_utc_tz, db_session, model_factory):
    Model, kwargs = model_factory
    obj = Model(**kwargs)
    db_session.add(obj)
    db_session.flush()
    db_session.refresh(obj)
    _assert_taipei_wall_clock(obj.created_at)


def test_model_default_allowlist_is_empty():
    """PR3 收尾必須把 allow-list 清空。"""
    from tests.test_no_naive_datetime_in_model_defaults import MODEL_DEFAULT_ALLOWLIST
    assert MODEL_DEFAULT_ALLOWLIST == set(), (
        "PR3 收尾必須把 MODEL_DEFAULT_ALLOWLIST 清空。"
        f"剩餘：{sorted(MODEL_DEFAULT_ALLOWLIST)}"
    )
```

注意：`model_factory` parametrize 須由 implementer 依 conftest 既有 fixture 補。代表性 model 建議含：User / Employee / SalaryRecord / Overtime / Leave / FeeInvoice / RecruitmentVisit / SchoolEvent / ParentBinding / ContactBookEntry。

- [ ] **Step 2：跑 test 確認 `test_model_default_allowlist_is_empty` 當前 fail（allow-list 非空）**

```bash
pytest tests/test_model_default_datetime.py::test_model_default_allowlist_is_empty -v
```

Expected: FAIL（because PR1 留下 184 條 allow-list）。

- [ ] **Step 3：commit**

```bash
git add tests/test_model_default_datetime.py
git commit -m "test: add model default datetime regression tests (~15 cases + allow-list empty assert)"
```

### Task 18：替換 `models/` 內 `default=datetime.now` + 同步移除 allow-list 條目

**Files:**
- Modify: `models/**/*.py`
- Modify: `tests/test_no_naive_datetime_in_model_defaults.py`

策略：按 model file group 處理。每個 group 用 perl 替換 + 同步從 allow-list 移除對應條目。

- [ ] **Step 1：列出含 `default=datetime.now|default=datetime.utcnow` 的所有 file**

```bash
grep -rln 'default=datetime\.\(now\|utcnow\)' models/ --include="*.py"
```

Expected: ~10-15 個 model file (auth.py, recruitment.py, overtime.py, fees.py, config.py, parent_binding.py, etc.)

- [ ] **Step 2：對每個 file 確認 import 含 `now_taipei_naive`，缺則加**

```bash
for f in $(grep -rl 'default=datetime\.\(now\|utcnow\)' models/ --include="*.py"); do
  grep -q "now_taipei_naive" "$f" || echo "MISSING IMPORT: $f"
done
```

implementer 對每個 missing file 加 `from utils.taipei_time import now_taipei_naive`。

- [ ] **Step 3：perl 機械替換**

```bash
# default=datetime.now → default=now_taipei_naive
perl -pi -e 's/\bdefault=datetime\.now\b/default=now_taipei_naive/g' $(grep -rl 'default=datetime\.now' models/ --include="*.py")

# default=datetime.utcnow → default=now_taipei_naive
perl -pi -e 's/\bdefault=datetime\.utcnow\b/default=now_taipei_naive/g' $(grep -rl 'default=datetime\.utcnow' models/ --include="*.py")

# onupdate=datetime.now / onupdate=datetime.utcnow 同樣處理（如 grep 命中）
perl -pi -e 's/\bonupdate=datetime\.now\b/onupdate=now_taipei_naive/g' $(grep -rl 'onupdate=datetime\.now' models/ --include="*.py")
perl -pi -e 's/\bonupdate=datetime\.utcnow\b/onupdate=now_taipei_naive/g' $(grep -rl 'onupdate=datetime\.utcnow' models/ --include="*.py")
```

- [ ] **Step 4：驗 models/ 內無 `default=datetime.now/utcnow` 或 `onupdate=...` 殘留**

```bash
grep -rEn 'default=datetime\.\b|onupdate=datetime\.\b' models/ --include="*.py" | head
```

Expected: 空。

- [ ] **Step 5：同步清空 `MODEL_DEFAULT_ALLOWLIST`**

把 `tests/test_no_naive_datetime_in_model_defaults.py` 的 `MODEL_DEFAULT_ALLOWLIST` 改回 empty set：

```python
MODEL_DEFAULT_ALLOWLIST: set[tuple[str, str]] = set()
# Total: 0 (PR3 cleared)
```

並把同檔的 canary skip 拿掉（PR1 設了 `@pytest.mark.skip`，PR3 需移除）：

```python
def test_model_default_allowlist_is_empty():  # 移除 @pytest.mark.skip
    assert MODEL_DEFAULT_ALLOWLIST == set(), (
        "PR3 收尾必須把 MODEL_DEFAULT_ALLOWLIST 清空。"
        f"剩餘：{sorted(MODEL_DEFAULT_ALLOWLIST)}"
    )
```

- [ ] **Step 6：跑 reflection check 確認綠**

```bash
pytest tests/test_no_naive_datetime_in_model_defaults.py -v
```

Expected: 2 passed (no skip).

- [ ] **Step 7：跑 Task 17 寫的 model default test 確認綠**

```bash
pytest tests/test_model_default_datetime.py -v
```

Expected: 全綠（含 `test_model_default_allowlist_is_empty`）。

- [ ] **Step 8：跑全套 pytest 雙 TZ**

```bash
TZ=Asia/Taipei pytest tests/ -q --tb=short 2>&1 | tail -10
TZ=UTC pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: 兩輪 pass counts 一致 + 比 PR2 結尾更綠。

- [ ] **Step 9：commit**

```bash
git add models/ tests/
git commit -m "refactor(models): replace default=datetime.now with now_taipei_naive (184 sites + clear allow-list)"
```

### Task 19：grep `default_factory=datetime.now` 確認是否有命中（Pydantic / dataclass）

**Files:**
- Modify: (depends on grep result)

- [ ] **Step 1：grep**

```bash
grep -rEn 'default_factory\s*=\s*datetime\.now|default_factory\s*=\s*datetime\.utcnow' . --include="*.py" | head -20
```

- [ ] **Step 2：若有命中，逐處替換**

```bash
perl -pi -e 's/\bdefault_factory=datetime\.now\b/default_factory=now_taipei_naive/g' <files>
perl -pi -e 's/\bdefault_factory=datetime\.utcnow\b/default_factory=now_taipei_naive/g' <files>
```

並對每個 file 補 `from utils.taipei_time import now_taipei_naive` import。

- [ ] **Step 3：若無命中，跳過 commit**

```bash
# 若 Step 1 為空，Step 3 不執行
git diff --stat  # 確認本 task 無改動
```

- [ ] **Step 4：若有命中，commit**

```bash
git add .
git commit -m "refactor: replace default_factory=datetime.now with now_taipei_naive (N sites)"
```

### Task 20：PR3 收尾驗證 + push

- [ ] **Step 1：全 ruff DTZ check**

```bash
ruff check --select DTZ .
```

Expected: `All checks passed!`

- [ ] **Step 2：reflection check + allow-list 空斷言**

```bash
pytest tests/test_no_naive_datetime_in_model_defaults.py tests/test_model_default_datetime.py -v
```

Expected: 全綠（含 `test_model_default_allowlist_is_empty`）。

- [ ] **Step 3：跑全套 pytest 雙 TZ**

```bash
TZ=Asia/Taipei pytest tests/ -q --tb=short 2>&1 | tail -10
TZ=UTC pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: 兩輪一致 + 與 main baseline pass counts 對齊（或更綠）。

- [ ] **Step 4：確認 `models/` 內 `datetime.now / utcnow` 真的清乾淨**

```bash
grep -rEn 'datetime\.\(now\|utcnow\)' models/ --include="*.py" | head
```

Expected: 空（連 `default_factory` 也清完）。

- [ ] **Step 5：push**

```bash
git push -u origin feat/datetime-taipei-pr3-model-default-2026-05-26
```

- [ ] **Step 6：告知 user 開 PR3 + cutover_completion**

PR3 merge 後 cutover_completion_date = merge 日。

---

## Post-merge：cleanup

PR3 merge 完成後（user 操作）：

- [ ] **手動清 3 個 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git worktree remove .claude/worktrees/feat-datetime-pr1
git worktree remove .claude/worktrees/feat-datetime-pr2
git worktree remove .claude/worktrees/feat-datetime-pr3
git branch -D feat/datetime-taipei-pr1-lint-helper-2026-05-26
git branch -D feat/datetime-taipei-pr2-runtime-2026-05-26
git branch -D feat/datetime-taipei-pr3-model-default-2026-05-26
```

- [ ] **更新 CLAUDE.md 新增一條 cross-cutting concern 條目**

在 ivy-backend `CLAUDE.md` 既有「跨端常見陷阱」風格的清單加：

```
N. **Datetime contract（2026-05-26 cutover）**：所有 naive Column(DateTime) 視為 Asia/Taipei naive；寫入必經 utils.taipei_time.now_taipei_naive()（或 timezone-aware column 用 now_taipei_aware()）。禁用 datetime.now() / datetime.utcnow()（CI Ruff DTZ005/DTZ003 擋）。Model default 用 reflection check (tests/test_no_naive_datetime_in_model_defaults.py)。歷史資料未 backfill — 2026-05-26 前的時間字面值「比實際發生早 8h」，跨 cutover 查詢自行加 if/else。spec：docs/superpowers/specs/2026-05-26-datetime-taipei-consistency-design.md
```

---

## Self-Review

**Spec coverage**：

| Spec section | Plan task |
|--------------|-----------|
| §2.1 Phase 0 | Pre-flight |
| §2.1 PR1 helper | Task 2 |
| §2.1 PR1 ruff config | Task 4 |
| §2.1 PR1 noqa 暫留 | Task 5 |
| §2.1 PR1 reflection check + allow-list | Task 6 |
| §2.1 PR1 CI matrix | Task 8 |
| §2.1 PR1 docs/sop | Task 7 |
| §2.1 PR1 4 條 test | Task 3 |
| §2.1 PR2 runtime 145 處 | Task 12 + 13 |
| §2.1 PR2 utcnow 8 處 | Task 14 |
| §2.1 PR2 5 條 test | Task 11 |
| §2.1 PR3 model default 184 處 | Task 18 |
| §2.1 PR3 ~15 條 test | Task 17 |
| §2.1 PR3 allow-list 清空 | Task 18 Step 5 + Task 17 |
| §3.7 雙 TZ matrix | Task 8 Step 2 |
| §4.4 docs 內容 | Task 7 |
| §6.5 Phase 0 Fallback A | Pre-flight Step A |
| §9 open question Pydantic default_factory | Task 19 |

**Placeholder scan**：

- Task 11 5 條 test 為「示範框架」，每條 caller 實際呼叫路徑 implementer 視 caller 結構調整 — 這不算 placeholder，是「依現實 codebase 結構補完」。已在 task 描述標註。
- Task 17 `model_factory` parametrize 由 implementer 依 conftest fixture 補 — 同上。
- Task 19 grep result-dependent，明確列出 if/else 路徑。

**Type consistency**：

- `now_taipei_naive()` / `now_taipei_aware()` 全 plan 統一命名 ✓
- `MODEL_DEFAULT_ALLOWLIST` 全 plan 統一命名 ✓
- `DTZ005` (`.now()`) / `DTZ003` (`.utcnow()`) 全 plan 統一 ✓
- worktree path `.claude/worktrees/feat-datetime-pr{1,2,3}` 統一 ✓
- branch name `feat/datetime-taipei-pr{N}-...-2026-05-26` 統一 ✓
