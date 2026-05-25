"""離職 ZIP bundle 產生服務。

ZIP 內容：
1. certificate.pdf  ← record.certificate_pdf_path 讀檔
2. salary_<year>_<month:02d>.pdf  ← 每月薪資單 PDF（reuse finance.salary_slip）
3. attendance.csv   ← 過去 12 月考勤（reuse attendance_csv）

設計：
- record.certificate_pdf_path 為 None → raise ValueError（前置條件）
- 單月 salary PDF 失敗不擋整 ZIP（try/except，跳過該月）
- 查詢範圍：resign_date 前 12 個月（含）的薪資記錄
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from models.employee import Employee
from models.offboarding import EmployeeOffboardingRecord
from models.salary import SalaryRecord
from services.finance.salary_slip import generate_salary_pdf
from services.offboarding.attendance_csv import generate_attendance_csv

logger = logging.getLogger(__name__)


def build_offboarding_zip(
    session: Session,
    record: EmployeeOffboardingRecord,
) -> bytes:
    """產生離職 ZIP bundle bytes。

    Args:
        session: SQLAlchemy session
        record: EmployeeOffboardingRecord（需已有 certificate_pdf_path）

    Returns:
        ZIP bytes（DEFLATED 壓縮）

    Raises:
        ValueError: certificate_pdf_path 為 None（尚未產離職證明）
    """
    if record.certificate_pdf_path is None:
        raise ValueError(
            f"certificate_pdf_path 尚未產生：employee_id={record.employee_id}"
        )

    employee = session.get(Employee, record.employee_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. 離職證明 PDF
        cert_path = Path(record.certificate_pdf_path)
        try:
            cert_bytes = cert_path.read_bytes()
            zf.writestr("certificate.pdf", cert_bytes)
            logger.info(
                "bundle：寫入 certificate.pdf size=%d bytes (employee_id=%s)",
                len(cert_bytes),
                record.employee_id,
            )
        except OSError as e:
            logger.error(
                "bundle：certificate.pdf 讀檔失敗 path=%s error=%s",
                record.certificate_pdf_path,
                e,
            )
            raise ValueError(
                f"certificate_pdf_path 讀檔失敗：{record.certificate_pdf_path}"
            ) from e

        # 2. 薪資單 PDF（過去 12 個月）
        resign_date = record.resign_date
        # 查詢範圍：resign_date 所在月份及前 11 個月
        salary_records = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == record.employee_id,
                # 在 resign 日期的年月之前（含）的 12 個月內
                (SalaryRecord.salary_year * 100 + SalaryRecord.salary_month)
                >= (
                    (resign_date.year - 1) * 100 + resign_date.month
                    if resign_date.month > 0
                    else (resign_date.year - 2) * 100 + 12
                ),
                (SalaryRecord.salary_year * 100 + SalaryRecord.salary_month)
                <= (resign_date.year * 100 + resign_date.month),
            )
            .order_by(SalaryRecord.salary_year, SalaryRecord.salary_month)
            .all()
        )

        for sr in salary_records:
            fname = f"salary_{sr.salary_year}_{sr.salary_month:02d}.pdf"
            try:
                pdf_bytes = generate_salary_pdf(
                    sr, employee, sr.salary_year, sr.salary_month
                )
                zf.writestr(fname, pdf_bytes)
                logger.info(
                    "bundle：寫入 %s size=%d bytes",
                    fname,
                    len(pdf_bytes),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "bundle：%s 產生失敗（跳過）error=%s",
                    fname,
                    e,
                )

        # 3. 考勤 CSV
        try:
            csv_bytes = generate_attendance_csv(
                session, record.employee_id, record.resign_date
            )
            zf.writestr("attendance.csv", csv_bytes)
            logger.info(
                "bundle：寫入 attendance.csv size=%d bytes (employee_id=%s)",
                len(csv_bytes),
                record.employee_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "bundle：attendance.csv 產生失敗（跳過）error=%s",
                e,
            )

    return buf.getvalue()
