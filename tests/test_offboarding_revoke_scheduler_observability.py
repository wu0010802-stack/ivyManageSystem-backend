"""離職撤權排程器 tick 失敗必須被 scheduler_iteration 記成失敗（SEC-007 / 資安掃描 2026-06-15 P2）。

原 run_offboarding_revoke_scheduler 把 try/except Exception 放在 `with scheduler_iteration(...)`
*內部*，使 run_offboarding_revoke_due_once() 拋例外時被內層 except 先吞掉，scheduler_iteration
永遠走成功路徑（consecutive_failures 歸零、heartbeat success=True、不告警）。結果：一筆 poison
record 即可使整批撤權靜默零執行，離職員工帳號/refresh token 持續未撤而監控全綠。

修法：把 try/except 移到 `with scheduler_iteration` *外*（仿 finance_reconciliation_scheduler /
data_quality_scheduler），使失敗先穿過 observability 記錄再被 swallow 保住 loop。
"""

import asyncio
from contextlib import contextmanager

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


class _FakeSavepoint:
    """模擬 session.begin_nested()：__exit__ 不吞例外（讓外層 try 接），
    記錄 rollback 是否被觸發。"""

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._session.savepoint_rollbacks += 1
        return False  # 不吞例外


class _FakeQuery:
    def __init__(self, records):
        self._records = records

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._records


class _FakeSession:
    def __init__(self, records):
        self._records = records
        self.savepoint_rollbacks = 0

    def begin_nested(self):
        return _FakeSavepoint(self)

    def query(self, *a, **k):
        return _FakeQuery(self._records)


def test_poison_record_isolated_others_still_revoked(monkeypatch):
    """C33：每筆離職記錄獨立故障隔離——一筆 poison record 不可拖垮整批。

    第 2 筆 revoke_run 拋例外時，第 1、3 筆仍應成功撤權、第 2 筆計入 failed，
    且只 rollback 該筆 savepoint（不 rollback 整批）。
    """
    from services.offboarding import offboarding_revoke_scheduler as mod

    rec1, rec2, rec3 = object(), object(), object()
    fake_session = _FakeSession([rec1, rec2, rec3])

    @contextmanager
    def _fake_session_scope():
        yield fake_session

    seen = []

    def _fake_revoke_run(session, record):
        seen.append(record)
        if record is rec2:
            raise RuntimeError("poison offboarding record")
        return {"status": "completed"}

    # run_offboarding_revoke_due_once 內以 local import 取 session_scope / revoke_run，
    # patch 來源模組屬性即可在呼叫時生效。
    monkeypatch.setattr("models.base.session_scope", _fake_session_scope)
    monkeypatch.setattr("services.offboarding.steps.revoke_user.run", _fake_revoke_run)

    result = mod.run_offboarding_revoke_due_once()

    # 三筆都被嘗試（poison 沒中止其餘）
    assert seen == [rec1, rec2, rec3]
    # 第 1、3 筆成功，第 2 筆失敗
    assert result["revoked"] == 2
    assert result["failed"] == 1
    # 只 rollback poison 那一筆的 savepoint
    assert fake_session.savepoint_rollbacks == 1
