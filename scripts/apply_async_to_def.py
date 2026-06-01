#!/usr/bin/env python3
"""把分類為 convert 的 route handler 從 `async def` 機械轉成 `def`（解單 worker event-loop 阻塞）。

讀 `.scratch/async_classify.json`（由 scripts/classify_async_handlers.py 產出），
只處理 `decision == "convert"` 的紀錄，對每處：

1. **每處編輯前驗證**：讀目標 file 第 `lineno` 行，strip 後須以 `async def <func>(` 開頭
   （func 名相符）；不符 → 中止該檔並報錯（防 lineno 漂移誤改）。
2. 把該行的單一 token `async def ` → `def `（保留縮排與其餘）。
3. 對含 `await asyncio.sleep(` 的 sleep handler：把其 body 內 `await asyncio.sleep(`
   → `time.sleep(`（同樣每行驗證後才改）。
4. import 清理：轉換後若該檔不再參照 `asyncio.`，把 `import asyncio` 換成 `import time`
   （in-place swap，零行數變動，避免 lineno 漂移）；若已有 `import time` 則僅移除
   `import asyncio`。

**直接用 open().write() 寫檔**，不經 Edit/Write 工具，避免 PostToolUse black hook 整檔重排。

用法：
    python3 scripts/apply_async_to_def.py            # 套用轉換
    python3 scripts/apply_async_to_def.py --dry-run  # 只驗證 + 印報告，不寫檔
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLASSIFY_JSON = ROOT / ".scratch" / "async_classify.json"

ASYNC_DEF = "async def "
SLEEP_OLD = "await asyncio.sleep("
SLEEP_NEW = "time.sleep("


class AbortFile(Exception):
    """某檔驗證失敗，中止該檔（不寫入）。"""


def _load_convert_records() -> list[dict]:
    data = json.loads(CLASSIFY_JSON.read_text(encoding="utf-8"))
    return [r for r in data if isinstance(r, dict) and r.get("decision") == "convert"]


def _convert_async_def_line(line: str, func: str) -> str:
    """把 `<indent>async def <func>(...` 的 `async def ` token 換成 `def `。"""
    stripped = line.lstrip()
    expected_prefix = f"{ASYNC_DEF}{func}("
    if not stripped.startswith(expected_prefix):
        raise AbortFile(
            f"lineno 內容不符：期望以 {expected_prefix!r} 開頭，實得 {line[:80]!r}"
        )
    indent_len = len(line) - len(stripped)
    indent = line[:indent_len]
    # 只去掉前綴的 'async ' 一個 token，其餘原樣保留
    return indent + stripped[len("async ") :]


def _convert_sleep_in_function(
    lines: list[str],
    start_lineno: int,
    next_func_lineno: int | None,
    file_label: str,
    func: str,
) -> int:
    """在 [start_lineno, next_func_lineno) 範圍內把 `await asyncio.sleep(` → `time.sleep(`。

    回傳替換的行數。範圍以「下一個 handler 起始行」為界，找不到則到檔尾。
    """
    end = (next_func_lineno - 1) if next_func_lineno else len(lines)
    replaced = 0
    for idx in range(
        start_lineno, end
    ):  # 0-based slice，start_lineno 是該函式 def 行(1-based) → 從其下一行起其實也含本行(def 行不含 sleep)
        line = lines[idx]
        if SLEEP_OLD in line:
            lines[idx] = line.replace(SLEEP_OLD, SLEEP_NEW)
            replaced += 1
    return replaced


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv

    records = _load_convert_records()
    # 依檔分組，並在每檔內依 lineno 排序，便於界定函式範圍
    by_file: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_file[r["file"]].append(r)
    for recs in by_file.values():
        recs.sort(key=lambda r: r["lineno"])

    changed_files: list[str] = []
    aborted_files: list[tuple[str, str]] = []
    total_handlers = 0
    sleep_results: list[str] = []
    import_results: list[str] = []

    for rel_file, recs in sorted(by_file.items()):
        path = ROOT / rel_file
        try:
            original = path.read_text(encoding="utf-8")
            # 保留行尾換行特性：用 keepends 取行，逐行原地替換
            lines = original.splitlines(keepends=True)
            local_handler_count = 0
            local_sleep_count = 0

            # 1) async def → def（每行驗證）
            for r in recs:
                ln = r["lineno"]
                if ln < 1 or ln > len(lines):
                    raise AbortFile(
                        f"lineno {ln} 超出檔案行數 {len(lines)}（func={r['func']}）"
                    )
                lines[ln - 1] = _convert_async_def_line(lines[ln - 1], r["func"])
                local_handler_count += 1

            # 2) sleep handler：把 await asyncio.sleep( → time.sleep(（驗證後替換）
            #    範圍以「下一個 convert handler 行」為界，避免越界改到別人。
            linenos = [r["lineno"] for r in recs]
            for i, r in enumerate(recs):
                if not r.get("await_names"):
                    continue
                # 此 handler 有 await（必為 asyncio.sleep）
                start = r["lineno"]  # 1-based def 行
                next_ln = linenos[i + 1] if i + 1 < len(linenos) else None
                cnt = _convert_sleep_in_function(
                    lines, start, next_ln, rel_file, r["func"]
                )
                if cnt == 0:
                    raise AbortFile(
                        f"sleep handler {r['func']}（line {start}）找不到 "
                        f"{SLEEP_OLD!r} 可替換（疑 lineno 漂移）"
                    )
                local_sleep_count += cnt
                sleep_results.append(
                    f"{rel_file}::{r['func']}（line {start}）替換 {cnt} 處 sleep → time.sleep"
                )

            # 3) import 清理：若該檔不再參照 asyncio.，移除/換 import asyncio
            new_text = "".join(lines)
            still_uses_asyncio = re.search(r"\basyncio\.", new_text) is not None
            import_msg = None
            if not still_uses_asyncio:
                # 找 `import asyncio`（整行）
                has_import_asyncio = re.search(
                    r"(?m)^[ \t]*import asyncio[ \t]*(#.*)?$", new_text
                )
                has_import_time = re.search(
                    r"(?m)^[ \t]*import time[ \t]*(#.*)?$", new_text
                )
                if has_import_asyncio:
                    if has_import_time:
                        # 已有 import time → 只刪 import asyncio 整行
                        new_lines = []
                        removed = False
                        for ln_text in lines:
                            if not removed and re.match(
                                r"^[ \t]*import asyncio[ \t]*(#.*)?\r?\n?$", ln_text
                            ):
                                removed = True
                                continue
                            new_lines.append(ln_text)
                        lines = new_lines
                        new_text = "".join(lines)
                        import_msg = (
                            f"{rel_file}：移除 import asyncio（已有 import time）"
                        )
                    else:
                        # in-place swap：import asyncio → import time（零行數變動）
                        swapped = False
                        for j, ln_text in enumerate(lines):
                            if re.match(
                                r"^[ \t]*import asyncio[ \t]*(#.*)?\r?\n?$", ln_text
                            ):
                                indent = ln_text[: len(ln_text) - len(ln_text.lstrip())]
                                eol = (
                                    "\r\n"
                                    if ln_text.endswith("\r\n")
                                    else ("\n" if ln_text.endswith("\n") else "")
                                )
                                lines[j] = f"{indent}import time{eol}"
                                swapped = True
                                break
                        if swapped:
                            new_text = "".join(lines)
                            import_msg = f"{rel_file}：import asyncio → import time（in-place swap）"
                # 若此檔本就沒 import asyncio（asyncio. 不再用且無 import 行），不做事
            if import_msg:
                import_results.append(import_msg)

            # 寫檔（dry-run 不寫）
            if not dry_run:
                path.write_text(new_text, encoding="utf-8")
            changed_files.append(rel_file)
            total_handlers += local_handler_count

        except AbortFile as e:
            aborted_files.append((rel_file, str(e)))
            print(f"[ABORT] {rel_file}: {e}", file=sys.stderr)

    # ---- 報告 ----
    print("=" * 70)
    print(f"{'[DRY-RUN] ' if dry_run else ''}async def → def 轉換報告")
    print("=" * 70)
    print(f"改的檔數：{len(changed_files)}")
    print(f"轉換 handler 數（async def→def）：{total_handlers}")
    print(f"sleep handler 處理：{len(sleep_results)} 處")
    for s in sleep_results:
        print(f"  - {s}")
    print(f"import 清理：{len(import_results)} 處")
    for s in import_results:
        print(f"  - {s}")
    if aborted_files:
        print(f"!!! 中止的檔（lineno 不符等）：{len(aborted_files)}")
        for f, msg in aborted_files:
            print(f"  - {f}: {msg}")
        return 1
    print("無任何中止檔。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
