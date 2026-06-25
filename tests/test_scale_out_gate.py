"""Scale-out 協調 gate（設計審查 2026-06-25 LONG-1）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from startup.scale_out_gate import ScaleOutMisconfigError, check_scale_out_backends


def _settings(mode, cache="memory", broadcast="memory", rate_limit="memory"):
    return SimpleNamespace(
        core=SimpleNamespace(deployment_mode=mode),
        cache=SimpleNamespace(backend=cache, effective_broadcast_backend=broadcast),
        network=SimpleNamespace(rate_limit_backend=rate_limit),
    )


def test_single_mode_memory_is_allowed():
    """single（預設/當前 prod）+ 全 memory → 不擋（零行為改變）。"""
    check_scale_out_backends(_settings("single"))  # 不應 raise


def test_multi_mode_all_memory_rejected():
    """multi + 全 memory → fail-fast，訊息含三個 backend。"""
    with pytest.raises(ScaleOutMisconfigError) as ei:
        check_scale_out_backends(_settings("multi"))
    msg = str(ei.value)
    assert "CACHE_BACKEND" in msg
    assert "BROADCAST_BACKEND" in msg
    assert "RATE_LIMIT_BACKEND" in msg


def test_multi_mode_all_shared_ok():
    """multi + 全部共享後端 → 通過。"""
    check_scale_out_backends(
        _settings("multi", cache="redis", broadcast="redis", rate_limit="postgres")
    )


def test_multi_mode_partial_memory_rejected():
    """multi + 只剩 rate-limit 是 memory → 仍 fail-fast，且只報該項。"""
    with pytest.raises(ScaleOutMisconfigError) as ei:
        check_scale_out_backends(
            _settings("multi", cache="redis", broadcast="redis", rate_limit="memory")
        )
    msg = str(ei.value)
    assert "RATE_LIMIT_BACKEND" in msg
    assert "CACHE_BACKEND" not in msg
    assert "BROADCAST_BACKEND" not in msg


def test_rate_limit_backend_case_insensitive():
    """RATE_LIMIT_BACKEND 大小寫不敏感（'Memory' 仍視為 memory）。"""
    with pytest.raises(ScaleOutMisconfigError):
        check_scale_out_backends(
            _settings("multi", cache="redis", broadcast="redis", rate_limit="Memory")
        )


def test_deployment_mode_reads_env(monkeypatch):
    """CoreSettings.deployment_mode 由 DEPLOYMENT_MODE env 驅動，預設 single。"""
    from config.core import CoreSettings

    assert CoreSettings().deployment_mode == "single"
    monkeypatch.setenv("DEPLOYMENT_MODE", "multi")
    assert CoreSettings().deployment_mode == "multi"
