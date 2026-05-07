"""政府資料同步：通用 helper。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_of_payload(payload: Any) -> str:
    """對 JSON-serializable payload 取穩定 SHA256（鍵序排序）。"""
    serialized = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_brackets_diff(current: list[dict], new: list[dict]) -> dict:
    """比對現行 vs 新版級距。回傳：
    {
        "added": [新增的 row dict],
        "removed": [刪除的 row dict],
        "modified": [{amount, field, old, new}, ...],
    }
    用於 UI 標色。
    """
    cur_by_amt = {r["amount"]: r for r in current}
    new_by_amt = {r["amount"]: r for r in new}

    added = [r for amt, r in new_by_amt.items() if amt not in cur_by_amt]
    removed = [r for amt, r in cur_by_amt.items() if amt not in new_by_amt]
    modified = []
    for amt, new_row in new_by_amt.items():
        cur_row = cur_by_amt.get(amt)
        if cur_row is None:
            continue
        for field_name in new_row:
            if field_name == "amount":
                continue
            if cur_row.get(field_name) != new_row.get(field_name):
                modified.append(
                    {
                        "amount": amt,
                        "field": field_name,
                        "old": cur_row.get(field_name),
                        "new": new_row[field_name],
                    }
                )
    return {"added": added, "removed": removed, "modified": modified}
