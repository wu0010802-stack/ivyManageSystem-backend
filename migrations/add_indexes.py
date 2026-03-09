"""
一次性腳本：在現有資料庫上建立索引
執行方式: cd backend && python add_indexes.py
"""

import logging
from sqlalchemy import text
from models.database import get_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INDEXES = [
    ("ix_attendance_emp_date", "attendances", ["employee_id", "attendance_date"]),
    ("ix_leave_emp_dates", "leave_records", ["employee_id", "start_date", "end_date"]),
    ("ix_overtime_emp_date", "overtime_records", ["employee_id", "overtime_date"]),
    ("ix_salary_emp_ym", "salary_records", ["employee_id", "salary_year", "salary_month"]),
    ("ix_meeting_emp_date", "meeting_records", ["employee_id", "meeting_date"]),
    ("ix_student_classroom", "students", ["classroom_id", "is_active"]),
]


def main():
    engine = get_engine()
    with engine.connect() as conn:
        for idx_name, table, columns in INDEXES:
            cols = ", ".join(columns)
            sql = f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})"
            try:
                conn.execute(text(sql))
                logger.info(f"Created index: {idx_name} on {table}({cols})")
            except Exception as e:
                logger.warning(f"Index {idx_name} skipped: {e}")
        conn.commit()
    logger.info("Done.")


if __name__ == "__main__":
    main()
