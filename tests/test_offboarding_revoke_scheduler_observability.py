"""離職撤權排程器 tick 失敗必須被 scheduler_iteration 記成失敗（SEC-007 / 資安掃描 2026-06-15 P2）。

原 run_offboarding_revoke_scheduler 把 try/except Exception 放在 `with scheduler_iteration(...)`
*內部*，使 run_offboarding_revoke_due_once() 拋例外時被內層 except 先吞掉，scheduler_iteration
永遠走成功路徑（consecutive_failures 歸零、heartbeat success=True、不告警）。結果：一筆 poison
record 即可使整批撤權靜默零執行，離職員工帳號/refresh token 持續未撤而監控全綠。

修法：把 try/except 移到 `with scheduler_iteration` *外*（仿 finance_reconciliation_scheduler /
data_quality_scheduler），使失敗先穿過 observability 記錄再被 swallow 保住 loop。
"""

import asyncio

from utils import scheduler_observability


def test_tick_failure_is_recorded_as_failure(test_db_session, monkeypatch):
    from services.offboarding import offboarding_revoke_scheduler as mod

    scheduler_observability.reset_for_tests()

    stop_event = asyncio.Event()

    def _boom():
        # 先讓 loop 在這次 iteration 後退出，再拋例外模擬 poison record
        stop_event.set()
        raise RuntimeError("poison offboarding record")

    monkeypatch.setattr(mod, "run_offboarding_revoke_due_once", _boom)

    asyncio.run(mod.run_offboarding_revoke_scheduler(stop_event))

    stats = scheduler_observability.get_metrics_snapshot().get("offboarding_revoke")
    assert stats is not None, "scheduler_iteration 應已建立該 scheduler 的 metrics"
    assert (
        stats.consecutive_failures >= 1
    ), "tick 失敗必須被 scheduler_iteration 記成失敗（不可被內層 except 靜默吞成功）"
    assert stats.last_success_at is None, "失敗的 tick 不可被記成成功"


def test_tick_success_is_recorded_as_success(test_db_session, monkeypatch):
    """對照組：正常 tick 應記成功（確保修補沒把成功路徑也弄壞）。"""
    from services.offboarding import offboarding_revoke_scheduler as mod

    scheduler_observability.reset_for_tests()

    stop_event = asyncio.Event()

    def _ok():
        stop_event.set()
        return {"revoked": 0}

    monkeypatch.setattr(mod, "run_offboarding_revoke_due_once", _ok)

    asyncio.run(mod.run_offboarding_revoke_scheduler(stop_event))

    stats = scheduler_observability.get_metrics_snapshot().get("offboarding_revoke")
    assert stats is not None
    assert stats.consecutive_failures == 0
    assert stats.last_success_at is not None
