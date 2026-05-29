"""掃 api/ 下定義 APIRouter 但未被 main.py（含 package __init__.py 鏈）include 的 router。

Sub-PR F：第五輪 P0 audit #22「30+ router 定義了但未掛載」初步盤點。

用法：
  python3 scripts/audit_unmounted_routers.py

輸出：candidate 列表，需人工 review 每筆才能決定 archive / docstring / 留做 internal helper。
不自動刪除（避免誤刪 internal helper 或 lazy-imported router）。
"""

from __future__ import annotations

import ast
import os
import re
import sys


def find_router_files(root: str = "api") -> list[str]:
    """所有定義 router = APIRouter() 的 .py 檔案。"""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            full = os.path.join(dirpath, f)
            with open(full, encoding="utf-8") as fh:
                content = fh.read()
            if re.search(r"^router\s*=\s*APIRouter\(", content, re.MULTILINE):
                module = full.replace("/", ".").replace(".py", "")
                if module.endswith(".__init__"):
                    continue
                out.append(module)
    return sorted(out)


def collect_imported_modules(*py_files: str) -> set[str]:
    """從多個 .py 檔案 (main.py + 所有 __init__.py) 收集所有 `from api.X import ...` 的 module path。"""
    modules = set()
    for path in py_files:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("api"):
                    modules.add(node.module)
    return modules


def collect_all_init_files(root: str = "api") -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        if "__init__.py" in files:
            out.append(os.path.join(dirpath, "__init__.py"))
    return out


def main() -> int:
    router_files = find_router_files()
    init_files = collect_all_init_files()
    imported = collect_imported_modules("main.py", *init_files)

    unmounted = [m for m in router_files if m not in imported]

    print(f"Total APIRouter-defining files: {len(router_files)}")
    print(f"Imported via main.py + __init__.py chain: {len(imported)}")
    print(f"Unmounted candidates (need manual review): {len(unmounted)}")
    print()
    for m in unmounted:
        print(f"  {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
