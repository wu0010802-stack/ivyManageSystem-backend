"""崩潰防護 P0：migration 靜態 gate 的單元測試 + 對真實 migration 鏈的 smoke。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.validate_migrations import (  # noqa: E402
    check_single_head,
    check_upgrade_downgrade_present,
    main,
)


def test_single_head_passes():
    assert check_single_head(["abc123"]) == []


def test_multi_head_flagged():
    problems = check_single_head(["aaa", "bbb"])
    assert len(problems) == 1
    assert "多個 alembic head" in problems[0]


def test_zero_head_flagged():
    assert check_single_head([]) != []


def test_missing_downgrade_flagged():
    class _Mod:
        def upgrade(self):  # noqa: D401
            pass

        # 無 downgrade

    class _Rev:
        revision = "deadbeef"
        module = _Mod()

    problems = check_upgrade_downgrade_present([_Rev()])
    assert any("缺 downgrade" in p for p in problems)


def test_real_migration_chain_passes_gate():
    """現行 migration 鏈須通過 gate（單一 head、全部可載入、up/down 齊全）。"""
    assert main() == 0
