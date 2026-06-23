"""is_lock_contention_error 判別與報名熱路徑 lock_timeout helper 的回歸測試。

背景：上線穩定度稽核（2026-06-23）——報名尖峰搶同一熱門課的列鎖爭用，
應快速失敗回 409「請稍候再試」而非佔住連線拖垮 20 條連線池 / 併到通用 500。

純邏輯測試，不碰 DB（helper 在 SQLite 下為 no-op，整合由既有 register/update 測試覆蓋）。
"""

from utils.errors import is_lock_contention_error


class _Orig:
    def __init__(self, pgcode):
        self.pgcode = pgcode


class _ExcWithOrig(Exception):
    def __init__(self, pgcode):
        super().__init__("db error")
        self.orig = _Orig(pgcode)


def test_lock_timeout_55P03_is_contention():
    assert is_lock_contention_error(_ExcWithOrig("55P03")) is True


def test_deadlock_40P01_is_contention():
    assert is_lock_contention_error(_ExcWithOrig("40P01")) is True


def test_unique_violation_23505_not_contention():
    # 並發雙寫由 IntegrityError 既有分流處理，不該被當鎖爭用吞掉
    assert is_lock_contention_error(_ExcWithOrig("23505")) is False


def test_statement_timeout_57014_not_contention():
    # statement_timeout 代表非鎖的長查詢異常，仍視為 500 / 上報
    assert is_lock_contention_error(_ExcWithOrig("57014")) is False


def test_plain_exception_not_contention():
    assert is_lock_contention_error(ValueError("x")) is False


def test_none_pgcode_not_contention():
    assert is_lock_contention_error(_ExcWithOrig(None)) is False
