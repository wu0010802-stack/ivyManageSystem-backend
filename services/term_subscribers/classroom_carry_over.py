"""term.changed subscriber：同學年 1→2 carry-over Classroom rows 與 Student.classroom_id。

行為矩陣：
- old=None：跳過（初次設定）+ log info
- same school_year semester 1→2：carry-over 全部 active classroom + 學生
- 跨 school_year（X-2 → X+1-1）：no-op + log info（admin 手動編班）
- 其他（2→1 同年 / 大跳）：no-op + log warning（應由 admin 確認異常切換）
"""

import logging
from sqlalchemy.orm import Session

from models.academic_term import AcademicTerm
from models.classroom import Classroom, Student
from utils.term_events import on_term_changed

logger = logging.getLogger(__name__)


@on_term_changed("classroom_carry_over")
def handle(*, old: AcademicTerm | None, new: AcademicTerm, session: Session) -> None:
    if old is None:
        logger.info("classroom_carry_over: 初次設定 is_current，跳過 carry-over")
        return

    if old.school_year == new.school_year and old.semester == 1 and new.semester == 2:
        _carry_over_same_year(old, new, session)
        return

    if (
        old.school_year + 1 == new.school_year
        and old.semester == 2
        and new.semester == 1
    ):
        logger.info(
            "classroom_carry_over: 跨學年 %s-2 → %s-1，no-op（請 admin 手動編班升級）",
            old.school_year,
            new.school_year,
        )
        return

    logger.warning(
        "classroom_carry_over: 非典型切換 %s-%s → %s-%s，no-op",
        old.school_year,
        old.semester,
        new.school_year,
        new.semester,
    )


def _carry_over_same_year(
    old: AcademicTerm, new: AcademicTerm, session: Session
) -> None:
    """同學年 1→2：每個 old classroom 生新 row（複製欄位、新 id），
    再把該 classroom 名下 active student.classroom_id 重新指向新 row。"""
    # 防重複：目標學期已存在班級 → 視為已 carry-over，跳過（排程器冪等保險）
    already = (
        session.query(Classroom)
        .filter(
            Classroom.school_year == new.school_year,
            Classroom.semester == new.semester,
        )
        .first()
    )
    if already is not None:
        logger.info(
            "classroom_carry_over: 目標學期 %s-%s 已有班級，跳過 carry-over",
            new.school_year,
            new.semester,
        )
        return
    old_classrooms = (
        session.query(Classroom)
        .filter(
            Classroom.school_year == old.school_year,
            Classroom.semester == old.semester,
        )
        .all()
    )
    if not old_classrooms:
        logger.info("classroom_carry_over: 上學期沒有 classroom，跳過")
        return

    old_to_new: dict[int, int] = {}
    for old_cls in old_classrooms:
        new_cls = Classroom(
            name=old_cls.name,
            school_year=new.school_year,
            semester=new.semester,
            grade_id=old_cls.grade_id,
            capacity=old_cls.capacity,
            head_teacher_id=old_cls.head_teacher_id,
            assistant_teacher_id=old_cls.assistant_teacher_id,
            art_teacher_id=old_cls.art_teacher_id,
            class_code=old_cls.class_code,
        )
        session.add(new_cls)
        session.flush()
        old_to_new[old_cls.id] = new_cls.id

    moved = 0
    for old_id, new_id in old_to_new.items():
        result = (
            session.query(Student)
            .filter(
                Student.classroom_id == old_id,
                Student.is_active.is_(True),
            )
            .update({Student.classroom_id: new_id}, synchronize_session=False)
        )
        moved += result

    logger.info(
        "classroom_carry_over: 複製 %d 個班級，遷移 %d 位學生 (%s-%s → %s-%s)",
        len(old_classrooms),
        moved,
        old.school_year,
        old.semester,
        new.school_year,
        new.semester,
    )
