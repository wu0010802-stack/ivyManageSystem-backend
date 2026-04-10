"""backfill_expected_start_label.py

一次性回填腳本：為 recruitment_visits 中 expected_start_label IS NULL 的歷史記錄
計算並填入預計就讀月份標籤。

執行方式（在 backend/ 目錄下）：
    python scripts/backfill_expected_start_label.py

需要先執行 alembic upgrade head 確認欄位已建立。
"""

import sys
import os

# 確保可以找到 backend 模組
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from models.database import session_scope
from models.recruitment import RecruitmentVisit
from api.recruitment import _extract_expected_label_from_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def backfill():
    with session_scope() as session:
        total = session.query(RecruitmentVisit).filter(
            RecruitmentVisit.expected_start_label.is_(None)
        ).count()
        logger.info(f"待回填記錄：{total} 筆")

    offset = 0
    updated = 0
    while True:
        with session_scope() as session:
            batch = (
                session.query(RecruitmentVisit)
                .filter(RecruitmentVisit.expected_start_label.is_(None))
                .order_by(RecruitmentVisit.id)
                .limit(BATCH_SIZE)
                .all()
            )
            if not batch:
                break
            for record in batch:
                record.expected_start_label = _extract_expected_label_from_text(
                    record.notes, record.parent_response, record.grade
                )
            updated += len(batch)
            offset += len(batch)

        logger.info(f"已回填 {updated} / {total} 筆")

    logger.info(f"回填完成，共更新 {updated} 筆")


if __name__ == "__main__":
    backfill()
