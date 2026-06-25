"""drain 共用單一 log session（P2 效能修補）。

舊版 _drain_after_commit 對每個 PendingEvent 呼叫 _fan_out，而每個 _fan_out 各開
一個新 session（get_session_factory()()）。scope='all' 廣播對每位 active 家長
enqueue 一個 event → 數百個新 session + 連線 churn。

改為整個 drain 共用一個 log session（透過 contextvar 傳遞，維持 _fan_out(evt)
單參數簽名以相容既有 hooks/fan_out 測試），_fan_out 在 drain 內重用該 session、
不再逐筆開新連線。standalone 直呼 _fan_out 仍自開自關（由 test_dispatch_fan_out.py 守護）。
"""

from unittest.mock import patch

from services.notification import dispatch


def _ctx():
    return {
        "reviewer_name": "X",
        "leave_type": "事假",
        "start": "2026-06-01",
        "end": "2026-06-02",
        "leave_id": 1,
    }


def test_drain_shares_single_session_across_events(test_db_session):
    seen = []

    def fake_fan_out(evt):
        # _fan_out 在 drain 內應從 contextvar 取得共用 session
        seen.append(dispatch._drain_session_var.get())

    with patch.object(dispatch, "_fan_out", side_effect=fake_fan_out):
        for i in range(5):
            dispatch.enqueue(
                test_db_session,
                event_type="leave.approved",
                recipient_user_id=i,
                context=_ctx(),
            )
        test_db_session.commit()

    assert len(seen) == 5, f"5 個 event 都應 fan-out，實得 {len(seen)}"
    assert all(s is not None for s in seen), "drain 內每個 event 都應拿到共用 session"
    assert (
        len({id(s) for s in seen}) == 1
    ), "整個 drain 應共用同一個 session（非逐筆開新）"


def test_fan_out_outside_drain_uses_own_session(test_db_session):
    """standalone（非 drain）呼叫時 contextvar 為 None，_fan_out 自開 session。"""
    assert dispatch._drain_session_var.get() is None
