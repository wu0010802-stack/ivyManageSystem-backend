"""守衛測試：禁止 router 用 Depends(get_session)（非 cleanup 版）。

Why: models/base.py 的 get_session() 是普通 return（非 generator），FastAPI 對
「回傳值型」依賴不做 cleanup → 每次 request 漏一條連線，在 pool_size 有限下會
逐步耗盡連線池導致全站 500。所有 router 的 Depends 一律須用 get_session_dep。

此測試把「靠人記得」升級成「機器強制」，避免未來再漂回舊版（系統設計審查
2026-06-14 quick win）。
"""

from __future__ import annotations

import pathlib
import re

_API_DIR = pathlib.Path(__file__).resolve().parent.parent / "api"

# 比對 Depends(get_session) 但**不**比對 Depends(get_session_dep)
_BARE = re.compile(r"Depends\(\s*get_session\s*\)")


def test_no_router_uses_bare_get_session_in_depends() -> None:
    offenders: list[str] = []
    for path in _API_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _BARE.search(line):
                offenders.append(f"{path.relative_to(_API_DIR.parent)}:{lineno}")
    assert not offenders, (
        "發現 Depends(get_session)（非 cleanup 版）會洩漏連線，請改用 "
        "get_session_dep：\n" + "\n".join(offenders)
    )
