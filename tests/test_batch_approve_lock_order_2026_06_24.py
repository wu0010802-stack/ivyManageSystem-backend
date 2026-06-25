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


def test_appraisal_batch_sign_locks_in_sorted_order():
    """考核批次簽核逐筆 with_for_update 必須以 id 排序取鎖（對齊 year_end）。

    兩位 reviewer 同 cycle、重疊 summary_ids 但順序相反（[3,5] vs [5,3]）→ 反向逐筆鎖
    → ABBA 死鎖 → PostgreSQL abort 整個交易 → 未捕捉 500，整批簽核未落地。
    """
    from api.appraisal import batch_sign_summaries

    src = inspect.getsource(batch_sign_summaries)
    assert ".with_for_update(" in src, "批次簽核應逐筆持 row lock"
    assert (
        "for sid in sorted(" in src
    ), "考核批次簽核逐筆鎖 summary 未排序，兩批次重疊 summary_ids 不同順序會 ABBA 死鎖"


def test_activity_batch_update_payment_orders_lock_by_id():
    """才藝批次繳費 id.in_() FOR UPDATE 必須 order_by(id)（全系統 row-lock 取鎖序不變量）。

    對照 POS checkout（pos._lock_regs）與離園同步皆 order_by(ActivityRegistration.id)；
    本端點漏排序 → 與其反序鎖同批 reg → ABBA 死鎖 → 500。
    """
    from api.activity.registrations_static import batch_update_payment

    src = inspect.getsource(batch_update_payment)
    assert ".with_for_update(" in src, "批次繳費應持 row lock"
    assert (
        "order_by(ActivityRegistration.id)" in src
    ), "批次繳費 FOR UPDATE 缺 order_by(id)，與 POS/離園同步反序鎖同批 reg 會 ABBA 死鎖"
    # 注意：docstring 也提到 `.with_for_update()`，故用 rindex 取「實際 query」那個
    # （最後一個）比對；order_by 只出現在 code，無此困擾。
    assert src.index("order_by(ActivityRegistration.id)") < src.rindex(
        ".with_for_update("
    ), "order_by 必須在 with_for_update 之前（同一條 query 內先排序再鎖）"
