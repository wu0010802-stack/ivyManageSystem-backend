"""驗證 alembic migration 的 backfill 純函式 round-trip 正確性。"""

import importlib.util
from pathlib import Path

import pytest

# 動態 load migration 模組（檔名含 timestamp，import path 不穩）
# 用 revision id (permtxt01) 抓而非 filename slug：robust to filename drift + 絕對路徑（不依賴 cwd）
_VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_paths = sorted(_VERSIONS_DIR.glob("*permtxt01*.py"))
assert (
    len(_paths) == 1
), f"預期找到 1 個 permtxt01 migration，實際 {len(_paths)}: {_paths}"
_MIGRATION_PATH = _paths[0]
_spec = importlib.util.spec_from_file_location("perm_migration", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

bigint_to_names = _mod._bigint_to_names
names_to_bigint = _mod._names_to_bigint


def test_null_roundtrip():
    assert bigint_to_names(None) is None
    assert names_to_bigint(None) is None


def test_minus_one_to_wildcard():
    assert bigint_to_names(-1) == ["*"]
    assert names_to_bigint(["*"]) == -1


def test_all_bits_except_zero_is_not_wildcard():
    """確認 -1 → ['*'] 是嚴格 equality（不是 sign check）。

    建構「bit 1-62 全 set，但 bit 0 不 set」的合法 mask；不該被當 wildcard。
    """
    # 全部 _LEGACY_BITS 加總（known_mask），再扣掉 bit 0
    known_mask = 0
    for bit in _mod._LEGACY_BITS.values():
        known_mask |= bit
    val = known_mask & ~(1 << 0)  # 拿掉 DASHBOARD
    names = bigint_to_names(val)
    assert names != ["*"]
    assert "DASHBOARD" not in names
    assert "VENDOR_PAYMENT_WRITE" in names
    assert len(names) == 62  # 63 - 1


def test_zero_to_empty():
    assert bigint_to_names(0) == []
    assert names_to_bigint([]) == 0


def test_single_bit_roundtrip():
    # EMPLOYEES_READ = 1 << 8
    val = 1 << 8
    names = bigint_to_names(val)
    assert names == ["EMPLOYEES_READ"]
    assert names_to_bigint(names) == val


def test_combined_bits_roundtrip():
    # EMPLOYEES_READ (1<<8) | SALARY_WRITE (1<<23)
    val = (1 << 8) | (1 << 23)
    names = bigint_to_names(val)
    assert set(names) == {"EMPLOYEES_READ", "SALARY_WRITE"}
    assert names_to_bigint(names) == val


def test_high_bit_roundtrip():
    # VENDOR_PAYMENT_WRITE = 1 << 62（最高位元）
    val = 1 << 62
    names = bigint_to_names(val)
    assert names == ["VENDOR_PAYMENT_WRITE"]
    assert names_to_bigint(names) == val


def test_downgrade_aborts_on_unknown_name():
    with pytest.raises(RuntimeError, match="LEGACY_BITS 不認得"):
        names_to_bigint(["TOTALLY_NEW_PERMISSION"])


def test_upgrade_aborts_on_unknown_bit():
    """對稱於 downgrade：bigint 含 LEGACY_BITS 外的 bit 應 raise。"""
    with pytest.raises(RuntimeError, match="LEGACY_BITS 範圍外的 bit"):
        bigint_to_names(1 << 63)  # bit 63 不在 _LEGACY_BITS 內


def test_legacy_bits_has_63_entries():
    """快照保護：本表已凍結，數量不應變動。"""
    assert len(_mod._LEGACY_BITS) == 63


def test_legacy_bits_max_is_62():
    assert max(_mod._LEGACY_BITS.values()) == (1 << 62)
