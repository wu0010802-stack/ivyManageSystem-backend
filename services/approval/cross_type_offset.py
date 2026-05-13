"""leave↔OT 跨類抵扣 — 單向觸發：approve leave 時偵測 OT。

⚠️ 金流影響：本 v1 僅在 ApprovalLog 留 metadata，不動 OvertimeRecord 也不接 salary engine。
未來若加上 OvertimeRecord 的 `offset_by_leave_id` 欄位並改寫 engine，啟用後將降低加班費總額。

Feature flag: 環境變數 `ENABLE_LEAVE_OT_OFFSET` 預設 false。
啟用值（不分大小寫）：`1` / `true` / `yes`。

設計取捨：
- 單向觸發：僅在 approve leave 流程呼叫，approve OT 時不反向觸發
  （避免雙向 race 條件並降低首版風險）。
- 跨日 leave 只處理 `start_date` 那一天；多日 leave 的其他日期延後再做。
- 補休假單（`source_overtime_id is not None`）已綁定特定 OT，不再額外 offset，
  避免雙重抵扣。
"""

import os
from typing import Optional

from models.database import LeaveRecord, OvertimeRecord


def _is_enabled() -> bool:
    return os.environ.get("ENABLE_LEAVE_OT_OFFSET", "").lower() in ("1", "true", "yes")


def resolve_cross_type_offset(session, leave: LeaveRecord) -> Optional[OvertimeRecord]:
    """approve leave 時偵測同員工同日已核准的 OT。

    回傳 OvertimeRecord instance 或 None。caller 負責於 ApprovalLog 留 metadata。

    短路條件（任一成立即回 None）：
    - feature flag 關閉
    - leave 為補休假單（已綁定 source_overtime_id）
    - leave 沒有 start_date

    比對條件：
    - employee_id 相同
    - overtime_date == leave.start_date（v1 限制：跨日只看首日）
    - is_approved == True
    - use_comp_leave == False（補休 OT 不再轉加班費，自然不需 offset）

    注意：OvertimeRecord 目前無「已抵扣/已發放」欄位；本 helper 不過濾此狀態，
    呼叫端負責把 offset 事件寫入 ApprovalLog metadata 留軌跡。重複呼叫同一筆 leave
    會重複寫 metadata（業務上 leave 只應該被 approve 一次，由呼叫端的 approval_changed
    旗標把關）。
    """
    if not _is_enabled():
        return None

    if leave.source_overtime_id is not None:
        # 補休假單來源已是 OT，已隱含抵扣關係，不再額外 offset
        return None

    if not getattr(leave, "start_date", None):
        return None

    target_date = leave.start_date

    query = (
        session.query(OvertimeRecord)
        .filter(
            OvertimeRecord.employee_id == leave.employee_id,
            OvertimeRecord.overtime_date == target_date,
            OvertimeRecord.is_approved.is_(True),
            OvertimeRecord.use_comp_leave.is_(False),
        )
        .order_by(OvertimeRecord.id)
    )

    return query.first()
