# Migration 品質硬化（Phase A）設計

日期：2026-05-21
範圍：ivy-backend
狀態：spec（待 user 審）

---

## 1. 動機

`migration-reviewer` audit 指出 alembic/migrations 三項可改善點。我們對 audit 數字做了對帳，校正後的真實規模與 audit 描述差距如下：

| Audit 描述 | 實際數字（已對帳） |
|---|---|
| 57% migration 含 `drop_*` 卻常缺對稱 downgrade | ✅ 93 / 163 = 57% 檔含 `drop_table` 或 `drop_column`（audit 正確） |
| 16 檔 `if dialect == 'sqlite'` 分支汙染 | ⚠️ **僅 2 檔** 真正分支（`add_activity_academic_term.py` / `add_activity_pending_review_and_classroom_fk.py`）；另 2 檔用 `sqlite_where=` 是 alembic 官方 dialect kwarg，不算汙染 |
| 10 檔空 downgrade（baseline / supervisor_role / classroom_term_fields …） | ⚠️ 確認 10 檔，但 **5 檔是 merge migration**（alembic 慣例空 body），實際可疑 5 檔皆為**資料 cleanup**（本身難可逆） |
| migrations/ 8 個 ad-hoc psycopg2 腳本硬編 `kindergarten_payroll` | ✅ 8 檔 + 1 檔（`add_columns.py`）硬編舊庫名；確認**無 production 程式 import** |

CI 現況：
- `services.postgres: postgres:15` 已存在，**主測試套件已跑真實 PG**。
- 本機 pytest 才走 SQLite — `tests/conftest.py` 在 import 前 monkey-patch `JSONB→JSON / BigInteger→Integer`。
- 已有 `alembic-heads` gate（assert single head），**沒有 upgrade/downgrade roundtrip 檢查**。

Phase A 目標：**最低風險路線** ── 不動本機測試層、不追改已 deploy 的 migration，僅 (a) 砍 dead code、(b) 加 CI 護欄、(c) 加 PR-time lint。

---

## 2. 三個 PR 範圍與邊界

| PR | 動作 | 風險 | 可否並行 |
|---|---|---|---|
| PR1 | 刪 `migrations/` 下 8 個 legacy psycopg2 腳本 | 極低（已驗無 import） | ✅ |
| PR2 | 新增 CI `alembic-roundtrip` job（PG container 跑 upgrade→downgrade→upgrade） | 中（首跑可能爆出既有不可逆 migration） | ✅ |
| PR3 | 新增 `scripts/lint_alembic_symmetry.py` + CI job + 為 7 個既有檔加 skip 註解 | 中（例外清單可能有漏） | ✅ |

### 2.1 不在範圍（明示 YAGNI）

- ❌ 本機 testcontainers Postgres（user 決定本機保留 SQLite）
- ❌ 重寫 `tests/conftest.py` 的 SQLite monkey-patch
- ❌ 追改 2 檔 dialect-branch migration（已 deploy，用 skip 註解保留）
- ❌ 為 5 條資料 cleanup migration 補 downgrade body（資料層 cleanup 本身難可逆）
- ❌ 補 alembic-heads 之外的 alembic gate（multi-head 警告等）
- ❌ 改 `alembic/env.py`
- ❌ 重命名 migration 檔
- ❌ 把 conftest.py 的 dialect patch 移除

---

## 3. PR1：刪除 legacy `migrations/`

### 3.1 範圍

刪除以下 8 檔：

```
migrations/add_columns.py
migrations/add_indexes.py
migrations/add_job_title_fk.py
migrations/add_office_staff_field.py
migrations/migrate_titles.py
migrations/swap_employee_columns.py
migrations/update_schema_job_titles.py
migrations/update_schema.py
```

若 `migrations/` 目錄變空，連目錄一起刪。

### 3.2 前置驗證（要在 commit 前重跑一次確認）

```bash
cd ivy-backend
grep -rEn "from migrations\b|import migrations\b" --include='*.py' . | grep -v "^\./startup/\|^\./\.claude/worktrees/"
```

- `startup/migrations.py` 是不同檔（名稱撞），不受影響
- 必須輸出空才能 commit

### 3.3 測試

- 跑既有 `pytest`：不可有回歸
- 不新增測試（純刪檔）

### 3.4 Commit message

```
chore: remove legacy psycopg2 migration scripts

These 8 scripts under migrations/ predate Alembic adoption.
None are imported by production code or tests.
add_columns.py hardcodes the legacy kindergarten_payroll DB name,
which would corrupt the wrong database if accidentally executed.
History remains in git.
```

---

## 4. PR2：CI migration roundtrip job

### 4.1 新 job 在 `.github/workflows/ci.yml`

加在現有 `alembic-heads` job 後：

```yaml
alembic-roundtrip:
  name: Alembic Roundtrip
  runs-on: ubuntu-latest
  timeout-minutes: 10

  services:
    postgres:
      image: postgres:15
      env:
        POSTGRES_USER: test
        POSTGRES_PASSWORD: test
        POSTGRES_DB: ivymanagement_roundtrip
      ports:
        - 5432:5432
      options: >-
        --health-cmd pg_isready
        --health-interval 10s
        --health-timeout 5s
        --health-retries 5

  steps:
    - uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: "3.12"
        cache: pip
        cache-dependency-path: requirements.txt

    - name: Install dependencies
      run: pip install -r requirements.txt

    - name: Roundtrip upgrade → downgrade → upgrade
      env:
        DATABASE_URL: postgresql://test:test@localhost:5432/ivymanagement_roundtrip
        ENV: development
        JWT_SECRET_KEY: ci-test-secret-key-not-for-production
      run: |
        alembic upgrade head
        alembic downgrade base
        alembic upgrade head
        echo "Roundtrip OK"
```

### 4.2 為何兩次 upgrade

第二次 `upgrade head` 是抓「downgrade 沒清乾淨」的情況：
- 若某條 downgrade 漏 drop 一張表 → 第二次 upgrade 嘗試 create 同名表 → fail
- 若某條 downgrade 沒移 enum/index → 第二次 upgrade 衝突
- 第二次 upgrade 過 = 「整段歷史可逆」的強保證

### 4.3 不灌 fixture / 不跑 ORM 測試

純 schema roundtrip。`models.Base.metadata` 與 `alembic` 狀態的對齊不在此 job 範圍（另有 follow-up 可加 `alembic check`，但先不擴）。

### 4.4 PR2 起手前的本機 dry-run

PR2 第一次 push 前，作者要在本機跑一次：

```bash
# 起一個臨時 PG 容器
docker run --rm -d --name ivy-roundtrip-test \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=ivymanagement_roundtrip \
  -p 5433:5432 postgres:15

# 等 ~5 秒讓 PG 起來
sleep 5

# 跑 roundtrip
DATABASE_URL=postgresql://test:test@localhost:5433/ivymanagement_roundtrip \
  alembic upgrade head && \
DATABASE_URL=postgresql://test:test@localhost:5433/ivymanagement_roundtrip \
  alembic downgrade base && \
DATABASE_URL=postgresql://test:test@localhost:5433/ivymanagement_roundtrip \
  alembic upgrade head

docker stop ivy-roundtrip-test
```

- 若 fail：在 PR2 內**先修不可逆的 migration**（重新審 downgrade body），不可硬把 job 設成 allow-failure
- 若這條太大（會牽連多條），切出 PR2.5 作 prep 修補，PR2 等 PR2.5 落地後再 push

### 4.5 測試

- 不需新增 pytest 測試
- 驗收 = CI 該 job 綠

### 4.6 Commit message

```
ci: add Alembic upgrade/downgrade/upgrade roundtrip job

Verifies the entire migration history is reversible on Postgres 15.
The second upgrade catches dirty downgrade leftovers (e.g. tables not
dropped, enums lingering) that would otherwise only surface in
production rollback drills.
```

---

## 5. PR3：AST downgrade 對稱 lint

### 5.1 新檔 `scripts/lint_alembic_symmetry.py`

**規格：** ~70 行純 Python 3 stdlib（`ast` + `argparse` + `pathlib`），無第三方依賴。

**輸入：** 一個或多個目錄 / 檔案路徑（預設 `alembic/versions/`）

**輸出：**
- 全部過：exit 0，stdout 印 `lint OK: <N> files checked, <M> skipped`
- 任一檔不過：exit 1，stderr 印每個違規 `<file>:<line>: upgrade has op.<X>(...), downgrade missing op.<reverse>(...)`

**對稱規則表**（hard-coded in script）：

```python
SYMMETRY_RULES = {
    "create_table": "drop_table",
    "add_column": "drop_column",
    "create_index": "drop_index",
    "create_foreign_key": "drop_constraint",
    "create_unique_constraint": "drop_constraint",
    "create_check_constraint": "drop_constraint",
    "create_primary_key": "drop_constraint",
}
```

**反向也檢查：** 若 upgrade 有 `drop_table` 但 downgrade 沒 `create_table` → fail（雖然這在 alembic 寫法極罕見）。

```python
REVERSE_RULES = {v: k for k, v in SYMMETRY_RULES.items()}
```

**不檢查的 op：** `alter_column`、`execute`、`bulk_insert`、`rename_table`、`batch_alter_table`（含子 op 時複雜，第一版略過）。

**Merge migration 自動跳過：** 若檔內找到 `down_revision = (` 或 `down_revision: tuple` → skip。

**Skip 註解語法：** 檔頭（前 10 行任一）出現 `# alembic-lint: skip-symmetry` → skip，原因必須附在同註解或下一行 `#` 後。

**Skip 統計：** stdout 印 `skipped: <file> (reason: ...)` 方便 review 看清單長度。

### 5.2 演算法

```python
def lint_one_file(path: Path) -> list[str]:
    """Return list of violation messages. Empty list = pass."""
    src = path.read_text()
    if has_skip_marker(src):
        return []
    tree = ast.parse(src)
    if is_merge_migration(tree):
        return []
    upgrade_ops = collect_op_calls(tree, fn_name="upgrade")
    downgrade_ops = collect_op_calls(tree, fn_name="downgrade")
    violations = []
    for op_name, lineno in upgrade_ops:
        if op_name in SYMMETRY_RULES:
            expected = SYMMETRY_RULES[op_name]
            if not any(o == expected for o, _ in downgrade_ops):
                violations.append(
                    f"{path}:{lineno}: upgrade has op.{op_name}(), "
                    f"downgrade missing op.{expected}()"
                )
    for op_name, lineno in downgrade_ops:
        if op_name in REVERSE_RULES:
            expected = REVERSE_RULES[op_name]
            if not any(o == expected for o, _ in upgrade_ops):
                violations.append(
                    f"{path}:{lineno}: downgrade has op.{op_name}(), "
                    f"upgrade missing op.{expected}()"
                )
    return violations
```

`collect_op_calls()` 用 `ast.NodeVisitor`，認 `op.<name>(...)` 形式（`Attribute` node、value.id == 'op'）。不展開 `if` 分支內外的差異 — 只看 statically 出現過幾種 op，這夠涵蓋 95% case 且邏輯簡單。

### 5.3 初版例外名單

PR3 commit 同時為 7 檔加註解（**僅加註解、不改其他內容**）：

| 檔 | Skip 原因註解 |
|---|---|
| `20260417_v4w5x6y7z8a9_add_activity_academic_term.py` | `# alembic-lint: skip-symmetry (legacy dialect branches; already shipped, do not retroactively rewrite)` |
| `20260418_w5x6y7z8a9b0_add_activity_pending_review_and_classroom_fk.py` | 同上 |
| `20260416_r9s0t1u2v3w4_cleanup_sync_raw_data.py` | `# alembic-lint: skip-symmetry (data-only cleanup; deleted rows not preserved for restore)` |
| `20260416_s0t1u2v3w4x5_remove_duplicate_indexes.py` | `# alembic-lint: skip-symmetry (dedup of redundant indexes; original duplicates intentionally not restored)` |
| `20260427_g2c3d4e5f6g7_backfill_employee_classroom.py` | `# alembic-lint: skip-symmetry (data backfill; prior NULL state ambiguous to restore)` |
| `20260502_9e4549832715_auto_approve_pending_student_leaves.py` | `# alembic-lint: skip-symmetry (data state transition; downgrade would not know which rows to revert)` |
| `20260507_n9o0p1q2r3s4_truncate_orphaned_sync_raw_data.py` | `# alembic-lint: skip-symmetry (orphan truncation; deleted rows not restorable)` |

> 5 條 merge migration（`down_revision = (...,...)`）由 lint 自動偵測 tuple 跳過，不需註解。

### 5.4 CI 接入

加在 `alembic-heads` job 後：

```yaml
alembic-symmetry-lint:
  name: Alembic Symmetry Lint
  runs-on: ubuntu-latest
  timeout-minutes: 3

  steps:
    - uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: "3.12"

    - name: Run symmetry lint
      run: python scripts/lint_alembic_symmetry.py alembic/versions/
```

**不需安裝 requirements.txt** — 純 stdlib。

### 5.5 Pre-commit hook（若 repo 有）

`grep -l "pre-commit" .pre-commit-config.yaml 2>/dev/null || echo "no pre-commit config"`

- 有 → 加 `local` hook 跑 lint script
- 無 → 略過，CI gate 已足夠

### 5.6 測試 `tests/test_alembic_symmetry_lint.py`

| Case | 期望 |
|---|---|
| Fixture：upgrade=create_table + downgrade=drop_table | pass |
| Fixture：upgrade=create_table，downgrade=空 | fail，訊息含 `drop_table` |
| Fixture：upgrade=add_column×2，downgrade=drop_column×1 | pass（規則只看「至少一個對應 op 存在」，不算 column 次數 — 簡化版） |
| Fixture：檔頭含 `# alembic-lint: skip-symmetry (xxx)` | skip，回傳空 violations |
| Fixture：`down_revision = ('a', 'b')` | skip（merge） |
| Fixture：upgrade=alter_column（不在規則表） | pass（不檢查） |
| Fixture：upgrade=drop_table，downgrade=空 | fail（reverse 規則生效） |
| 跑既有 163 檔 | 0 violations |

> 「at least one 對應 op 存在」是刻意妥協 — 不去 match 參數（表名 / 欄名），避免名字重複或 alembic 變數寫法導致 false positive。第一版求穩、可後續加 stricter mode。

### 5.7 Commit message

```
ci: add AST-based Alembic symmetry lint

scripts/lint_alembic_symmetry.py uses ast.parse() to verify every
op.create_*/add_column in upgrade() has a matching op.drop_* in
downgrade(). Exceptions are opt-in via `# alembic-lint: skip-symmetry`
header comment, with reason required. 7 existing files annotated.
Merge migrations auto-skipped. CI gate added.
```

---

## 6. 接 PR 與整體驗收

### 6.1 整體驗收

- ✅ `git ls-files migrations/` 空（PR1）
- ✅ CI `alembic-roundtrip` job 綠（PR2）
- ✅ CI `alembic-symmetry-lint` job 綠跑既有 163 檔零 false positive（PR3）
- ✅ pytest 全綠（三 PR 各自）

### 6.2 三 PR 為何可並行

- PR1 改 `migrations/` 目錄
- PR2 改 `.github/workflows/ci.yml`（新增 job block）
- PR3 改 `.github/workflows/ci.yml`（新增 job block） + 加 `scripts/` + 加 `tests/` + 加 7 條註解到 `alembic/versions/`

PR2 + PR3 都動 `ci.yml`，但各加各自的 `job` block，merge 時的衝突是純文字 conflict，雙方 review 不需協同。

### 6.3 三 PR 為何不能合成一個

- PR1 純刪檔，零 review 負擔
- PR2 動 CI infra，需要在 PR2 起手前本機 dry-run（4.4 節）
- PR3 引入新 script + 規則表 + 例外清單，是三個中 review 最重的

合 PR 會把 PR3 的設計討論卡 PR1 的進度。

---

## 7. 風險與緩解

| 風險 | 緩解 |
|---|---|
| PR2 首跑爆既有不可逆 migration | 4.4 強制 PR 起手前本機 dry-run。若爆，先切 PR2.5 修補對應 downgrade。 |
| PR3 例外清單漏抓 false positive | 5.6 驗收要求「跑既有 163 檔零 false positive」。實作完先 dry-run 才送 PR。 |
| PR3 lint 過於寬鬆（只 match op 種類不 match 表名）讓真 bug 漏網 | 接受第一版妥協（5.6 末段註解），follow-up 可加 strict mode。 |
| 7 條 skip 註解未來被誤用（變成 escape hatch） | 註解強制附原因；新加 skip 在 PR review 階段必被質疑。 |
| 並行 PR2 / PR3 衝到 ci.yml | 後 merge 的 rebase 解一次 trivial conflict。 |

---

## 8. Follow-ups（不在 Phase A）

- 為 PR3 lint 加 strict mode：match 表名 / 欄名而非僅 op 種類
- 引入 `alembic check`（檢查 model vs migration schema 漂移）
- 把本機 SQLite monkey-patch 重新評估（觀察 testcontainers 在 dev 機器啟動成本是否可接受）
- 把 conftest 的 dialect patch 收斂到 helper（不要散落在頂端）

---

## 9. 命名與位置

- Spec：`ivy-backend/docs/superpowers/specs/2026-05-21-migration-quality-hardening-design.md`（本檔）
- Plan：`ivy-backend/docs/superpowers/plans/2026-05-21-migration-quality-hardening-plan.md`（writing-plans 階段產出）
- Lint script：`ivy-backend/scripts/lint_alembic_symmetry.py`
- Lint test：`ivy-backend/tests/test_alembic_symmetry_lint.py`
