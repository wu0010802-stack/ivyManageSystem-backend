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
    """Return True if first 10 lines contain the skip marker as a `#` comment.

    Marker inside docstrings is intentionally ignored — only top-of-file
    `# alembic-lint: skip-symmetry` comments suppress the lint.
    """
    for line in src.splitlines()[:10]:
        stripped = line.lstrip()
        if stripped.startswith("#") and SKIP_MARKER in stripped:
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
    if up_fn is None and down_fn is None:
        # Not a normal alembic migration (e.g. a utility module under
        # alembic/versions/). Skip cleanly.
        return ("checked", [])

    up_ops = _collect_op_calls(up_fn) if up_fn is not None else []
    down_ops = _collect_op_calls(down_fn) if down_fn is not None else []
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
