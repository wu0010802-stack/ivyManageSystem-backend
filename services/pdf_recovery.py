"""啟動時掃 StudentGrowthReport 孤兒 'generating' row 標 failed。

時機：app lifespan startup，**在** pdf_worker 開始接 submit 之前。

假設：單 worker 部署（同 graduation/recruitment_term_advance scheduler 的 CLAUDE.md
慣例）。多 worker 部署只開 leader 上的 PDF_WORKER_RECOVERY_ENABLED=true，否則
worker B 啟動會把 worker A 正在跑的 job 誤標 failed。
"""

from __future__ import annotations

import logging

from models.database import StudentGrowthReport, session_scope
from models.portfolio import REPORT_STATUS_FAILED, REPORT_STATUS_GENERATING

logger = logging.getLogger(__name__)


_INTERRUPTED_MESSAGE = "PDF 生成被中斷（伺服器重啟），請重新觸發"


def recover_orphan_pdf_jobs() -> int:
    """把所有 status='generating' 標為 failed，回傳處理筆數。"""
    with session_scope() as session:
        orphans = (
            session.query(StudentGrowthReport)
            .filter(StudentGrowthReport.status == REPORT_STATUS_GENERATING)
            .all()
        )
        for r in orphans:
            r.status = REPORT_STATUS_FAILED
            r.error_message = _INTERRUPTED_MESSAGE
        count = len(orphans)

    if count:
        logger.warning(
            "PDF recovery: %d orphan 'generating' report(s) marked failed", count
        )
    else:
        logger.info("PDF recovery: no orphan reports")
    return count
