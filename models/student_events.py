"""models/student_events.py — Student.student_id 顯示快取的 before_flush 維護。

對 session 內「新建」或「classroom_id/enrollment_seq 有異動」且 enrollment_seq
非 NULL 的 Student，重算 student_id 顯示快取。涵蓋所有 ORM 寫入路徑
（報到分班 insert、bulk_transfer 與 PUT /students 的屬性 set）。

不涵蓋 query.update(synchronize_session=False) 的 bulk path（如 classroom_carry_over
同學年 1→2），但該 path 同學年同年級、顯示值不變，故無需重算。
"""

from sqlalchemy import event
from sqlalchemy.orm import Session


@event.listens_for(Session, "before_flush")
def _recompute_student_display_id(session, flush_context, instances):
    from models.classroom import Student
    from services.student_numbering import compute_student_display_id

    targets = [
        obj
        for obj in (set(session.new) | set(session.dirty))
        if isinstance(obj, Student)
    ]
    if not targets:
        return

    with session.no_autoflush:
        for stu in targets:
            if getattr(stu, "enrollment_seq", None) is None:
                continue
            new_id = compute_student_display_id(session, stu)
            if new_id and stu.student_id != new_id:
                stu.student_id = new_id
