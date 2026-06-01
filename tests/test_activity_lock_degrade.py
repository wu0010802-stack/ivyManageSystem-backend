"""
tests/test_activity_lock_degrade.py

A9-1 TDD：驗證 _lock_regs（pos.py）與 _lock_registration（_shared.py）
的降級行為收斂修正：

- CompileError / NotImplementedError → 降級（回無鎖查詢結果，SQLite 相容）
- OperationalError → **上拋**（真 DB 錯誤 / lock timeout，不應靜默降級）
"""

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy.exc import CompileError, OperationalError

# ─────────────────────────────────────────────────────────────
# _lock_regs (api/activity/pos.py)
# ─────────────────────────────────────────────────────────────


class TestLockRegs:
    """測試 api.activity.pos._lock_regs 的降級行為。"""

    def _make_session_and_query(
        self, with_for_update_side_effect, fallback_result=None
    ):
        """建立 mock session；query chain：.filter().with_for_update().all() 拋例外，
        fallback .filter().all() 回 fallback_result。"""
        # 行鎖查詢物件
        locked_query = MagicMock()
        locked_query.all.side_effect = with_for_update_side_effect

        # 無鎖查詢物件
        unlocked_query = MagicMock()
        unlocked_query.all.return_value = fallback_result or []

        # filter 回傳共用的 filter_query；with_for_update 分叉
        filter_query = MagicMock()
        filter_query.with_for_update.return_value = locked_query
        filter_query.all.return_value = fallback_result or []  # 無鎖路徑

        session = MagicMock()
        session.query.return_value.filter.return_value = filter_query
        return session, filter_query

    # ── OperationalError → 上拋 ──
    def test_lock_regs_operational_error_raises(self):
        from api.activity.pos import _lock_regs

        session, _ = self._make_session_and_query(
            with_for_update_side_effect=OperationalError(
                "lock wait timeout", None, None
            )
        )
        with pytest.raises(OperationalError):
            _lock_regs(session, [1, 2, 3])

    # ── CompileError → 降級 ──
    def test_lock_regs_compile_error_degrades(self):
        from api.activity.pos import _lock_regs

        fallback = [MagicMock(id=1), MagicMock(id=2)]
        session, filter_query = self._make_session_and_query(
            with_for_update_side_effect=CompileError("FOR UPDATE not supported"),
            fallback_result=fallback,
        )
        result = _lock_regs(session, [1, 2])
        assert result == fallback

    # ── NotImplementedError → 降級 ──
    def test_lock_regs_not_implemented_error_degrades(self):
        from api.activity.pos import _lock_regs

        fallback = [MagicMock(id=10)]
        session, filter_query = self._make_session_and_query(
            with_for_update_side_effect=NotImplementedError("not supported"),
            fallback_result=fallback,
        )
        result = _lock_regs(session, [10])
        assert result == fallback


# ─────────────────────────────────────────────────────────────
# _lock_registration (api/activity/_shared.py)
# ─────────────────────────────────────────────────────────────


class TestLockRegistration:
    """測試 api.activity._shared._lock_registration 的降級行為。"""

    def _make_session_and_query(
        self, with_for_update_side_effect, fallback_result=None
    ):
        """建立 mock session；query chain：
        .filter().with_for_update().first() 拋例外，
        fallback .filter().first() 回 fallback_result。
        """
        # 行鎖查詢物件
        locked_query = MagicMock()
        locked_query.first.side_effect = with_for_update_side_effect

        # filter 回傳共用的 filter_query；with_for_update 分叉
        filter_query = MagicMock()
        filter_query.with_for_update.return_value = locked_query
        filter_query.first.return_value = fallback_result  # 無鎖路徑

        session = MagicMock()
        session.query.return_value.filter.return_value = filter_query
        return session, filter_query

    # ── OperationalError → 上拋 ──
    def test_lock_registration_operational_error_raises(self):
        from api.activity._shared import _lock_registration

        session, _ = self._make_session_and_query(
            with_for_update_side_effect=OperationalError("connection lost", None, None)
        )
        with pytest.raises(OperationalError):
            _lock_registration(session, 42)

    # ── CompileError → 降級 ──
    def test_lock_registration_compile_error_degrades(self):
        from api.activity._shared import _lock_registration

        fallback = MagicMock(id=42)
        session, filter_query = self._make_session_and_query(
            with_for_update_side_effect=CompileError("FOR UPDATE not supported"),
            fallback_result=fallback,
        )
        result = _lock_registration(session, 42)
        assert result == fallback

    # ── NotImplementedError → 降級 ──
    def test_lock_registration_not_implemented_error_degrades(self):
        from api.activity._shared import _lock_registration

        fallback = MagicMock(id=99)
        session, filter_query = self._make_session_and_query(
            with_for_update_side_effect=NotImplementedError("not implemented"),
            fallback_result=fallback,
        )
        result = _lock_registration(session, 99)
        assert result == fallback
