"""Pre-flight 工具:掃既有部分請假缺 start_time/end_time 的 row,
列表並可選擇退審到 pending 讓 admin 重新審核補時段。

用法:
    python scripts/fix_partial_leave_times.py            # dry-run
    python scripts/fix_partial_leave_times.py --apply    # 把缺時段的 row status='pending'(退審)
"""

import argparse
import os
import sys

# 確保可找到 backend 模組
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from utils.taipei_time import today_taipei

from sqlalchemy import text

# models.database 一次性 import 確保所有 SQLAlchemy relationship 都已註冊
from models.approval import ApprovalStatus
from models.database import LeaveRecord, get_session  # noqa: F401


def find_bad_partial(session):
    cutoff = today_taipei() - timedelta(days=365)  
    return (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.status == ApprovalStatus.APPROVED.value,
            LeaveRecord.end_date >= cutoff,
            LeaveRecord.leave_hours.isnot(None),
            LeaveRecord.leave_hours < 8,
            (LeaveRecord.start_time.is_(None) | LeaveRecord.end_time.is_(None)),
        )
        .all()
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="把缺 start_time/end_time 的 row 退審到 pending",
    )
    args = parser.parse_args()

    session = get_session()
    try:
        bad = find_bad_partial(session)

        if not bad:
            print("[fix_partial] 無問題 row,migration 可進行")
            return 0

        print(
            f"[fix_partial] 偵測到 {len(bad)} 筆已核可的部分請假缺 start_time/end_time:"
        )
        for lv in bad:
            print(
                f"  id={lv.id} employee_id={lv.employee_id} "
                f"date={lv.start_date}..{lv.end_date} hours={lv.leave_hours} "
                f"start_time={lv.start_time} end_time={lv.end_time}"
            )

        if not args.apply:
            print(f"\n[fix_partial] dry-run only;加 --apply 退審 {len(bad)} 筆")
            return 0

        for lv in bad:
            session.execute(
                text("""
                    INSERT INTO audit_logs
                        (action, entity_type, entity_id, summary, created_at)
                    VALUES ('UPDATE', 'leave_records', :id, :summary, NOW())
                    """),
                {
                    "id": str(lv.id),
                    "summary": "fix_partial_leave_times: 退審至 pending 待補時段",
                },
            )
            lv.status = ApprovalStatus.PENDING.value  # 退審到 pending
        session.commit()
        print(f"[fix_partial] 已退審 {len(bad)} 筆,請通知 admin 補時段後重新核可")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
