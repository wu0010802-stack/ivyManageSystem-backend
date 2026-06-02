"""FE/BE PII denylist 與 exempt list 平行校驗。

目的：`utils/sentry_init.py` 與 `../ivy-frontend/src/utils/sentry.ts` 的
`PII_KEY_SUBSTRINGS` / `PII_KEY_EXEMPT_SUBSTRINGS` 維護成相同集合。靠註解
「與後端保持一致」維護無 CI 攔截，未來新增 PII 欄位只改一邊會靜默 drift。

策略：用 regex 從 sentry.ts 抽出兩個 array 字串字面值，跟 backend 集合比對。
若前端 repo 不在預期路徑（CI 環境變數差異）—— skip 而非 fail，避免綁死 layout。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from utils.sentry_init import _PII_KEY_EXEMPT_SUBSTRINGS, _PII_KEY_SUBSTRINGS

# backend repo root → workspace sibling 'ivy-frontend'
_FRONTEND_SENTRY = (
    Path(__file__).resolve().parents[2] / "ivy-frontend" / "src" / "utils" / "sentry.ts"
)

# 抓 `const NAME = [ ... ]` 內所有單引號 / 雙引號 string literal
_ARRAY_RE = re.compile(
    r"const\s+(?P<name>PII_KEY_SUBSTRINGS|PII_KEY_EXEMPT_SUBSTRINGS)\s*=\s*\[(?P<body>.*?)\]",
    re.DOTALL,
)
_STRING_RE = re.compile(r"""['"]([^'"]+)['"]""")


def _parse_fe_arrays() -> tuple[frozenset[str], frozenset[str]]:
    text = _FRONTEND_SENTRY.read_text(encoding="utf-8")
    found: dict[str, frozenset[str]] = {}
    for m in _ARRAY_RE.finditer(text):
        name = m.group("name")
        body = m.group("body")
        items = frozenset(_STRING_RE.findall(body))
        found[name] = items
    if "PII_KEY_SUBSTRINGS" not in found or "PII_KEY_EXEMPT_SUBSTRINGS" not in found:
        pytest.fail(
            f"無法從 {_FRONTEND_SENTRY} 抽出 PII_KEY_SUBSTRINGS / "
            f"PII_KEY_EXEMPT_SUBSTRINGS array；偵測到的 const："
            f"{sorted(found.keys())}"
        )
    return found["PII_KEY_SUBSTRINGS"], found["PII_KEY_EXEMPT_SUBSTRINGS"]


def _requires_frontend():
    if not _FRONTEND_SENTRY.exists():
        # CI 專屬 gate：dedicated job（ci.yml: pii-denylist-parity）會 sibling checkout
        # 前端並設 PII_PARITY_REQUIRE_FRONTEND=1，此時缺檔改 fail 而非 skip——否則此
        # parity 守衛在前端不在預期 layout 的環境會「靜默 skip」給假信心
        # （CLAUDE.md §8：FE/BE PII denylist 單側 drift = prod 洩漏）。本機與一般
        # CI job（無此 env、無前端 sibling）仍維持優雅 skip，不綁死 layout。
        if os.getenv("PII_PARITY_REQUIRE_FRONTEND"):
            pytest.fail(
                f"PII_PARITY_REQUIRE_FRONTEND 已設，但前端 sentry.ts 不在預期路徑 "
                f"{_FRONTEND_SENTRY}；CI sibling checkout layout 可能壞了，"
                f"請檢查 .github/workflows/ci.yml 的 pii-denylist-parity job。"
            )
        pytest.skip(
            f"前端 sentry.ts 不在預期路徑 {_FRONTEND_SENTRY}（CI checkout layout 不同？）；"
            f"skip parity check。"
        )


class TestPiiDenylistParity:
    def test_denylist_matches(self):
        _requires_frontend()
        fe_deny, _ = _parse_fe_arrays()
        be_deny = frozenset(_PII_KEY_SUBSTRINGS)
        missing_in_fe = be_deny - fe_deny
        extra_in_fe = fe_deny - be_deny
        assert not missing_in_fe and not extra_in_fe, (
            f"FE/BE PII denylist drift：\n"
            f"  backend 有但 frontend 缺：{sorted(missing_in_fe)}\n"
            f"  frontend 有但 backend 缺：{sorted(extra_in_fe)}\n"
            f"修正：同步更新 utils/sentry_init.py 與 "
            f"../ivy-frontend/src/utils/sentry.ts 兩個 array。"
        )

    def test_exempt_list_matches(self):
        _requires_frontend()
        _, fe_exempt = _parse_fe_arrays()
        be_exempt = frozenset(_PII_KEY_EXEMPT_SUBSTRINGS)
        missing_in_fe = be_exempt - fe_exempt
        extra_in_fe = fe_exempt - be_exempt
        assert not missing_in_fe and not extra_in_fe, (
            f"FE/BE PII exempt list drift：\n"
            f"  backend 有但 frontend 缺：{sorted(missing_in_fe)}\n"
            f"  frontend 有但 backend 缺：{sorted(extra_in_fe)}\n"
        )
