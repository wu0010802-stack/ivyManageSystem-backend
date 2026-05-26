"""generate_certificate step：呼叫 PDF service → 寫檔 → 寫 record.certificate_pdf_path。

寫檔位置：storage/offboarding_certificates/{employee_id}_{resign_date}.pdf
失敗（磁碟滿 / 權限不足 / 字型 fail）→ raise OffboardingError(CERTIFICATE_GENERATION_FAILED)
"""

import logging
from datetime import datetime
from utils.taipei_time import now_taipei_naive
from pathlib import Path

from sqlalchemy.orm import Session

from models.offboarding import EmployeeOffboardingRecord
from services.employee_offboarding_certificate_pdf import generate_certificate_pdf
from services.offboarding.orchestrator import OffboardingError, StepResult

logger = logging.getLogger(__name__)

STORAGE_DIR = Path("storage/offboarding_certificates")


def run(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    """產生離職證明 PDF 並寫入磁碟。

    Args:
        session: SQLAlchemy session
        record: EmployeeOffboardingRecord（提供 employee_id / resign_date）

    Returns:
        StepResult with step="generate_certificate"

    Raises:
        OffboardingError: PDF 產生或寫檔失敗 → code="CERTIFICATE_GENERATION_FAILED"
    """
    try:
        pdf_bytes = generate_certificate_pdf(
            session, record.employee_id, record.resign_date
        )

        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = (
            STORAGE_DIR / f"{record.employee_id}_{record.resign_date.isoformat()}.pdf"
        )
        pdf_path.write_bytes(pdf_bytes)

        now = now_taipei_naive()
        record.certificate_pdf_path = str(pdf_path)
        record.certificate_generated_at = now

        logger.info(
            "離職證明 PDF 已產：employee_id=%s path=%s size=%d bytes",
            record.employee_id,
            pdf_path,
            len(pdf_bytes),
        )

        return {
            "step": "generate_certificate",
            "status": "completed",
            "completed_at": now,
            "payload": {"pdf_path": str(pdf_path), "bytes": len(pdf_bytes)},
            "error": None,
        }
    except OSError as e:
        # 磁碟 / 權限失敗
        raise OffboardingError(
            f"離職證明 PDF 寫檔失敗: {e}",
            code="CERTIFICATE_GENERATION_FAILED",
        ) from e
    except Exception as e:
        # 字型 / reportlab 失敗
        raise OffboardingError(
            f"離職證明 PDF 產生失敗: {e}",
            code="CERTIFICATE_GENERATION_FAILED",
        ) from e
