"""tests/test_data_quality_scheduler.py — Ch2 scheduler 模組。"""

import asyncio
from contextlib import contextmanager
from unittest.mock import patch


def test_scheduler_enabled_returns_false_by_default(monkeypatch):
    """DATA_QUALITY_ENABLED 環境變數未設時應 False。"""
    monkeypatch.delenv("DATA_QUALITY_ENABLED", raising=False)
    from config import reset_for_tests

    reset_for_tests()

    from services.data_quality_scheduler import scheduler_enabled

    assert scheduler_enabled() is False


def test_scheduler_enabled_returns_true_when_env_set(monkeypatch):
    """DATA_QUALITY_ENABLED=true → True。"""
    monkeypatch.setenv("DATA_QUALITY_ENABLED", "true")
    from config import reset_for_tests

    reset_for_tests()

    from services.data_quality_scheduler import scheduler_enabled

    assert scheduler_enabled() is True


def test_run_data_quality_once_orchestrates_engine_and_dispatch(test_db_session):
    """run_data_quality_once：跑 engine.run_all_rules → dispatch.emit → flush_line_digest。
    回傳 dict 含 detected/new_open/ran_at。
    """
    from services.data_quality._base import Violation
    from services.data_quality_scheduler import run_data_quality_once

    fake_v = Violation(
        rule_code="x", severity="P0", entity_type="e", entity_id="1", summary="s"
    )

    with (
        patch(
            "services.data_quality_scheduler.run_all_rules", return_value=[fake_v]
        ) as m_run,
        patch("services.data_quality_scheduler.emit", return_value=True) as m_emit,
        patch("services.data_quality_scheduler.flush_line_digest") as m_flush,
    ):
        result = run_data_quality_once()

    assert m_run.called
    assert m_emit.called
    assert m_flush.called
    assert result["detected"] == 1
    assert result["new_open"] == 1
    assert "ran_at" in result


def test_run_data_quality_once_returns_zero_on_no_violations(test_db_session):
    """空 violation list → detected=0, new_open=0。"""
    from services.data_quality_scheduler import run_data_quality_once

    with (
        patch("services.data_quality_scheduler.run_all_rules", return_value=[]),
        patch("services.data_quality_scheduler.flush_line_digest"),
    ):
        result = run_data_quality_once()

    assert result["detected"] == 0
    assert result["new_open"] == 0


def test_failed_tick_does_not_mark_day_done_so_it_retries(monkeypatch):
    """C35：run_data_quality_once 拋例外時 last_run_date 不可被標今天，否則當日不重試。

    驅動 scheduler loop 兩個 tick（同一天）：第一次 tick 失敗，第二次 tick
    應仍會嘗試執行（證明失敗那次沒把當日標成已跑完）。
    """
    from services import data_quality_scheduler as mod

    monkeypatch.setenv("DATA_QUALITY_ENABLED", "true")
    from config import reset_for_tests

    reset_for_tests()

    @contextmanager
    def _fake_lock(*args, **kwargs):
        yield True

    class _FakeSession:
        def close(self):
            pass

    calls = {"n": 0}
    stop_event = asyncio.Event()

    def _run_once():
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次失敗（poison）；不在這裡 stop，讓後續 tick 有機會重試
            raise RuntimeError("boom")
        # 第二次成功 → 停止 loop
        stop_event.set()
        return {"detected": 0}

    # 用真實 should_run_data_quality（會依 last_run_date 去重），這樣 bug 存在時
    # 失敗那次把 last_run_date 標成今天 → 後續 tick 被 should_run 擋掉永不重試。
    # 安全閥：tick 數超過上限即強制停，避免 bug 未修時 loop 空轉到 timeout。
    ticks = {"n": 0}
    real_should_run = mod.should_run_data_quality

    def _counting_should_run(now, th, tm, last_run_date=None):
        ticks["n"] += 1
        if ticks["n"] > 6:
            stop_event.set()
            return False
        return real_should_run(now, th, tm, last_run_date)

    # 目標時刻 00:00 → 當日只要 last_run_date != today 即觸發
    monkeypatch.setattr(mod, "_target_hm", lambda: (0, 0))
    monkeypatch.setattr(mod, "should_run_data_quality", _counting_should_run)
    monkeypatch.setattr(mod, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(mod, "run_data_quality_once", _run_once)
    monkeypatch.setattr("utils.advisory_lock.try_scheduler_lock", _fake_lock)

    from config import get_settings

    object.__setattr__(get_settings().scheduler, "data_quality_check_interval", 0.001)

    async def _drive():
        await asyncio.wait_for(mod.run_data_quality_scheduler(stop_event), timeout=5.0)

    asyncio.run(_drive())

    assert (
        calls["n"] >= 2
    ), "失敗的 tick 不可把當日標成已跑完；後續 tick 應仍嘗試執行（重試）"
