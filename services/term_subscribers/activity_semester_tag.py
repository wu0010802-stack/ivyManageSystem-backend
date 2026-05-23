"""term.changed subscriber：活動報名學期標籤 reset（placeholder）。

目前 v1：log info 通知，不做實質動作。
未來 v2：清除 ActivityRegistration 「目前學期」標記、或自動把上學期未結算報名歸檔。
"""

import logging
from sqlalchemy.orm import Session

from models.academic_term import AcademicTerm
from utils.term_events import on_term_changed

logger = logging.getLogger(__name__)


@on_term_changed("activity_semester_tag_reset")
def handle(*, old: AcademicTerm | None, new: AcademicTerm, session: Session) -> None:
    logger.info(
        "activity_semester_tag_reset: placeholder triggered for %s-%s → %s-%s "
        "(目前為 no-op，未來實作學期報名標籤更新)",
        old.school_year if old else None,
        old.semester if old else None,
        new.school_year,
        new.semester,
    )
