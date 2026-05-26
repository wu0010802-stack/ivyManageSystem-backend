"""mark_appraisal step：純寫 appraisal_marked_at audit timestamp。

aggregator filter 改條件後（task 14），離職員工自動繼續出現在當期 cycle，
此 step 本身不動 appraisal 資料，只留 audit timestamp 標記「此員工的離職事件
已被系統標記，後續 dashboard 仍會顯示」。
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive
from sqlalchemy.orm import Session

from models.offboarding import EmployeeOffboardingRecord
from services.offboarding.orchestrator import StepResult


def run(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    now = now_taipei_naive()
    record.appraisal_marked_at = now
    return {
        "step": "mark_appraisal",
        "status": "completed",
        "completed_at": now,
        "payload": None,
        "error": None,
    }
