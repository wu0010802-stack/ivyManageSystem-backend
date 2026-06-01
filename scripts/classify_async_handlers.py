#!/usr/bin/env python3
"""AST 掃描分類腳本：判定 api/ 下哪些 async route handler 可轉成 def。

用途：全 app `async def` → `def` 遷移前置。本腳本**不修改任何 production code**，
只掃 `api/**/*.py`，對每個被 router HTTP-method 裝飾器修飾的 route handler 判定要
不要把 `async def` 轉成同步 `def`，產出 JSON 清單供人工覆核。

判定規則（只針對 route handler，即 decorator 含 `<物件>.<方法>(...)` 且 `<方法>`
∈ {get, post, put, patch, delete, websocket} 的函式；物件名不限）：

- 已是 `def`（同步）→ decision = "skip"
- `@<obj>.websocket(...)` → decision = "keep"（ws 必須 async，即使無 await）
- `async def`：
  - body 內有任何 `async with` / `async for` → decision = "keep"
  - 否則蒐集 body 內所有 `Await`（不下探巢狀 def）：
    - 無 await，或所有 await 都是 `asyncio.sleep(...)` → decision = "convert"
    - 否則 → decision = "keep"
- 非 router-decorated 的 `def` / `async def`（dependency / helper）→
  decision = "non_handler_skip"（不列入轉換，避免誤轉被 await 的 helper）

輸出：JSON 陣列，每筆
  {"file", "func", "lineno", "decorator", "decision", "await_names": [...]}
最後印統計行：`convert=N keep=M skip=K`（只計三類 route handler，
non_handler_skip 不入此統計）。

用法：
    python3 scripts/classify_async_handlers.py            # 印 JSON 到 stdout
    python3 scripts/classify_async_handlers.py api/foo.py # 只掃指定檔/目錄
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
ROUTER_DECORATOR_METHODS = HTTP_METHODS | {"websocket"}


def _decorator_method(dec: ast.expr) -> str | None:
    """若 decorator 是 `<obj>.<method>(...)` 且 method 是 router HTTP-method/websocket，
    回傳 method 名（如 "get"/"websocket"），否則 None。

    物件名不限（router / app / r / appraisal_router ...），只看方法名。
    """
    # decorator 形如 @router.get("/x")（Call），func 是 Attribute
    if isinstance(dec, ast.Call):
        target = dec.func
    else:
        target = dec
    if isinstance(target, ast.Attribute) and target.attr in ROUTER_DECORATOR_METHODS:
        return target.attr
    return None


def _handler_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """回傳此函式作為 route handler 的 decorator method 名；非 handler 回 None。

    疊多個 decorator 時，只要其一是 router HTTP-method 即視為 handler。
    websocket 與 HTTP-method 並存時，websocket 優先（一律 keep）。
    """
    methods = []
    for dec in node.decorator_list:
        m = _decorator_method(dec)
        if m is not None:
            methods.append(m)
    if not methods:
        return None
    if "websocket" in methods:
        return "websocket"
    return methods[0]


def _await_call_name(call_func: ast.expr) -> str:
    """把被 await 的 Call 的 func 還原成可讀名稱，如 'asyncio.sleep' / 'ws.close'。"""
    if isinstance(call_func, ast.Attribute):
        parts = [call_func.attr]
        cur = call_func.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(call_func, ast.Name):
        return call_func.id
    return f"<{type(call_func).__name__}>"


def _await_name(await_node: ast.Await) -> str:
    """回傳被 await 的對象名稱。

    `await foo.bar()` → 'foo.bar'；`await coro`（非 Call）→ '<Name>' 之類 type label，
    確保非 Call 的 await 也不會 crash，且因名稱不等於 asyncio.sleep 而被視為真 await。
    """
    val = await_node.value
    if isinstance(val, ast.Call):
        return _await_call_name(val.func)
    return f"<{type(val).__name__}>"


def _scan_async_signals(
    body: list[ast.stmt],
) -> tuple[list[str], bool]:
    """掃 handler body，回傳 (await_names, has_async_with_or_for)。

    手寫遍歷：遇到巢狀 FunctionDef / AsyncFunctionDef 即停止下探（不計入其內層的
    await / async with / async for），符合「不下探巢狀 async def」的規則。
    Lambda 不含 await/async with，無須特別處理（其 body 為 expr，不會有這些節點）。
    """
    await_names: list[str] = []
    has_async_with_for = False

    def visit(node: ast.AST) -> None:
        """檢查 node 自身，再遞迴其子節點；遇巢狀 def 停止下探。"""
        nonlocal has_async_with_for
        if isinstance(node, ast.Await):
            await_names.append(_await_name(node))
        elif isinstance(node, (ast.AsyncWith, ast.AsyncFor)):
            has_async_with_for = True
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 不下探巢狀 def（其 await/async-with 屬於它自己）
                continue
            visit(child)

    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # body 內直接的巢狀 def（含 async def closure）不下探
            continue
        visit(stmt)
    return await_names, has_async_with_for


def _is_only_asyncio_sleep(await_names: list[str]) -> bool:
    """所有 await 名稱是否都是 asyncio.sleep（限定 attribute 形式 'asyncio.sleep'）。"""
    return all(name == "asyncio.sleep" for name in await_names)


def _classify_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef, rel_path: str
) -> dict | None:
    """分類單一函式定義；非 route handler 回 non_handler_skip 紀錄。"""
    decorator = _handler_decorator(node)
    is_async = isinstance(node, ast.AsyncFunctionDef)

    if decorator is None:
        # 非 router-decorated：dependency / helper / 被 await 的內部函式
        return {
            "file": rel_path,
            "func": node.name,
            "lineno": node.lineno,
            "decorator": None,
            "decision": "non_handler_skip",
            "await_names": [],
        }

    # 是 route handler
    if decorator == "websocket":
        # ws 必須 async，一律 keep（即使無 await）
        await_names, _ = _scan_async_signals(node.body) if is_async else ([], False)
        return {
            "file": rel_path,
            "func": node.name,
            "lineno": node.lineno,
            "decorator": "websocket",
            "decision": "keep",
            "await_names": await_names,
        }

    if not is_async:
        # 已是同步 def handler
        return {
            "file": rel_path,
            "func": node.name,
            "lineno": node.lineno,
            "decorator": decorator,
            "decision": "skip",
            "await_names": [],
        }

    # async def handler（非 websocket）
    await_names, has_async_with_for = _scan_async_signals(node.body)
    if has_async_with_for:
        decision = "keep"
    elif _is_only_asyncio_sleep(await_names):
        # 含「無 await」與「全 asyncio.sleep」兩種情形
        decision = "convert"
    else:
        decision = "keep"

    return {
        "file": rel_path,
        "func": node.name,
        "lineno": node.lineno,
        "decorator": decorator,
        "decision": decision,
        "await_names": await_names,
    }


def classify_source(source: str, rel_path: str) -> list[dict]:
    """分類一段 source（字串），回傳所有 top-level 與巢狀（class 內）函式的分類紀錄。

    只走 module / class body 的直接函式定義與 class 內的 method；route handler 在
    FastAPI 通常是 module-level，但為涵蓋 class-based 寫法也遍歷 ClassDef body。
    巢狀於函式內的 def 不視為 handler（也不輸出），其 await 已由外層分析忽略。
    """
    tree = ast.parse(source)
    results: list[dict] = []

    def walk_container(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rec = _classify_node(node, rel_path)
                if rec is not None:
                    results.append(rec)
            elif isinstance(node, ast.ClassDef):
                walk_container(node.body)

    walk_container(tree.body)
    return results


def classify_file(path: Path, root: Path) -> list[dict]:
    """分類單一檔案，file 欄位用相對 root 的路徑。"""
    rel_path = str(path.relative_to(root))
    source = path.read_text(encoding="utf-8")
    return classify_source(source, rel_path)


def _iter_py_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.rglob("*.py"))


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent  # repo 根目錄
    targets = argv[1:] if len(argv) > 1 else ["api"]

    files: list[Path] = []
    for t in targets:
        target_path = (root / t) if not Path(t).is_absolute() else Path(t)
        if not target_path.exists():
            print(f"[warn] 路徑不存在：{target_path}", file=sys.stderr)
            continue
        for py in _iter_py_files(target_path):
            if "__pycache__" not in py.parts:
                files.append(py)

    records: list[dict] = []
    for py in files:
        records.extend(classify_file(py, root))

    # 只輸出 route handler；規格輸出每筆 decision ∈ convert/keep/skip。
    # non_handler_skip（dependency / helper）既不輸出也不入統計，避免污染覆核清單。
    handler_records = [r for r in records if r["decision"] != "non_handler_skip"]

    print(json.dumps(handler_records, ensure_ascii=False, indent=2))

    convert = sum(1 for r in handler_records if r["decision"] == "convert")
    keep = sum(1 for r in handler_records if r["decision"] == "keep")
    skip = sum(1 for r in handler_records if r["decision"] == "skip")
    print(f"convert={convert} keep={keep} skip={skip}", file=sys.stderr)
    print(f"convert={convert} keep={keep} skip={skip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
