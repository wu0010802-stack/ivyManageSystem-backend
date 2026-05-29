"""WSConnectionLimiter unit tests。"""

from unittest.mock import MagicMock

import pytest

from utils.ws_connection_limiter import (
    WS_MAX_CONN_PER_USER,
    WSConnectionLimitExceeded,
    assert_under_limit,
    count,
    register,
    reset_for_tests,
    unregister,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


def test_assert_under_limit_passes_below_max():
    user_id = 42
    for _ in range(WS_MAX_CONN_PER_USER - 1):
        register(user_id, MagicMock())
    assert_under_limit(user_id)


def test_assert_under_limit_raises_at_max():
    user_id = 42
    for _ in range(WS_MAX_CONN_PER_USER):
        register(user_id, MagicMock())
    with pytest.raises(WSConnectionLimitExceeded):
        assert_under_limit(user_id)


def test_unregister_decrements_count():
    user_id = 42
    ws = MagicMock()
    register(user_id, ws)
    assert count(user_id) == 1
    unregister(ws)
    assert count(user_id) == 0


def test_unregister_idempotent():
    """double-call unregister 不 raise。"""
    user_id = 42
    ws = MagicMock()
    register(user_id, ws)
    unregister(ws)
    unregister(ws)
    assert count(user_id) == 0


def test_unregister_unknown_ws_noop():
    """unregister 從未 register 的 ws 不 raise。"""
    unregister(MagicMock())


def test_isolation_between_users():
    """user A 達上限不影響 user B。"""
    for _ in range(WS_MAX_CONN_PER_USER):
        register(1, MagicMock())
    assert_under_limit(2)
    register(2, MagicMock())
    assert count(2) == 1
