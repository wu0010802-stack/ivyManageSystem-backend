"""加班批次審核後薪資重算去重（P2 效能修補）。

批次審核 commit 後，舊版對 changes 逐筆呼叫 process_salary_calculation；同一員工
同月有多筆 OT 一起審（很常見：一位老師整月加班一次過審）時，同 (employee_id,
year, month) 的完整單人薪資計算被重複跑 N 次。修補後同 (emp, year, month) 只重算
一次。

端點整合行為由既有 tests/test_overtimes*.py / test_batch_approve_response_model.py
守護；本檔針對抽出的 _recalc_salaries_after_overtime_batch 去重邏輯做單元測試。
"""

import types
from datetime import date
from unittest.mock import MagicMock

from api.overtimes import _recalc_salaries_after_overtime_batch


def _change(ot_id, emp_id, d, was_approved=False):
    """組裝 batch_approve 的 changes 元素：(ot_id, ot, was_approved, is_reject, log_id)。"""
    ot = types.SimpleNamespace(employee_id=emp_id, overtime_date=d)
    return (ot_id, ot, was_approved, False, None)


def test_same_employee_same_month_recalc_once():
    """同員工同月 3 筆 OT 一起核准 → 只重算一次。"""
    engine = MagicMock()
    changes = [
        _change(1, 10, date(2026, 3, 10)),
        _change(2, 10, date(2026, 3, 11)),
        _change(3, 10, date(2026, 3, 12)),
    ]
    _recalc_salaries_after_overtime_batch(
        MagicMock(), changes, approved=True, salary_engine=engine
    )
    assert engine.process_salary_calculation.call_count == 1
    engine.process_salary_calculation.assert_called_once_with(10, 2026, 3)


def test_distinct_employee_month_keys_each_recalc_once():
    """跨員工 + 跨月 → 每個 distinct (emp, year, month) 各重算一次。"""
    engine = MagicMock()
    changes = [
        _change(1, 10, date(2026, 3, 10)),
        _change(2, 10, date(2026, 3, 20)),  # 同 (10,2026,3)
        _change(3, 20, date(2026, 3, 5)),  # (20,2026,3)
        _change(4, 10, date(2026, 4, 1)),  # (10,2026,4)
    ]
    _recalc_salaries_after_overtime_batch(
        MagicMock(), changes, approved=True, salary_engine=engine
    )
    called = {args.args for args in engine.process_salary_calculation.call_args_list}
    assert called == {(10, 2026, 3), (20, 2026, 3), (10, 2026, 4)}
    assert engine.process_salary_calculation.call_count == 3


def test_reject_only_recalcs_previously_approved():
    """駁回（approved=False）只重算 was_approved=True 的（撤銷已核准影響薪資）。"""
    engine = MagicMock()
    changes = [
        _change(1, 10, date(2026, 3, 10), was_approved=True),  # 撤銷已核准 → 重算
        _change(2, 20, date(2026, 3, 10), was_approved=False),  # 駁回待審 → 不重算
    ]
    _recalc_salaries_after_overtime_batch(
        MagicMock(), changes, approved=False, salary_engine=engine
    )
    engine.process_salary_calculation.assert_called_once_with(10, 2026, 3)


def test_no_engine_is_noop():
    """salary_engine 為 None 時不應拋錯。"""
    _recalc_salaries_after_overtime_batch(
        MagicMock(),
        [_change(1, 10, date(2026, 3, 10))],
        approved=True,
        salary_engine=None,
    )
