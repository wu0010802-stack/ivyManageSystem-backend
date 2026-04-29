"""考勤類自我守衛 helper。

業務規則：員工不可建立／修改／刪除自己的考勤、補打卡、缺勤異常等紀錄；
這些操作會直接影響本人薪資扣款。即使持有 ATTENDANCE_WRITE perm 也應交由
他人代為處理，以維持「最少特權」與「不可自審」原則。

對齊既有 idiom：api/overtimes.py:1078-1079、api/leaves.py:1014-1018。
由 IDOR audit Phase 2 抽出（F-015 / F-041 / F-042 / F-046）。
"""

from __future__ import annotations
from fastapi import HTTPException


def require_not_self_attendance(
    current_user: dict,
    target_employee_id: int,
    *,
    detail: str = "不可修改／刪除自己的考勤紀錄",
) -> None:
    """若 caller 的 employee_id 等於 target_employee_id，raise 403。

    None-safe：caller 無 employee_id（純管理帳號）一律放行——管理帳號本就
    不會誤打誤撞自己的考勤；他們的所有操作都會留稽核痕跡。
    """
    caller_eid = current_user.get("employee_id")
    if caller_eid is None:
        return
    if int(target_employee_id) == int(caller_eid):
        raise HTTPException(status_code=403, detail=detail)


def assert_no_self_in_batch(
    current_user: dict,
    employee_ids,
    *,
    detail: str = "批次操作不可包含自己的考勤紀錄",
) -> None:
    """若 employee_ids 含 caller 自己，raise 403。
    用於 bulk upload / batch-confirm 等多列寫入路徑。

    employee_ids 可為 list/set/iterable；None 元素自動忽略。
    """
    caller_eid = current_user.get("employee_id")
    if caller_eid is None:
        return
    caller_eid_int = int(caller_eid)
    for eid in employee_ids:
        if eid is None:
            continue
        try:
            if int(eid) == caller_eid_int:
                raise HTTPException(status_code=403, detail=detail)
        except (TypeError, ValueError):
            # 無法轉 int 的 id 不可能等於 caller，跳過
            continue
