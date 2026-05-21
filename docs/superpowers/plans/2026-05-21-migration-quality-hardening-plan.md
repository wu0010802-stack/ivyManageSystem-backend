# Migration 品質硬化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land three independent PRs that harden Alembic migration quality: delete dead legacy psycopg2 scripts, add CI roundtrip safety net, add AST-based downgrade symmetry lint.

**Architecture:** Three PRs from three branches off `main`, each individually mergeable and reviewable. PR1 is a pure deletion. PR2 adds a single CI job. PR3 adds a stdlib-only Python script, its pytest suite, a CI job, and 7 inline skip annotations on already-shipped migrations. No production runtime code changes.

**Tech Stack:** Python 3.12 stdlib (`ast`, `pathlib`), Alembic, GitHub Actions, pytest, Postgres 15.

**Spec:** `docs/superpowers/specs/2026-05-21-migration-quality-hardening-design.md`

**Branch convention:** `feat/<feature>-2026-05-21-backend`

---

## Pre-flight (read before starting any PR)

Repo is `~/Desktop/ivy-backend`. Workspace is `~/Desktop/ivyManageSystem` (multi-repo wrapper, not itself a git repo — actual git is in `ivy-backend`).

There is significant uncommitted WIP from parallel sessions (~50 `M` files visible at planning time). **Each PR must be done in its own worktree** (or its own clean branch) so it does not pull in unrelated changes. The standard pattern:

```bash
cd ~/Desktop/ivy-backend
git fetch origin
git worktree add .claude/worktrees/<feature>-2026-05-21-backend \
  -b feat/<feature>-2026-05-21-backend origin/main
cd .claude/worktrees/<feature>-2026-05-21-backend
```

After each PR is merged, clean up:

```bash
cd ~/Desktop/ivy-backend
git worktree remove .claude/worktrees/<feature>-2026-05-21-backend
git branch -D feat/<feature>-2026-05-21-backend
```

Each PR's tasks below assume CWD = the worktree root.

The three PRs are **independent and can be done in any order or in parallel**. The simplest order (lowest risk first) is PR1 → PR2 → PR3, but nothing forces it.

---

## File Structure (across all three PRs)

| Path | Status | Owner PR | Responsibility |
|---|---|---|---|
| `migrations/*.py` (8 files) | DELETE | PR1 | (gone; git history retains) |
| `.github/workflows/ci.yml` | MODIFY | PR2 | Add `alembic-roundtrip` job |
| `.github/workflows/ci.yml` | MODIFY | PR3 | Add `alembic-symmetry-lint` job (separate block from PR2's edit) |
| `scripts/lint_alembic_symmetry.py` | CREATE | PR3 | AST-based symmetry checker, ~140 lines stdlib-only |
| `tests/test_alembic_symmetry_lint.py` | CREATE | PR3 | pytest covering 8 cases |
| `alembic/versions/20260417_v4w5x6y7z8a9_add_activity_academic_term.py` | MODIFY (1 line comment) | PR3 | Add skip marker |
| `alembic/versions/20260418_w5x6y7z8a9b0_add_activity_pending_review_and_classroom_fk.py` | MODIFY (1 line comment) | PR3 | Add skip marker |
| `alembic/versions/20260416_r9s0t1u2v3w4_cleanup_sync_raw_data.py` | MODIFY (1 line comment) | PR3 | Add skip marker |
| `alembic/versions/20260416_s0t1u2v3w4x5_remove_duplicate_indexes.py` | MODIFY (1 line comment) | PR3 | Add skip marker |
| `alembic/versions/20260427_g2c3d4e5f6g7_backfill_employee_classroom.py` | MODIFY (1 line comment) | PR3 | Add skip marker |
| `alembic/versions/20260502_9e4549832715_auto_approve_pending_student_leaves.py` | MODIFY (1 line comment) | PR3 | Add skip marker |
| `alembic/versions/20260507_n9o0p1q2r3s4_truncate_orphaned_sync_raw_data.py` | MODIFY (1 line comment) | PR3 | Add skip marker |

---

# PR1 — Delete legacy `migrations/`

**Branch:** `feat/remove-legacy-migrations-2026-05-21-backend`

## Task 1.1: Verify zero imports

**Files:** none (read-only investigation)

- [ ] **Step 1: Grep for any production / test import of `migrations` package**

Run:

```bash
grep -rEn "from migrations\b|import migrations\b" \
  --include='*.py' . \
  | grep -v "^\./startup/" \
  | grep -v "^\./\.claude/worktrees/" \
  | grep -v "^\./migrations/"
```

Expected output: empty.

If output is **non-empty**, STOP. There is a real import. Surface it back to user and do not delete.

`startup/migrations.py` is a different file (name collision) and is excluded above. `.claude/worktrees/` are isolated copies and also excluded.

## Task 1.2: Delete the 8 files

**Files:**
- Delete: `migrations/add_columns.py`
- Delete: `migrations/add_indexes.py`
- Delete: `migrations/add_job_title_fk.py`
- Delete: `migrations/add_office_staff_field.py`
- Delete: `migrations/migrate_titles.py`
- Delete: `migrations/swap_employee_columns.py`
- Delete: `migrations/update_schema_job_titles.py`
- Delete: `migrations/update_schema.py`

- [ ] **Step 1: Confirm exactly 8 files exist**

Run:

```bash
ls migrations/
```

Expected: the 8 file names above. If the directory has `__init__.py` or other files, surface to user before proceeding.

- [ ] **Step 2: Delete via git rm**

Run:

```bash
git rm migrations/add_columns.py \
       migrations/add_indexes.py \
       migrations/add_job_title_fk.py \
       migrations/add_office_staff_field.py \
       migrations/migrate_titles.py \
       migrations/swap_employee_columns.py \
       migrations/update_schema_job_titles.py \
       migrations/update_schema.py
```

- [ ] **Step 3: Remove now-empty directory if applicable**

Run:

```bash
rmdir migrations/ 2>/dev/null || echo "directory still has files; check ls migrations/"
```

If `rmdir` failed and `ls migrations/` shows leftover files, **STOP** and surface to user.

## Task 1.3: Verify no regression

**Files:** none (test run)

- [ ] **Step 1: Run pytest**

Run:

```bash
pytest tests/ -x --tb=short -q
```

Expected: all green (modulo pre-existing failures unrelated to migrations).

If a new failure surfaces (e.g. some test importing `migrations.xxx`), STOP and restore the deleted file:

```bash
git restore --staged --worktree migrations/<file>
```

Then surface to user.

## Task 1.4: Commit

- [ ] **Step 1: Commit the deletion**

Run:

```bash
git commit -m "$(cat <<'EOF'
chore: remove legacy psycopg2 migration scripts

These 8 scripts under migrations/ predate Alembic adoption.
None are imported by production code or tests.
add_columns.py hardcodes the legacy kindergarten_payroll DB name,
which would corrupt the wrong database if accidentally executed.
History remains in git.
EOF
)"
```

- [ ] **Step 2: Verify commit**

Run:

```bash
git log -1 --stat
```

Expected: 8 file deletions, ~80 lines removed.

- [ ] **Step 3: Surface ready-to-push**

Stop here. Inform user PR1 branch is ready to push and open PR.

---

# PR2 — CI Alembic roundtrip job

**Branch:** `feat/alembic-roundtrip-ci-2026-05-21-backend`

## Task 2.1: Local dry-run on Postgres

This must pass before we add the CI job — otherwise we ship a broken CI gate.

**Files:** none (verification only)

- [ ] **Step 1: Start a temp Postgres 15 container**

Run:

```bash
docker run --rm -d --name ivy-roundtrip-test \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=ivymanagement_roundtrip \
  -p 5433:5432 postgres:15
```

Wait ~5 seconds for PG to come up:

```bash
sleep 5
docker exec ivy-roundtrip-test pg_isready -U test
```

Expected: `localhost:5432 - accepting connections`

- [ ] **Step 2: Run the roundtrip**

Run:

```bash
export DATABASE_URL=postgresql://test:test@localhost:5433/ivymanagement_roundtrip
export ENV=development
export JWT_SECRET_KEY=ci-test-secret-key-not-for-production
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

Expected: three commands all succeed; final output shows `head` revision applied. No SQL errors.

If **upgrade fails**: a migration is broken on PG (likely never tested on PG). Surface to user — this is a real bug, not a plan issue.

If **downgrade fails**: a downgrade body is broken or missing. Surface to user with the offending revision; PR2 cannot ship until that revision is fixed (may need a PR2.5 prep PR).

If **second upgrade fails**: the corresponding downgrade left dirty state. Same handling — surface offending revision.

- [ ] **Step 3: Tear down container**

Run:

```bash
docker stop ivy-roundtrip-test
```

(`--rm` removes it automatically when stopped.)

## Task 2.2: Add the CI job

**Files:**
- Modify: `.github/workflows/ci.yml`

Locate the existing `alembic-heads` job (search for `alembic-heads:` in the file). Insert the new job **immediately after** the closing of `alembic-heads` (i.e. before the next `name:` at the same indentation level).

- [ ] **Step 1: Read the current ci.yml**

Run:

```bash
grep -n "^  alembic-heads:" .github/workflows/ci.yml
grep -n "^  [a-z].*:" .github/workflows/ci.yml | head -20
```

Identify the line range of the `alembic-heads` job (from its `name:` to the last line before the next job's `name:`).

- [ ] **Step 2: Append the new job block to ci.yml**

Use the Edit tool to insert this block immediately after the `alembic-heads` job's last line:

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

(Leading blank line is intentional — separates jobs.)

- [ ] **Step 3: Lint the YAML**

Run:

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Expected: no error.

If error: re-check indentation. GitHub Actions YAML is sensitive to 2-space indents.

## Task 2.3: Commit

- [ ] **Step 1: Stage and commit**

Run:

```bash
git add .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
ci: add Alembic upgrade/downgrade/upgrade roundtrip job

Verifies the entire migration history is reversible on Postgres 15.
The second upgrade catches dirty downgrade leftovers (e.g. tables not
dropped, enums lingering) that would otherwise only surface in
production rollback drills.

Locally dry-run on Postgres 15 before committing — see plan §2.1.
EOF
)"
```

- [ ] **Step 2: Verify commit**

Run:

```bash
git log -1 --stat
```

Expected: 1 file modified, ~45 lines added.

- [ ] **Step 3: Surface ready-to-push**

Stop here. Inform user PR2 branch is ready to push.

---

# PR3 — AST downgrade symmetry lint

**Branch:** `feat/alembic-symmetry-lint-2026-05-21-backend`

This PR has the most pieces. Order:

1. Write failing tests
2. Write the lint script to make them pass
3. Run lint on existing 163 files — expect violations on 7 specific files
4. Annotate those 7 files with skip markers
5. Re-run lint — expect zero violations
6. Add CI job
7. Commit

## Task 3.1: Write failing tests

**Files:**
- Create: `tests/test_alembic_symmetry_lint.py`

- [ ] **Step 1: Create the test file**

```python
"""Tests for scripts/lint_alembic_symmetry.py."""
import sys
import textwrap
from pathlib import Path

import pytest

# Make scripts/ importable for tests
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from lint_alembic_symmetry import classify_and_lint  # noqa: E402


def _write_migration(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(textwrap.dedent(body).lstrip())
    return f


def test_create_table_with_matching_drop_passes(tmp_path):
    f = _write_migration(tmp_path, "m_pass.py", '''
        """add foo"""
        revision = "abc123"
        down_revision = "xyz789"

        def upgrade():
            op.create_table("foo")

        def downgrade():
            op.drop_table("foo")
    ''')
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_create_table_without_drop_fails(tmp_path):
    f = _write_migration(tmp_path, "m_fail.py", '''
        """add foo"""
        revision = "abc123"
        down_revision = "xyz789"

        def upgrade():
            op.create_table("foo")

        def downgrade():
            pass
    ''')
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert len(violations) == 1
    assert "create_table" in violations[0]
    assert "drop_table" in violations[0]


def test_count_mismatch_passes(tmp_path):
    """First-version intentional weakness: 2 add_column + 1 drop_column passes."""
    f = _write_migration(tmp_path, "m_count.py", '''
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.add_column("t", sa.Column("a"))
            op.add_column("t", sa.Column("b"))

        def downgrade():
            op.drop_column("t", "a")
    ''')
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_skip_marker_in_header_skips(tmp_path):
    f = _write_migration(tmp_path, "m_skip.py", '''
        """add foo"""
        # alembic-lint: skip-symmetry (data-only cleanup)
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.create_table("foo")

        def downgrade():
            pass
    ''')
    status, violations = classify_and_lint(f)
    assert status == "skipped:marker"
    assert violations == []


def test_merge_migration_with_tuple_down_revision_skips(tmp_path):
    f = _write_migration(tmp_path, "m_merge.py", '''
        """merge heads"""
        revision = "merge1"
        down_revision = ("head_a", "head_b")

        def upgrade():
            pass

        def downgrade():
            pass
    ''')
    status, violations = classify_and_lint(f)
    assert status == "skipped:merge"
    assert violations == []


def test_alter_column_not_checked_passes(tmp_path):
    """op.alter_column is intentionally out of scope (too complex to symmetric-check)."""
    f = _write_migration(tmp_path, "m_alter.py", '''
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.alter_column("t", "c", type_=sa.String(64))

        def downgrade():
            pass
    ''')
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_drop_table_without_create_fails_reverse(tmp_path):
    """Reverse rule: if downgrade has drop_X, upgrade should have create_X."""
    f = _write_migration(tmp_path, "m_reverse.py", '''
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            pass

        def downgrade():
            op.drop_table("foo")
    ''')
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert len(violations) == 1
    assert "drop_table" in violations[0]
    assert "create_table" in violations[0]


def test_create_index_with_drop_index_passes(tmp_path):
    f = _write_migration(tmp_path, "m_index.py", '''
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.create_index("ix_foo", "foo", ["a"])

        def downgrade():
            op.drop_index("ix_foo", "foo")
    ''')
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []


def test_create_fk_with_drop_constraint_passes(tmp_path):
    """All create_foreign_key/unique/check map to op.drop_constraint."""
    f = _write_migration(tmp_path, "m_fk.py", '''
        revision = "abc"
        down_revision = "xyz"

        def upgrade():
            op.create_foreign_key("fk_a", "t1", "t2", ["a"], ["id"])

        def downgrade():
            op.drop_constraint("fk_a", "t1")
    ''')
    status, violations = classify_and_lint(f)
    assert status == "checked"
    assert violations == []
```

Save to `tests/test_alembic_symmetry_lint.py`.

- [ ] **Step 2: Run tests — verify they all fail (ImportError, no script yet)**

Run:

```bash
pytest tests/test_alembic_symmetry_lint.py -v
```

Expected: every test errors with `ModuleNotFoundError: No module named 'lint_alembic_symmetry'`.

That confirms we have not yet implemented the script. Proceed to next task.

## Task 3.2: Write the lint script

**Files:**
- Create: `scripts/lint_alembic_symmetry.py`

- [ ] **Step 1: Create the script**

```python
#!/usr/bin/env python3
"""AST-based Alembic upgrade/downgrade symmetry lint.

Flags migrations where upgrade() has op.create_table / add_column / create_index /
create_foreign_key / create_unique_constraint / create_check_constraint /
create_primary_key but downgrade() lacks the matching reverse op.

Also checks the reverse direction (downgrade has drop_* but upgrade lacks create_*).

Exceptions:
  - Merge migrations (down_revision = (a, b, ...)) are auto-skipped.
  - Any file with `# alembic-lint: skip-symmetry (reason)` in the first 10
    lines is skipped. Reason MUST be included; reviewers enforce.

Out of scope (intentional first-version weakness):
  - alter_column, execute, bulk_insert, rename_table, batch_alter_table.
  - Count matching: 2 add_column + 1 drop_column passes (we only check at least
    one matching op exists). Reviewers catch the rest in PR review.
"""
import ast
import sys
from pathlib import Path
from typing import Optional

SYMMETRY_RULES = {
    "create_table": "drop_table",
    "add_column": "drop_column",
    "create_index": "drop_index",
    "create_foreign_key": "drop_constraint",
    "create_unique_constraint": "drop_constraint",
    "create_check_constraint": "drop_constraint",
    "create_primary_key": "drop_constraint",
}
REVERSE_RULES = {
    "drop_table": "create_table",
    "drop_column": "add_column",
    "drop_index": "create_index",
    # drop_constraint maps to multiple creators; we don't reverse-check it
    # (would produce false positives — many drops are paired by name only).
}
SKIP_MARKER = "alembic-lint: skip-symmetry"


def has_skip_marker(src: str) -> bool:
    """Return True if first 10 lines contain the skip marker."""
    for line in src.splitlines()[:10]:
        if SKIP_MARKER in line:
            return True
    return False


def is_merge_migration(tree: ast.Module) -> bool:
    """down_revision = (a, b, ...) → merge migration."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "down_revision":
                if isinstance(node.value, ast.Tuple):
                    return True
    return False


def _find_function(tree: ast.Module, name: str) -> Optional[ast.FunctionDef]:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _collect_op_calls(fn: ast.FunctionDef) -> list[tuple[str, int]]:
    """Return [(op_name, lineno), ...] for every op.X(...) call in fn."""
    results: list[tuple[str, int]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id == "op"
        ):
            results.append((f.attr, node.lineno))
    return results


def classify_and_lint(path: Path) -> tuple[str, list[str]]:
    """Classify a migration file and return any violations.

    Returns:
        (status, violations) where status is one of:
          - "checked": file was linted; violations list may be empty or non-empty
          - "skipped:marker": file has `# alembic-lint: skip-symmetry` marker
          - "skipped:merge": file is a merge migration (tuple down_revision)
    """
    src = path.read_text()
    if has_skip_marker(src):
        return ("skipped:marker", [])
    tree = ast.parse(src)
    if is_merge_migration(tree):
        return ("skipped:merge", [])

    up_fn = _find_function(tree, "upgrade")
    down_fn = _find_function(tree, "downgrade")
    if up_fn is None or down_fn is None:
        # Not a normal alembic migration; skip cleanly.
        return ("checked", [])

    up_ops = _collect_op_calls(up_fn)
    down_ops = _collect_op_calls(down_fn)
    up_names = {name for name, _ in up_ops}
    down_names = {name for name, _ in down_ops}

    violations: list[str] = []
    for op_name, lineno in up_ops:
        if op_name in SYMMETRY_RULES:
            expected = SYMMETRY_RULES[op_name]
            if expected not in down_names:
                violations.append(
                    f"{path}:{lineno}: upgrade has op.{op_name}(), "
                    f"downgrade missing op.{expected}()"
                )
    for op_name, lineno in down_ops:
        if op_name in REVERSE_RULES:
            expected = REVERSE_RULES[op_name]
            if expected not in up_names:
                violations.append(
                    f"{path}:{lineno}: downgrade has op.{op_name}(), "
                    f"upgrade missing op.{expected}()"
                )
    return ("checked", violations)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage: lint_alembic_symmetry.py <dir-or-file> [<dir-or-file> ...]",
            file=sys.stderr,
        )
        return 2

    paths: list[Path] = []
    for arg in argv[1:]:
        p = Path(arg)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.py")))
        elif p.is_file():
            paths.append(p)
        else:
            print(f"Path not found: {arg}", file=sys.stderr)
            return 2

    checked = 0
    skipped_marker = 0
    skipped_merge = 0
    all_violations: list[str] = []

    for p in paths:
        if p.name == "__init__.py":
            continue
        status, violations = classify_and_lint(p)
        if status == "skipped:marker":
            skipped_marker += 1
        elif status == "skipped:merge":
            skipped_merge += 1
        else:
            checked += 1
            all_violations.extend(violations)

    for v in all_violations:
        print(v, file=sys.stderr)

    if all_violations:
        print(
            f"\nlint FAILED: {len(all_violations)} violations across {checked} files",
            file=sys.stderr,
        )
        return 1

    print(
        f"lint OK: {checked} files checked, "
        f"{skipped_marker} skipped (marker), {skipped_merge} skipped (merge)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

Save to `scripts/lint_alembic_symmetry.py`.

- [ ] **Step 2: Make it executable**

Run:

```bash
chmod +x scripts/lint_alembic_symmetry.py
```

- [ ] **Step 3: Run the test suite**

Run:

```bash
pytest tests/test_alembic_symmetry_lint.py -v
```

Expected: all 9 tests pass.

If any test fails, **read the failure carefully**. Likely causes:
- Indentation in test fixture body (use `textwrap.dedent` correctly)
- The `sys.path.insert(0, ...)` line in the test file points to wrong path

Fix and re-run until all green.

## Task 3.3: Dry-run lint on existing 163 migrations

**Files:** none (verification)

- [ ] **Step 1: Run lint against alembic/versions/**

Run:

```bash
python3 scripts/lint_alembic_symmetry.py alembic/versions/
```

Expected output: a list of violations, then `lint FAILED: ...`.

The violations should come from the 7 files identified in spec §5.3. Verify by counting:

```bash
python3 scripts/lint_alembic_symmetry.py alembic/versions/ 2>&1 \
  | grep -oE "alembic/versions/[^:]+" | sort -u
```

Expected unique file list:

```
alembic/versions/20260416_r9s0t1u2v3w4_cleanup_sync_raw_data.py
alembic/versions/20260416_s0t1u2v3w4x5_remove_duplicate_indexes.py
alembic/versions/20260417_v4w5x6y7z8a9_add_activity_academic_term.py
alembic/versions/20260418_w5x6y7z8a9b0_add_activity_pending_review_and_classroom_fk.py
alembic/versions/20260427_g2c3d4e5f6g7_backfill_employee_classroom.py
alembic/versions/20260502_9e4549832715_auto_approve_pending_student_leaves.py
alembic/versions/20260507_n9o0p1q2r3s4_truncate_orphaned_sync_raw_data.py
```

If the list is **exactly these 7 files**: proceed to Task 3.4 to annotate them.

If the list contains **other files**: surface to user. Either there is a real bug in one of those migrations (rare, since they've all been deployed) or the lint is over-strict. Inspect the offender and decide case-by-case.

If the list is **missing one or more of the 7**: the lint script logic is wrong. Re-examine.

## Task 3.4: Annotate the 7 known-exception files with skip markers

**Files:** Modify each of the 7 files listed above. **One-line comment insertion only** — do not touch any other code in these files.

The skip comment goes on **line 1** of the file body (the module-level area), before any imports or assignment. Format:

```python
# alembic-lint: skip-symmetry (<reason>)
```

The 7 reasons (per spec §5.3):

| File | Reason text |
|---|---|
| `20260417_v4w5x6y7z8a9_add_activity_academic_term.py` | `legacy dialect branches; already shipped, do not retroactively rewrite` |
| `20260418_w5x6y7z8a9b0_add_activity_pending_review_and_classroom_fk.py` | `legacy dialect branches; already shipped, do not retroactively rewrite` |
| `20260416_r9s0t1u2v3w4_cleanup_sync_raw_data.py` | `data-only cleanup; deleted rows not preserved for restore` |
| `20260416_s0t1u2v3w4x5_remove_duplicate_indexes.py` | `dedup of redundant indexes; original duplicates intentionally not restored` |
| `20260427_g2c3d4e5f6g7_backfill_employee_classroom.py` | `data backfill; prior NULL state ambiguous to restore` |
| `20260502_9e4549832715_auto_approve_pending_student_leaves.py` | `data state transition; downgrade would not know which rows to revert` |
| `20260507_n9o0p1q2r3s4_truncate_orphaned_sync_raw_data.py` | `orphan truncation; deleted rows not restorable` |

- [ ] **Step 1: For each file, read current first line and decide insertion point**

A typical alembic-generated file starts:

```python
"""description

Revision ID: xxxxxx
...
"""
```

The skip marker should go **above the docstring** so it's parsed as a top-of-module comment that survives any docstring edits. Example after insertion:

```python
# alembic-lint: skip-symmetry (data-only cleanup; deleted rows not preserved for restore)
"""description

Revision ID: xxxxxx
...
"""
```

Use the Edit tool to insert. For each file, the Edit pattern is:

- `old_string`: the existing first line (typically `"""...`)
- `new_string`: `# alembic-lint: skip-symmetry (<reason>)\n` + original first line

- [ ] **Step 2: Edit `20260417_v4w5x6y7z8a9_add_activity_academic_term.py`**

Read the file's first 3 lines to determine the docstring opener, then prepend the skip comment.

- [ ] **Step 3: Edit `20260418_w5x6y7z8a9b0_add_activity_pending_review_and_classroom_fk.py`**

Same pattern.

- [ ] **Step 4: Edit `20260416_r9s0t1u2v3w4_cleanup_sync_raw_data.py`**

Same.

- [ ] **Step 5: Edit `20260416_s0t1u2v3w4x5_remove_duplicate_indexes.py`**

Same.

- [ ] **Step 6: Edit `20260427_g2c3d4e5f6g7_backfill_employee_classroom.py`**

Same.

- [ ] **Step 7: Edit `20260502_9e4549832715_auto_approve_pending_student_leaves.py`**

Same.

- [ ] **Step 8: Edit `20260507_n9o0p1q2r3s4_truncate_orphaned_sync_raw_data.py`**

Same.

- [ ] **Step 9: Verify all 7 files now have the marker**

Run:

```bash
grep -l "alembic-lint: skip-symmetry" alembic/versions/*.py | sort
```

Expected: the 7 file paths above, exactly.

## Task 3.5: Re-run lint and verify clean

**Files:** none

- [ ] **Step 1: Run lint again**

Run:

```bash
python3 scripts/lint_alembic_symmetry.py alembic/versions/
```

Expected output:

```
lint OK: 151 files checked, 7 skipped (marker), 5 skipped (merge)
```

Numbers should sum to 163. If you get any violations, re-check the previous task — likely one skip annotation was inserted in the wrong place (e.g. after `def upgrade()` instead of in module header).

## Task 3.6: Add CI job

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Locate insertion point**

The new `alembic-symmetry-lint` job goes after `alembic-heads` (and after `alembic-roundtrip` if PR2 is already merged; if not, just after `alembic-heads`).

Run:

```bash
grep -n "^  [a-z].*:$" .github/workflows/ci.yml
```

Identify the last alembic-related job.

- [ ] **Step 2: Append the new job block**

Use Edit tool to insert this block:

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
        run: python3 scripts/lint_alembic_symmetry.py alembic/versions/
```

(Leading blank line separates jobs. No `requirements.txt` install needed — script is stdlib only.)

- [ ] **Step 3: Validate YAML**

Run:

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Expected: no error.

## Task 3.7: Final regression check

**Files:** none

- [ ] **Step 1: Run the new test suite**

Run:

```bash
pytest tests/test_alembic_symmetry_lint.py -v
```

Expected: all 9 tests pass.

- [ ] **Step 2: Run the whole pytest suite for regression**

Run:

```bash
pytest tests/ -x --tb=short -q
```

Expected: green (modulo pre-existing failures listed in MEMORY.md / unrelated to migrations or lint).

- [ ] **Step 3: Run the lint script one final time**

Run:

```bash
python3 scripts/lint_alembic_symmetry.py alembic/versions/
```

Expected:

```
lint OK: 151 files checked, 7 skipped (marker), 5 skipped (merge)
```

## Task 3.8: Commit

- [ ] **Step 1: Verify staged contents**

Run:

```bash
git status --short
```

Expected:

```
A  scripts/lint_alembic_symmetry.py
A  tests/test_alembic_symmetry_lint.py
M  .github/workflows/ci.yml
M  alembic/versions/20260416_r9s0t1u2v3w4_cleanup_sync_raw_data.py
M  alembic/versions/20260416_s0t1u2v3w4x5_remove_duplicate_indexes.py
M  alembic/versions/20260417_v4w5x6y7z8a9_add_activity_academic_term.py
M  alembic/versions/20260418_w5x6y7z8a9b0_add_activity_pending_review_and_classroom_fk.py
M  alembic/versions/20260427_g2c3d4e5f6g7_backfill_employee_classroom.py
M  alembic/versions/20260502_9e4549832715_auto_approve_pending_student_leaves.py
M  alembic/versions/20260507_n9o0p1q2r3s4_truncate_orphaned_sync_raw_data.py
```

10 files total. **No other files** — if you see other `M`, stash or discard before committing.

- [ ] **Step 2: Stage and commit**

Run:

```bash
git add scripts/lint_alembic_symmetry.py \
        tests/test_alembic_symmetry_lint.py \
        .github/workflows/ci.yml \
        alembic/versions/20260416_r9s0t1u2v3w4_cleanup_sync_raw_data.py \
        alembic/versions/20260416_s0t1u2v3w4x5_remove_duplicate_indexes.py \
        alembic/versions/20260417_v4w5x6y7z8a9_add_activity_academic_term.py \
        alembic/versions/20260418_w5x6y7z8a9b0_add_activity_pending_review_and_classroom_fk.py \
        alembic/versions/20260427_g2c3d4e5f6g7_backfill_employee_classroom.py \
        alembic/versions/20260502_9e4549832715_auto_approve_pending_student_leaves.py \
        alembic/versions/20260507_n9o0p1q2r3s4_truncate_orphaned_sync_raw_data.py

git commit -m "$(cat <<'EOF'
ci: add AST-based Alembic symmetry lint

scripts/lint_alembic_symmetry.py uses ast.parse() to verify every
op.create_*/add_column in upgrade() has a matching op.drop_* in
downgrade() (and vice versa). Exceptions are opt-in via
`# alembic-lint: skip-symmetry (reason)` header comment.

7 existing files annotated:
  - 2 with legacy dialect branches (add_activity_academic_term,
    add_activity_pending_review_and_classroom_fk)
  - 5 data-only cleanup / backfill / state transition migrations
    where downgrade is genuinely ambiguous

Merge migrations (down_revision = tuple) are auto-skipped.
First version intentionally skips alter_column/execute/bulk_insert/
rename_table/batch_alter_table; reviewers catch the rest.

CI gate added. Local dry-run output:
  lint OK: 151 files checked, 7 skipped (marker), 5 skipped (merge)
EOF
)"
```

- [ ] **Step 3: Verify commit**

Run:

```bash
git log -1 --stat
```

Expected: 10 files changed, ~250 lines added, ~7 lines modified (the 7 skip-comment insertions).

- [ ] **Step 4: Surface ready-to-push**

Stop here. Inform user PR3 branch is ready to push.

---

## Self-Review checklist (run after writing all tasks, before handoff)

- [ ] Spec coverage:
  - §3 PR1 → Tasks 1.1–1.4 ✓
  - §4 PR2 → Tasks 2.1–2.3 ✓
  - §5 PR3 → Tasks 3.1–3.8 ✓
  - §5.6 8 test cases → 9 test functions in Task 3.1 ✓ (split count-mismatch into its own test for clarity)
  - §5.3 7 skip annotations → Task 3.4 lists all 7 with exact reasons matching spec ✓
- [ ] No placeholders:
  - Every code block has actual content
  - Every commit message is fully written
  - Every grep / pytest command is fully formed
- [ ] Type consistency:
  - `classify_and_lint(path) -> (status, violations)` signature used consistently across Task 3.1 (tests) and Task 3.2 (implementation)
  - Status strings: `"checked"`, `"skipped:marker"`, `"skipped:merge"` — same in both
  - Function names: `has_skip_marker`, `is_merge_migration`, `classify_and_lint` — consistent

---

## Out of scope (Phase B follow-ups; do NOT do now)

- Strict mode for the lint (match table / column names, not just op types)
- `alembic check` integration (model vs migration drift)
- Re-evaluate SQLite monkey-patch in conftest
- Refactor the 2 dialect-branch migrations (already shipped; skip-symmetry suffices)
- Compose multiple plans into one PR

If during implementation any of these surface as blockers, surface to user — do not silently expand scope.
