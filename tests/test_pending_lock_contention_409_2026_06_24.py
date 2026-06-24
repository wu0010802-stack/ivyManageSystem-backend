"""崩潰防護 P1 wiring：才藝待審後台寫入端點須把鎖爭用/死鎖映射成 409 而非 500。

這些端點取鎖序（reg row → identity advisory）與公開 public_update 相反，同一筆
pending 報名併發改寫時會 deadlock（40P01）。它們原本只有 `except Exception →
raise_safe_500` → 500 + Sentry 噪音。本測試（source-inspection，PG row-lock 行為
SQLite 無法重現，依本 repo 鎖序測試慣例）斷言每個端點都新增了 OperationalError 分支
並走 raise_lock_contention_or_500（→ 409）。helper 本身的 409/500 映射由
test_lock_contention_helper_2026_06_24 以行為測試覆蓋。
"""

import inspect

import pytest

from api.activity.registrations import update_registration_basic
from api.activity.registrations_pending import (
    force_accept_registration,
    match_registration,
    rematch_registration,
    restore_registration,
)

_TARGETS = [
    match_registration,
    rematch_registration,
    force_accept_registration,
    restore_registration,
    update_registration_basic,
]


@pytest.mark.parametrize("fn", _TARGETS, ids=[f.__name__ for f in _TARGETS])
def test_backend_pending_endpoint_maps_lock_contention_to_409(fn):
    src = inspect.getsource(fn)
    assert (
        "OperationalError" in src
    ), f"{fn.__name__} 未捕捉 OperationalError → deadlock 會落入通用 except 變 500"
    assert (
        "raise_lock_contention_or_500" in src
    ), f"{fn.__name__} 未用 raise_lock_contention_or_500，死鎖仍噴 500 + Sentry 噪音"
    # OperationalError 分支必須在通用 except Exception 之前才會被先攔
    if "except Exception" in src:
        assert src.index("OperationalError") < src.index(
            "except Exception"
        ), f"{fn.__name__} 的 OperationalError 分支必須在 except Exception 之前"
