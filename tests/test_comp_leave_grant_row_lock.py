"""補休 grant ledger consume/release/expiry 須以 row lock 序列化，防跨月併發 lost-update。

稽核 2026-06-03 P1#3：OvertimeCompLeaveGrant 是補休餘額真值（granted_hours - consumed_hours）。
三路（consume FIFO / release FIFO / expiry）皆以無鎖 .all() 讀 active grant 後做
check-then-act 更新 consumed_hours；approve_leave 唯一序列化是 per-(employee,year,month)
薪資 advisory 鎖，同員工兩張補休假單落在「不同月份」由兩主管併發核准時互不互斥 →
兩交易各讀到同一筆 grant、各算 available、各寫 consumed_hours → last-writer-wins
lost-update：兩張假都核准但 ledger 只記一次消耗 → expiry 把幽靈未消耗額度換算成
UnusedLeavePayoutLog 金額（真實金錢放大）。

修法：三路 grant 查詢加 with_for_update（expiry 為 join 查詢，用 of=OvertimeCompLeaveGrant
只鎖 grant 列、不鎖 Employee）。Postgres row lock 無法在 SQLite 行為層重現，故依本 repo
既有慣例（test_leave_overtime_security_fixes）以 source-inspection 斷言鎖存在。
"""

import inspect

from api.leaves import (
    _consume_compensatory_grants_fifo,
    _release_compensatory_grants_fifo,
)
from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants


def test_consume_fifo_locks_grant_rows():
    src = inspect.getsource(_consume_compensatory_grants_fifo)
    assert (
        ".with_for_update(" in src
    ), "consume FIFO 缺 grant row lock（lost-update 風險）"


def test_release_fifo_locks_grant_rows():
    src = inspect.getsource(_release_compensatory_grants_fifo)
    assert ".with_for_update(" in src, "release FIFO 缺 grant row lock"


def test_expiry_locks_grant_rows():
    src = inspect.getsource(expire_comp_leave_grants)
    assert (
        ".with_for_update(" in src
    ), "expiry 缺 grant row lock（與 consume 併發放大為金額）"
