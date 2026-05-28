#!/usr/bin/env python3
"""每個 @router.<method> decorator 後必須出現 response_model= 關鍵字。

行為：
- 掃 api/ 全部 router 檔
- grandfather list (`.grandfather-no-response-model` 純文字檔，one
  `<file>:<funcname>` per line) 內的 endpoint 暫時允許缺失
- 新增 endpoint 缺 response_model 且不在 grandfather → exit 1
- 嘗試新增 grandfather 條目（git diff against origin/main）→ exit 1
  （Phase 1 之後 grandfather 只能變短不能變長）

使用方式：
    python3 scripts/check_response_model_coverage.py

CI 接入：.github/workflows/ci.yml 加 job `response_model_gate`，本 phase
不直接 enforce blocking（先讓 grandfather 落定）；下個 PR 起 enforce。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
API_DIR = REPO_ROOT / "api"
GRANDFATHER_FILE = REPO_ROOT / ".grandfather-no-response-model"
HTTP_METHODS = {"get", "post", "put", "delete", "patch"}


def load_grandfather() -> set[str]:
    if not GRANDFATHER_FILE.exists():
        return set()
    return {
        line.strip()
        for line in GRANDFATHER_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """回傳 [(lineno, funcname, key)] for endpoints missing response_model."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    rel = str(path.relative_to(REPO_ROOT))
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in HTTP_METHODS:
                continue
            # 確認 dec.func.value 是 router 名稱類（router / app / xxx_router）
            # — 過於嚴格反而會漏，這裡只認 .attr in HTTP_METHODS 即視為 endpoint
            has_rm = any(kw.arg == "response_model" for kw in dec.keywords)
            if not has_rm:
                out.append((dec.lineno, node.name, f"{rel}:{node.name}"))
    return out


def check_grandfather_only_shrinks() -> list[str]:
    """檢查 grandfather list 相對 origin/main 是否只移除沒新增。回傳新增條目清單（empty = OK）。"""
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "origin/main",
                "--",
                str(GRANDFATHER_FILE.relative_to(REPO_ROOT)),
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []  # 沒 git 或 origin/main 取不到時 skip 該 check
    added = []
    for line in result.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:].strip()
            if content and not content.startswith("#"):
                added.append(content)
    return added


def main() -> int:
    grandfather = load_grandfather()
    all_missing: list[tuple[str, int, str]] = []
    for f in sorted(API_DIR.rglob("*.py")):
        if f.name == "__init__.py":
            continue
        for lineno, funcname, key in scan_file(f):
            if key in grandfather:
                continue
            all_missing.append((str(f.relative_to(REPO_ROOT)), lineno, funcname))

    added_grandfather = check_grandfather_only_shrinks()

    fail = False
    if all_missing:
        print("Endpoints missing response_model= (新增 endpoint 必須補上)：")
        for f, ln, fn in all_missing:
            print(f"  {f}:{ln}  {fn}")
        fail = True

    if added_grandfather:
        print("\n禁止新增 grandfather 條目（只能移除清債，不能新增欠帳）：")
        for line in added_grandfather:
            print(f"  + {line}")
        fail = True

    if not fail:
        print(
            f"OK: response_model coverage check passed "
            f"(grandfather: {len(grandfather)} 條暫免)"
        )

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
