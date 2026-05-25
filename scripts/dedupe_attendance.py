"""Pre-flight 工具:偵測並清理 attendances 表內 (employee_id, attendance_date) 重複。

用法:
    python scripts/dedupe_attendance.py            # dry-run 列出
    python scripts/dedupe_attendance.py --apply    # 保留每組最小 id,刪除其他

刪除前寫進 audit_logs(entity_type='attendance_records', action='DELETE')。
Migration upgrade 前 SOP 必跑。
"""

import argparse
import os
import sys

# 確保可找到 backend 模組
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collections import defaultdict

from sqlalchemy import text

# models.database 一次性 import 確保所有 SQLAlchemy relationship 都已註冊
from models.database import Attendance, get_session  # noqa: F401


def find_dups(session):
    # 使用 raw SQL 只讀既有欄位，確保 migration 前可正常執行
    # (worktree 模型已含新欄位，但 DB 尚未跑 migration)
    result = session.execute(text("""
            SELECT id, employee_id, attendance_date
            FROM attendances
            ORDER BY employee_id, attendance_date, id
            """)).fetchall()
    by_key = defaultdict(list)
    for row in result:
        by_key[(row.employee_id, row.attendance_date)].append(row)
    dups = {k: v for k, v in by_key.items() if len(v) > 1}
    return dups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="實際刪除;預設只 dry-run 列出",
    )
    args = parser.parse_args()

    session = get_session()
    try:
        dups = find_dups(session)

        if not dups:
            print("[dedupe] 無重複,migration 可直接 upgrade")
            return 0

        print(f"[dedupe] 偵測到 {len(dups)} 組重複:")
        total_to_delete = 0
        for (emp_id, date_), rows in dups.items():
            ids = [r.id for r in rows]
            keep = rows[0].id  # ORDER BY id ASC → 保留最小 id
            delete_ids = [r.id for r in rows[1:]]
            total_to_delete += len(delete_ids)
            print(
                f"  employee_id={emp_id} date={date_}: ids={ids}"
                f" → keep {keep}, delete {delete_ids}"
            )

        if not args.apply:
            print(
                f"\n[dedupe] dry-run only;若無誤,加 --apply 實際刪除 {total_to_delete} 筆"
            )
            return 0

        for (emp_id, date_), rows in dups.items():
            for r in rows[1:]:
                session.execute(
                    text("""
                        INSERT INTO audit_logs
                            (action, entity_type, entity_id, summary, created_at)
                        VALUES ('DELETE', 'attendance_records', :id, :summary, NOW())
                        """),
                    {
                        "id": str(r.id),
                        "summary": f"dedupe before migration: dup of {rows[0].id}",
                    },
                )
                session.execute(
                    text("DELETE FROM attendances WHERE id = :id"),
                    {"id": r.id},
                )
        session.commit()
        print(f"[dedupe] 已刪除 {total_to_delete} 筆")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
