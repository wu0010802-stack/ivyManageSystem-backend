"""Track C — qa-loop round2（2026-06-29）overtime/leave 兩個硬故障 P2。

P2-1（補休 grant 撞 UNIQUE→500）：overtime_comp_leave_grants.overtime_record_id 為 unique，
一張 OT 一生只能一筆 grant。_revoke_comp_leave_grant 退審/駁回時只設 grant.status='revoked'
不刪列，unique 槽仍被佔；之後再核准走 _grant_comp_leave_quota 一律 session.add 新列 →
撞 UNIQUE → IntegrityError → approve_overtime except 攔成 500、交易回滾，該 OT 永遠無法再核准。
（改判/退審再核准/批次三條合法路徑都會中。）修法：grant 時重用既有列（upsert）。

P2-2（update_leave 漏同日部分假衝突守衛）：create_leave 有 _assert_no_same_day_partial_collision
擋「同日已有 pending/approved 假、又登錄帶時段的單日部分假」（核准時 sync.apply 會撞 422
永遠無法核准）；update_leave 驗證鏈漏掉它，可 update 成此情境產生永遠無法核准的假單。
修法：update_leave 補同一守衛並帶 exclude_id=leave_id。
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from models.overtime import OvertimeRecord
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.leave import LeaveRecord
import api.overtimes as ot_api
import api.leaves as leaves_api

# ── P2-1：補休 grant→revoke→grant 不撞 UNIQUE ────────────────────────────────


def test_grant_comp_leave_reuses_existing_revoked_row(test_db_session):
    """同一 OT 先 grant→revoke（留 revoked 列）後再 grant，須重用既有列、不撞 UNIQUE。"""
    session = test_db_session
    ot = OvertimeRecord(
        employee_id=1,
        overtime_date=date(2026, 1, 10),
        overtime_type="weekday",
        hours=8,
        use_comp_leave=True,
        comp_leave_granted=False,
        status="approved",
    )
    session.add(ot)
    session.commit()
    # 模擬「先前 grant 後又 revoke」遺留的 revoked grant 列（unique 槽已被佔）
    session.add(
        OvertimeCompLeaveGrant(
            overtime_record_id=ot.id,
            employee_id=1,
            granted_hours=8,
            granted_at=ot.overtime_date,
            expires_at=ot.overtime_date + timedelta(days=365),
            status="revoked",
        )
    )
    session.commit()

    # 再核准 → 不可 add 新列撞 UNIQUE；應重用既有列並重置為 active。
    ot.comp_leave_granted = False
    ot_api._grant_comp_leave_quota(session, ot, {})
    session.commit()

    grants = (
        session.query(OvertimeCompLeaveGrant)
        .filter(OvertimeCompLeaveGrant.overtime_record_id == ot.id)
        .all()
    )
    assert len(grants) == 1, "同一 OT 應只有一筆 grant 列（重用而非新增）"
    assert grants[0].status == "active"
    assert grants[0].granted_hours == 8


# ── P2-2：update_leave 同日部分假衝突守衛 ────────────────────────────────────


def _mk_leave(session, emp_id, d, status, start_time=None, end_time=None, hours=8):
    lv = LeaveRecord(
        employee_id=emp_id,
        leave_type="personal",
        start_date=d,
        end_date=d,
        start_time=start_time,
        end_time=end_time,
        leave_hours=hours,
        status=status,
    )
    session.add(lv)
    session.commit()
    return lv


def test_update_leave_rejects_same_day_partial_collision(test_db_session):
    """把假單 update 成壓在另一筆同日（時段不重疊）假上的單日部分假 → 409（與 create 對齊）。"""
    session = test_db_session
    day = date(2026, 3, 10)
    # 同日已有一筆 approved 部分假 09:00-10:00
    _mk_leave(
        session, 1, day, "approved", start_time="09:00", end_time="10:00", hours=1
    )
    # 另一筆待更新的假（不同日）
    target = _mk_leave(session, 1, date(2026, 3, 11), "pending", hours=8)

    data = leaves_api.LeaveUpdate(
        start_date=day,
        end_date=day,
        start_time="14:00",
        end_time="15:00",
        leave_hours=1,
    )
    with pytest.raises(HTTPException) as exc:
        leaves_api.update_leave(
            target.id,
            data,
            MagicMock(),
            current_user={"user_id": 999, "employee_id": 999, "role": "hr"},
        )
    assert exc.value.status_code == 409
