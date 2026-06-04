"""FE/BE Permission 全表平行校驗。

目的：後端 ``utils/permissions.Permission`` enum 與前端
``../ivy-frontend/src/constants/permissions.ts`` 的 ``PERMISSION_NAMES`` 維護成相同集合。
此前靠註解「兩端同步」維護、無 CI 攔截，新增權限只改一邊會靜默 drift——例：2026-05-29
後端加 ``DATA_QUALITY_READ`` / ``DATA_QUALITY_WRITE``，前端漏同步造成 68 vs 70，
管理端權限設定 UI 會漏列該權限、admin 無法在前端把它指派給角色（後端守衛仍在，
方向 fail-closed 不洩漏，但功能漏接）。本 gate 補上 PII denylist parity
（``test_pii_denylist_parity.py``）與 scope-aware parity 之外缺的「全表」缺口。

策略：用 regex 從 permissions.ts 抽出 ``PERMISSION_NAMES`` 的值，跟 backend enum value
雙向比對。前端不在預期路徑時，預設優雅 skip（不綁死 layout）；CI dedicated job
（ci.yml: ``permission-parity``）設 ``PERMISSION_PARITY_REQUIRE_FRONTEND=1`` 時缺檔改
fail 而非靜默 skip（否則此 gate 在前端不在預期 layout 的環境會給假信心）。
``PERMISSION_PARITY_FRONTEND`` 可覆寫前端 permissions.ts 路徑（CI 指定 / 本機驗證用）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from utils.permissions import Permission

# backend repo root → workspace sibling 'ivy-frontend'；可用 env 覆寫
_FRONTEND_PERMS = (
    Path(os.environ["PERMISSION_PARITY_FRONTEND"])
    if os.getenv("PERMISSION_PARITY_FRONTEND")
    else Path(__file__).resolve().parents[2]
    / "ivy-frontend"
    / "src"
    / "constants"
    / "permissions.ts"
)

# 抓 `export const PERMISSION_NAMES = { ... } as const`（物件為扁平、無巢狀 `}`）
_OBJECT_RE = re.compile(
    r"const\s+PERMISSION_NAMES\s*=\s*\{(?P<body>.*?)\}\s*as\s+const",
    re.DOTALL,
)
# 物件內每筆 `KEY: 'VALUE'`；限定 KEY 為大寫識別字 + 引號值，避開註解文字
_ENTRY_RE = re.compile(r"""(?P<key>[A-Z][A-Z0-9_]*)\s*:\s*['"](?P<val>[^'"]+)['"]""")


def _parse_fe_permission_names() -> frozenset[str]:
    text = _FRONTEND_PERMS.read_text(encoding="utf-8")
    m = _OBJECT_RE.search(text)
    if not m:
        pytest.fail(
            f"無法從 {_FRONTEND_PERMS} 抽出 PERMISSION_NAMES 物件（格式變動？）。"
        )
    entries = _ENTRY_RE.findall(m.group("body"))
    if not entries:
        pytest.fail(
            f"{_FRONTEND_PERMS} 的 PERMISSION_NAMES 抽不到任何 KEY: 'VALUE' 項目。"
        )
    # 前端慣例 key == value；順帶守衛避免單側打錯
    mismatched = [(k, v) for k, v in entries if k != v]
    assert not mismatched, f"前端 PERMISSION_NAMES key != value：{mismatched}"
    return frozenset(v for _, v in entries)


def _requires_frontend():
    if not _FRONTEND_PERMS.exists():
        # CI 專屬 gate：dedicated job 會 sibling checkout 前端並設
        # PERMISSION_PARITY_REQUIRE_FRONTEND=1，此時缺檔改 fail 而非 skip——否則此
        # parity 守衛在前端不在預期 layout 的環境會「靜默 skip」給假信心。
        # 本機與一般 CI job（無此 env、無前端 sibling）仍維持優雅 skip。
        if os.getenv("PERMISSION_PARITY_REQUIRE_FRONTEND"):
            pytest.fail(
                f"PERMISSION_PARITY_REQUIRE_FRONTEND 已設，但前端 permissions.ts 不在預期路徑 "
                f"{_FRONTEND_PERMS}；CI sibling checkout layout 可能壞了，"
                f"請檢查 .github/workflows/ci.yml 的 permission-parity job。"
            )
        pytest.skip(
            f"前端 permissions.ts 不在預期路徑 {_FRONTEND_PERMS}（CI checkout layout 不同？）；"
            f"skip parity check。"
        )


class TestPermissionParity:
    def test_permission_names_match(self):
        _requires_frontend()
        fe = _parse_fe_permission_names()
        be = frozenset(p.value for p in Permission)
        missing_in_fe = be - fe
        extra_in_fe = fe - be
        assert not missing_in_fe and not extra_in_fe, (
            f"FE/BE Permission 全表 drift：\n"
            f"  backend 有但 frontend 缺：{sorted(missing_in_fe)}\n"
            f"  frontend 有但 backend 缺：{sorted(extra_in_fe)}\n"
            f"修正：同步更新 utils/permissions.py 的 Permission enum 與 "
            f"../ivy-frontend/src/constants/permissions.ts 的 PERMISSION_NAMES。"
        )
