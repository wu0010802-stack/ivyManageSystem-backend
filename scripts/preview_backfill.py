"""Pre-flight 工具:預演 backfill,列出規模、衝突、預估影響。

不修改任何資料。

用法:
    python scripts/preview_backfill.py
"""

import os
import sys

# 確保可找到 backend 模組
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

from sqlalchemy import text

# models.database 一次性 import 確保所有 SQLAlchemy relationship 都已註冊
from models.approval import ApprovalStatus
from models.database import LeaveRecord, get_session  # noqa: F401


def _has_leave_record_id_col(session) -> bool:
    """檢查 attendances.leave_record_id 欄位是否已存在（migration 前可能不存在）。"""
    row = session.execute(text("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'attendances'
              AND column_name = 'leave_record_id'
            LIMIT 1
            """)).fetchone()
    return row is not None


def main():
    session = get_session()
    try:
        cutoff = date.today() - timedelta(days=365)

        leaves = (
            session.query(LeaveRecord)
            .filter(
                LeaveRecord.status == ApprovalStatus.APPROVED.value,
                LeaveRecord.end_date >= cutoff,
            )
            .all()
        )

        full_day, half_day, hourly = 0, 0, 0
        bad_time = []
        expected_attendance_overwrite = 0
        expected_attendance_create = 0
        expected_conflicts = []

        # 檢查 leave_record_id 欄位是否已存在（migration 前不存在，衝突檢查降級）
        has_lrid_col = _has_leave_record_id_col(session)
        if not has_lrid_col:
            print(
                "[preview] 注意: attendances.leave_record_id 欄位尚未存在"
                "（migration 未跑），衝突偵測將跳過。"
            )

        for lv in leaves:
            if (
                lv.start_time is None
                and lv.end_time is None
                and (lv.leave_hours is None or lv.leave_hours >= 8)
            ):
                full_day += 1
            elif lv.leave_hours and lv.leave_hours < 4.5:
                hourly += 1
            else:
                half_day += 1

            if (
                lv.leave_hours
                and lv.leave_hours < 8
                and (lv.start_time is None or lv.end_time is None)
            ):
                bad_time.append(lv.id)

            # 估計每日 attendance row 情況（raw SQL 避免新欄位不存在）
            d = lv.start_date
            while d <= lv.end_date:
                if has_lrid_col:
                    row = session.execute(
                        text("""
                            SELECT id, leave_record_id
                            FROM attendances
                            WHERE employee_id = :emp_id
                              AND attendance_date = :att_date
                            LIMIT 1
                            """),
                        {"emp_id": lv.employee_id, "att_date": d},
                    ).fetchone()
                    if row is None:
                        expected_attendance_create += 1
                    elif (
                        row.leave_record_id is not None and row.leave_record_id != lv.id
                    ):
                        expected_conflicts.append((lv.id, row.id, d))
                    else:
                        expected_attendance_overwrite += 1
                else:
                    # 欄位不存在時只統計 row 是否已存在
                    row = session.execute(
                        text("""
                            SELECT id
                            FROM attendances
                            WHERE employee_id = :emp_id
                              AND attendance_date = :att_date
                            LIMIT 1
                            """),
                        {"emp_id": lv.employee_id, "att_date": d},
                    ).fetchone()
                    if row is None:
                        expected_attendance_create += 1
                    else:
                        expected_attendance_overwrite += 1
                d += timedelta(days=1)

        print(f"=== Backfill Preview (cutoff = {cutoff}) ===")
        print(f"近 12 月 approved leave 總筆數: {len(leaves)}")
        print(f"  全天: {full_day}")
        print(f"  半天: {half_day}")
        print(f"  小時: {hourly}")
        print(f"預估覆寫既有 AttendanceRecord: {expected_attendance_overwrite}")
        print(f"預估新建 AttendanceRecord: {expected_attendance_create}")

        if bad_time:
            print(f"\n⚠️  缺 start_time/end_time 的部分請假: {len(bad_time)} 筆")
            print("   (需先跑 fix_partial_leave_times.py)")
            print(f"   leave_ids: {bad_time[:10]}{'...' if len(bad_time) > 10 else ''}")

        if expected_conflicts:
            print(f"\n⚠️  同日不同 leave 衝突: {len(expected_conflicts)} 筆")
            for lv_id, att_id, d in expected_conflicts[:10]:
                print(f"   leave_id={lv_id} 卡到 attendance.id={att_id} date={d}")

        if not bad_time and not expected_conflicts:
            print("\n✅ 預演 OK,migration upgrade 可進行")
    finally:
        session.close()


if __name__ == "__main__":
    main()
