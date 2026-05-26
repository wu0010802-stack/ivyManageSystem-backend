"""services/leave_overlap_service.py — 假單重疊偵測共用服務。

從 api/leaves.py 抽出，避免 api/portal/leaves.py 反向 import admin router
私有 helper（F1 第一波）。核心 SQL 邏輯不動，保留所有時段比對規則。

公開：
- find_overlapping_leave(...) — 含 include_pending 參數
- find_approved_overlapping_leave(...) — 只看已核准

呼叫端：
- api/leaves.py（admin）
- api/portal/leaves.py（教師端）
"""

from datetime import date
from typing import Optional

from sqlalchemy import and_, or_

from models.approval import ApprovalStatus
from models.database import LeaveRecord


def find_overlapping_leave(
    session,
    employee_id: int,
    start_date: date,
    end_date: date,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    exclude_id: int = None,
    include_pending: bool = False,
) -> "LeaveRecord | None":
    """檢查員工在指定日期區間（含時段）是否已有重疊假單。

    預設只檢查已核准假單；`include_pending=True` 時，待審假單也視為衝突。

    時段重疊規則：
    - 若任一方跨多天 → 純日期重疊即視為衝突
    - 若雙方都是同一天的單日假單，且雙方都提供了 start_time/end_time
      → 做時間區間精確比對，不重疊則放行
      （不重疊條件：new_end <= exist_start 或 exist_end <= new_start）
    - 其餘情況（缺乏時間資訊）→ 同日即視為衝突
    """
    q = session.query(LeaveRecord).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.start_date <= end_date,
        LeaveRecord.end_date >= start_date,
    )
    if include_pending:
        q = q.filter(
            or_(
            LeaveRecord.status == ApprovalStatus.APPROVED.value,
            LeaveRecord.status == ApprovalStatus.PENDING.value,
        )
        )
    else:
        q = q.filter(LeaveRecord.status == ApprovalStatus.APPROVED.value)
    if exclude_id is not None:
        q = q.filter(LeaveRecord.id != exclude_id)

    is_new_single_day = start_date == end_date

    # 只有新假單是單日且提供時間資訊時，才能在 DB 層排除確定不重疊的單日記錄
    # HH:MM 字串字典序與時間順序一致，可直接在 SQL 比較
    if is_new_single_day and start_time and end_time:
        q = q.filter(
            ~and_(
                LeaveRecord.start_date == LeaveRecord.end_date,
                LeaveRecord.start_time.isnot(None),
                LeaveRecord.end_time.isnot(None),
                or_(
                    LeaveRecord.end_time <= start_time,
                    LeaveRecord.start_time >= end_time,
                ),
            )
        )

    return q.first()


def find_approved_overlapping_leave(
    session,
    employee_id: int,
    start_date: date,
    end_date: date,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    exclude_id: int = None,
) -> "LeaveRecord | None":
    """檢查員工在指定日期區間（含時段）是否已有「已核准」的請假記錄。"""
    return find_overlapping_leave(
        session,
        employee_id,
        start_date,
        end_date,
        start_time=start_time,
        end_time=end_time,
        exclude_id=exclude_id,
        include_pending=False,
    )
