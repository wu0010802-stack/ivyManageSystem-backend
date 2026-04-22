"""backfill_meeting_overtime_pay.py

一次性回填腳本：將園務會議紀錄（MeetingRecord）從舊的固定 200/100 加班費
改為依勞基法平日加班費公式計算（時薪 × 1 小時 × 1.34）。

規則：
- 僅處理出席（attended=True）且所屬月份尚未封存的 MeetingRecord。
- 使用員工當前的 base_salary 計算。
- 缺席紀錄不處理（原本就應該是 0）。
- 底薪為 0 或未設定者：overtime_pay 設為 0，並記錄警告。

執行方式（在 backend/ 目錄下）：
    python scripts/backfill_meeting_overtime_pay.py
    python scripts/backfill_meeting_overtime_pay.py --dry-run
    python scripts/backfill_meeting_overtime_pay.py --year 2026 --month 4
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import and_

from api.overtimes import calculate_overtime_pay
from models.database import session_scope
from models.event import MeetingRecord
from models.employee import Employee
from models.salary import SalaryRecord
from services.salary.constants import DEFAULT_MEETING_HOURS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _is_finalized(session, employee_id: int, year: int, month: int) -> bool:
    row = (
        session.query(SalaryRecord.is_finalized)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .first()
    )
    return bool(row and row[0])


def backfill(year: int | None = None, month: int | None = None, dry_run: bool = False):
    with session_scope() as session:
        q = session.query(MeetingRecord).filter(
            MeetingRecord.attended == True
        )  # noqa: E712
        if year is not None:
            q = q.filter(
                and_(
                    MeetingRecord.meeting_date >= f"{year}-01-01",
                    MeetingRecord.meeting_date <= f"{year}-12-31",
                )
            )
        records = q.order_by(MeetingRecord.meeting_date, MeetingRecord.id).all()

        emp_ids = {r.employee_id for r in records}
        employees = session.query(Employee).filter(Employee.id.in_(emp_ids)).all()
        emp_map = {e.id: e for e in employees}

        updated = 0
        skipped_finalized = 0
        skipped_zero_base = 0
        unchanged = 0

        for r in records:
            r_year, r_month = r.meeting_date.year, r.meeting_date.month
            if month is not None and r_month != month:
                continue
            if year is not None and r_year != year:
                continue

            if _is_finalized(session, r.employee_id, r_year, r_month):
                skipped_finalized += 1
                continue

            emp = emp_map.get(r.employee_id)
            base = getattr(emp, "base_salary", 0) or 0
            if base <= 0:
                if r.overtime_pay != 0:
                    logger.warning(
                        "員工 id=%s 底薪為 0 或未設定，將 meeting_date=%s 的 overtime_pay 重設為 0（原值 %s）",
                        r.employee_id,
                        r.meeting_date,
                        r.overtime_pay,
                    )
                    if not dry_run:
                        r.overtime_pay = 0
                    updated += 1
                else:
                    skipped_zero_base += 1
                continue

            new_pay = calculate_overtime_pay(base, DEFAULT_MEETING_HOURS, "weekday")
            if round(r.overtime_pay or 0) == round(new_pay):
                unchanged += 1
                continue

            logger.info(
                "員工 id=%s 於 %s：%s → %s",
                r.employee_id,
                r.meeting_date,
                r.overtime_pay,
                new_pay,
            )
            if not dry_run:
                r.overtime_pay = new_pay
            updated += 1

        if dry_run:
            session.rollback()
            logger.info("[DRY-RUN] 無實際寫入")
        else:
            session.commit()

        logger.info(
            "回填完成：更新 %d、未變 %d、封存跳過 %d、底薪為零略過 %d",
            updated,
            unchanged,
            skipped_finalized,
            skipped_zero_base,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="回填園務會議加班費（勞基法平日公式）")
    parser.add_argument("--year", type=int, default=None, help="只回填指定年份")
    parser.add_argument(
        "--month", type=int, default=None, help="只回填指定月份（需搭配 --year）"
    )
    parser.add_argument("--dry-run", action="store_true", help="試算不寫入")
    args = parser.parse_args()
    backfill(year=args.year, month=args.month, dry_run=args.dry_run)
