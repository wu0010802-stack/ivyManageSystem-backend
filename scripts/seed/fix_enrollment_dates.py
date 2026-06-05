"""scripts/seed/fix_enrollment_dates.py — 修正 enrollment_date 讓整年出缺勤可見(冪等)。

問題:dev DB 多數 active 學生的 `enrollment_date` 被先前的 roster import 蓋成「執行當天」
(2026-06-xx),晚於他們整個 114 學年的出缺勤日期。出勤報表/總覽以 `_student_active_on`
(date < enrollment_date → 視為未在籍)過濾 → 91% 出缺勤被靜默歸零、總覽只剩 test1 班。

修法:把「enrollment_date 晚於 114 學年上學期起點(2025-08-01)」的 active 學生
backfill 成 2024-08-01(早於任何 seed 出缺勤),使整年資料在報表/總覽正確顯示。

冪等:修完後無 active 學生 enrollment_date > cutoff,重跑新增/更新 0 筆。
"""

from __future__ import annotations

import logging
from datetime import date

from scripts.seed._common import session_scope
from models.classroom import Student

logger = logging.getLogger(__name__)

# 114 學年上學期起點;早於此即可涵蓋整年出缺勤(最早 2025-08-04)
_CUTOFF = date(2025, 8, 1)
_TARGET = date(2024, 8, 1)


def step() -> None:
    with session_scope() as session:
        rows = (
            session.query(Student)
            .filter(
                Student.is_active == True,  # noqa: E712
                Student.enrollment_date > _CUTOFF,
            )
            .all()
        )
        n = 0
        for stu in rows:
            stu.enrollment_date = _TARGET
            n += 1
    logger.info("enrollment_date backfill:修正 %d 名學生 → %s", n, _TARGET)
    print(f"[fix_enrollment_dates] 修正 {n} 名 active 學生 enrollment_date → {_TARGET}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    step()
