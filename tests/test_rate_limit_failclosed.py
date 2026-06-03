"""RA-MED-2：auth scope 限流在 DB 失敗時不應 fail-open 回 0。

漏洞：count_recent_attempts DB 失敗時一律回 0（fail-open）→ 攻擊者只要讓 DB
短暫失聯即可繞過 login / change-password / reset / bind 的所有限流。

修法：count_recent_attempts 加 fail_closed 參數。auth scope 的 caller 傳
fail_closed=True：DB 失敗時改用 module 層 in-process backstop（per-worker
滑動視窗，降級但非歸零），而非回 0。非 auth scope 維持 fail-open（行為不變）。
"""

from unittest.mock import patch

import pytest

from utils import rate_limit_db


@pytest.fixture(autouse=True)
def _clear_inproc():
    """每個 test 前後清 in-process backstop，避免互相污染。"""
    rate_limit_db._inproc.clear()
    yield
    rate_limit_db._inproc.clear()


def test_auth_scope_fail_closed_on_db_error():
    """fail_closed=True 且 DB 失敗時應回 backstop 計數（≥ 之前累積），非 0。"""
    rate_limit_db.record_attempt_inproc("login_ip", "1.2.3.4", window_seconds=900)
    with patch.object(
        rate_limit_db, "_count_from_db", side_effect=Exception("db down")
    ):
        n = rate_limit_db.count_recent_attempts(
            "login_ip", "1.2.3.4", within_seconds=900, fail_closed=True
        )
    assert n >= 1  # 修補前為 0


def test_non_auth_scope_still_fail_open():
    """未傳 fail_closed（預設 fail-open）時 DB 失敗仍回 0（行為不變）。"""
    with patch.object(
        rate_limit_db, "_count_from_db", side_effect=Exception("db down")
    ):
        n = rate_limit_db.count_recent_attempts(
            "public_register", "1.2.3.4", within_seconds=60
        )
    assert n == 0


def test_fail_closed_returns_zero_when_no_inproc_record():
    """fail_closed=True 但 backstop 無紀錄時回 0（無從降級，不誤鎖無辜）。"""
    with patch.object(
        rate_limit_db, "_count_from_db", side_effect=Exception("db down")
    ):
        n = rate_limit_db.count_recent_attempts(
            "login_ip", "never-seen", within_seconds=900, fail_closed=True
        )
    assert n == 0


def test_record_attempt_populates_inproc_for_auth_scope():
    """record_attempt 對 auth scope 應同時寫 in-process backstop（DB 死時才有資料可數）。

    用 _BrokenEngine 讓 DB 寫入失敗，仍應落 backstop。
    """

    class _BrokenEngine:
        def begin(self):
            raise RuntimeError("DB down")

        class _Dialect:
            name = "sqlite"

        dialect = _Dialect()

    rate_limit_db.record_attempt(
        "login_ip", "9.9.9.9", window_seconds=900, engine=_BrokenEngine()
    )
    n = rate_limit_db._count_recent_inproc("login_ip", "9.9.9.9", within_seconds=900)
    assert n >= 1


def test_record_attempt_skips_inproc_for_non_auth_scope():
    """非 auth scope 不寫 backstop（避免無謂記憶體成長）。"""

    class _BrokenEngine:
        def begin(self):
            raise RuntimeError("DB down")

        class _Dialect:
            name = "sqlite"

        dialect = _Dialect()

    rate_limit_db.record_attempt(
        "public_register", "9.9.9.9", window_seconds=60, engine=_BrokenEngine()
    )
    n = rate_limit_db._count_recent_inproc(
        "public_register", "9.9.9.9", within_seconds=60
    )
    assert n == 0


def test_inproc_sliding_window_excludes_old():
    """backstop 滑動視窗應排除超出 within_seconds 的舊紀錄。"""
    import time

    # 直接塞一筆「很久以前」的時間戳
    bk = rate_limit_db._inproc_key("login_ip", "8.8.8.8")
    rate_limit_db._inproc[bk] = [time.monotonic() - 10000]
    n = rate_limit_db._count_recent_inproc("login_ip", "8.8.8.8", within_seconds=900)
    assert n == 0
