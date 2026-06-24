"""崩潰防護 P2：批次核准/簽核的 row lock 必須以 id 排序取得，避免批次間 ABBA 死鎖。

問題：
- batch_approve_overtimes / batch_approve_leaves 用 `id.in_(ids).with_for_update().all()`
  無 order_by。單一 `FOR UPDATE WHERE id IN (...)` 不保證列鎖取得順序（依查詢計畫）。
  兩個併發批次帶重疊 id（A=[3,5]、B=[5,3]）可能反向逐列鎖 → ABBA 死鎖。
- year_end._process_settlement_batch 逐筆 `with_for_update()`，依 caller 傳入順序，
  未排序 → 兩個併發批次簽核帶重疊 settlement_ids 但順序不同 → ABBA。

PG row-lock 取得順序無法在 SQLite 行為層重現，故依本 repo 既有鎖序測試慣例
（test_comp_leave_grant_row_lock / test_leave_overtime_security_fixes）以
source-inspection 斷言排序存在。修法與既有 comp_leave_expiry / 才藝課程批次鎖
（order_by(...id)）一致。
"""

import inspect

from api.leaves import batch_approve_leaves
from api.overtimes import batch_approve_overtimes
from api.year_end import _process_settlement_batch


def test_batch_approve_overtimes_orders_lock_by_id():
    src = inspect.getsource(batch_approve_overtimes)
    assert ".with_for_update(" in src, "批次核准應持 row lock"
    assert (
        "order_by(OvertimeRecord.id)" in src
    ), "批次核准的 FOR UPDATE 缺 order_by(id)，兩批次重疊 id 會 ABBA 死鎖"
    # order_by 必須在 with_for_update 之前（同一條 query 內先排序再鎖）
    assert src.index("order_by(OvertimeRecord.id)") < src.index(
        ".with_for_update("
    ), "order_by 必須在 with_for_update 之前"


def test_batch_approve_leaves_orders_lock_by_id():
    src = inspect.getsource(batch_approve_leaves)
    assert ".with_for_update(" in src, "批次核准應持 row lock"
    assert (
        "order_by(LeaveRecord.id)" in src
    ), "批次核准的 FOR UPDATE 缺 order_by(id)，兩批次重疊 id 會 ABBA 死鎖"
    assert src.index("order_by(LeaveRecord.id)") < src.index(
        ".with_for_update("
    ), "order_by 必須在 with_for_update 之前"


def test_year_end_settlement_batch_locks_in_sorted_order():
    src = inspect.getsource(_process_settlement_batch)
    assert ".with_for_update(" in src, "年終批次簽核應逐筆持 row lock"
    assert (
        "sorted(" in src
    ), "年終批次簽核逐筆鎖 settlement 未排序，兩批次重疊 id 不同順序會 ABBA 死鎖"
