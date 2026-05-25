"""離職員工過去 12 月考勤記錄 CSV 匯出。

設計：
- 起始日 = max(resign_date - 365 天, hire_date)，終止日 = resign_date
- UTF-8 with BOM（Excel 直接開啟不亂碼）
- 欄位對齊 AttendanceRecord 真實欄位名
- 若員工不存在 → raise ValueError
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models.attendance import Attendance
from models.employee import Employee

logger = logging.getLogger(__name__)

_HEADER = [
    "attendance_date",
    "punch_in_time",
    "punch_out_time",
    "status",
    "is_late",
    "late_minutes",
    "is_early_leave",
    "early_leave_minutes",
    "is_missing_punch_in",
    "is_missing_punch_out",
    "confirmed_action",
    "remark",
]


def generate_attendance_csv(
    session: Session,
    employee_id: int,
    resign_date: date,
) -> bytes:
    """產生員工考勤 CSV（UTF-8 with BOM）。

    Args:
        session: SQLAlchemy session
        employee_id: 員工 DB id
        resign_date: 離職日期（查詢上界，含當日）

    Returns:
        CSV bytes（UTF-8 BOM）

    Raises:
        ValueError: 員工不存在
    """
    employee: Optional[Employee] = session.get(Employee, employee_id)
    if employee is None:
        raise ValueError(f"員工不存在：id={employee_id}")

    hire_date: Optional[date] = employee.hire_date
    start_date = resign_date - timedelta(days=365)
    if hire_date is not None and hire_date > start_date:
        start_date = hire_date

    records = (
        session.query(Attendance)
        .filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= resign_date,
        )
        .order_by(Attendance.attendance_date)
        .all()
    )

    logger.info(
        "考勤 CSV 匯出：employee_id=%s resign_date=%s start_date=%s rows=%d",
        employee_id,
        resign_date,
        start_date,
        len(records),
    )

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_HEADER)

    for rec in records:
        writer.writerow(
            [
                str(rec.attendance_date) if rec.attendance_date else "",
                str(rec.punch_in_time) if rec.punch_in_time else "",
                str(rec.punch_out_time) if rec.punch_out_time else "",
                rec.status or "",
                "1" if rec.is_late else "0",
                rec.late_minutes or 0,
                "1" if rec.is_early_leave else "0",
                rec.early_leave_minutes or 0,
                "1" if rec.is_missing_punch_in else "0",
                "1" if rec.is_missing_punch_out else "0",
                rec.confirmed_action or "",
                rec.remark or "",
            ]
        )

    csv_text = buf.getvalue()
    # UTF-8 with BOM for Excel compatibility
    return b"\xef\xbb\xbf" + csv_text.encode("utf-8")
