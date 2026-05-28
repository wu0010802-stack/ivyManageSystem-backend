#!/usr/bin/env python3
"""禁止 Pydantic Out schema 暴露 PII 欄位（denylist substring 命中即 fail）。

對齊 utils/sentry_init._PII_KEY_SUBSTRINGS。僅檢查 `schemas/` 下檔案內
class 名以 "Out" 結尾的 Pydantic 模型欄位名稱；In/Patch/Query 不檢查
（request body 本就含 PII）。

例外機制（inline comment）：
    class EmployeeOut(IvyBaseModel):
        id_number: Optional[str] = None  # pii-allow: admin 端必看

inline `# pii-allow:` 後跟原因即視為合法 PII，跳過檢查。
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"

# 從 utils/sentry_init 載入 denylist + exempt
sys.path.insert(0, str(REPO_ROOT))
from utils.sentry_init import (
    _PII_KEY_EXEMPT_SUBSTRINGS,
    _PII_KEY_SUBSTRINGS,
)  # noqa: E402


def parse_pii_allow_lines(path: Path) -> set[int]:
    """取出有 `# pii-allow:` 註解的行號集合。"""
    allow_lines: set[int] = set()
    with path.open("rb") as f:
        try:
            tokens = tokenize.tokenize(f.readline)
            for tok in tokens:
                if tok.type == tokenize.COMMENT and "pii-allow" in tok.string:
                    allow_lines.add(tok.start[0])
        except tokenize.TokenizeError:
            pass
    return allow_lines


def scan_file(path: Path) -> list[tuple[int, str, str, str]]:
    """回傳 [(lineno, class.field, denied_substring, reason)] for PII leaks."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    allow_lines = parse_pii_allow_lines(path)
    errors: list[tuple[int, str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.name.endswith("Out"):
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign):
                continue
            if not isinstance(item.target, ast.Name):
                continue
            fname_lower = item.target.id.lower()
            # exempt 優先
            if any(s in fname_lower for s in _PII_KEY_EXEMPT_SUBSTRINGS):
                continue
            for denied in _PII_KEY_SUBSTRINGS:
                if denied in fname_lower:
                    # AnnAssign 可能跨多行（formatter 包行）；範圍內任一行有 pii-allow 即視為例外
                    end_ln = getattr(item, "end_lineno", item.lineno) or item.lineno
                    if any(ln in allow_lines for ln in range(item.lineno, end_ln + 1)):
                        break  # inline allow
                    errors.append(
                        (item.lineno, f"{node.name}.{item.target.id}", denied, "")
                    )
                    break
    return errors


def main() -> int:
    fail = False
    for f in sorted(SCHEMAS_DIR.rglob("*.py")):
        if f.name.startswith("_") and f.name not in {"_base.py"}:
            continue
        errors = scan_file(f)
        if errors:
            print(f"PII leak in {f.relative_to(REPO_ROOT)}:")
            for ln, fld, denied, _ in errors:
                print(
                    f"  L{ln}  {fld}  含 PII '{denied}' （加 # pii-allow: <reason> 例外）"
                )
            fail = True

    if not fail:
        print("OK: schemas Out classes 無未標註 PII 欄位")

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
