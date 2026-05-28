"""OpsSettings：MAINTENANCE_MODE / READ_ONLY_MODE / MAINTENANCE_MESSAGE env 綁定。

env-only kill switch 設計（避開事故時 DB 可能掛）。實作見 utils/kill_switch.py。
"""

import pytest


def test_maintenance_mode_default_off(monkeypatch):
    for var in ("MAINTENANCE_MODE", "READ_ONLY_MODE", "MAINTENANCE_MESSAGE"):
        monkeypatch.delenv(var, raising=False)
    from config import get_settings, reset_for_tests

    reset_for_tests()
    s = get_settings()
    assert s.ops.maintenance_mode is False
    assert s.ops.read_only_mode is False
    assert s.ops.maintenance_message  # has non-empty default


def test_maintenance_mode_env_on(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_MODE", "1")
    monkeypatch.setenv("MAINTENANCE_MESSAGE", "升級中")
    from config import get_settings, reset_for_tests

    reset_for_tests()
    s = get_settings()
    assert s.ops.maintenance_mode is True
    assert s.ops.maintenance_message == "升級中"


def test_read_only_mode_env_on(monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "true")
    from config import get_settings, reset_for_tests

    reset_for_tests()
    s = get_settings()
    assert s.ops.read_only_mode is True
    assert s.ops.maintenance_mode is False  # 兩 flag 獨立


def test_maintenance_mode_accepts_various_bool_strings(monkeypatch):
    """確認 pydantic v2 bool 解析涵蓋常見字串值。"""
    for truthy in ("1", "true", "True", "yes"):
        monkeypatch.setenv("MAINTENANCE_MODE", truthy)
        from config import get_settings, reset_for_tests

        reset_for_tests()
        assert get_settings().ops.maintenance_mode is True, truthy

    for falsy in ("0", "false", "False", "no"):
        monkeypatch.setenv("MAINTENANCE_MODE", falsy)
        from config import get_settings, reset_for_tests

        reset_for_tests()
        assert get_settings().ops.maintenance_mode is False, falsy
