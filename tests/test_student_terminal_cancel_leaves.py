"""學生轉終態時取消 pending 學生請假（#6 對稱級聯）。

對稱基準：同一終態流程已 cancel pending 接送通知（_cancel_active_dismissal_calls）。
pending 學生請假是結構相同的「在讀 in-flight 項」，離校後不該留在教師審核佇列。
僅取消 pending；approved/歷史已 upsert StudentAttendance，by-design 保留。
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.classroom import Student
from models.student_leave import StudentLeaveRequest
from api.students import _cancel_pending_student_leaves


def _leave(session, student_id, status, day):
    lr = StudentLeaveRequest(
        student_id=student_id,
        applicant_user_id=1,
        leave_type="病假",
        start_date=day,
        end_date=day,
        status=status,
    )
    session.add(lr)
    session.flush()
    return lr.id


def test_cancel_pending_only(test_db_session):
    s = test_db_session
    stu = Student(student_id="S1", name="小明", lifecycle_status="active")
    s.add(stu)
    s.flush()
    pending = _leave(s, stu.id, "pending", date(2026, 5, 20))
    approved = _leave(s, stu.id, "approved", date(2026, 5, 21))
    s.commit()

    n = _cancel_pending_student_leaves(s, stu)
    s.commit()

    assert n == 1
    assert (
        s.query(StudentLeaveRequest).filter_by(id=pending).one().status == "cancelled"
    )
    # approved 不動（歷史紀錄，by-design 保留）
    assert (
        s.query(StudentLeaveRequest).filter_by(id=approved).one().status == "approved"
    )


def test_no_pending_returns_zero(test_db_session):
    s = test_db_session
    stu = Student(student_id="S2", name="小華", lifecycle_status="active")
    s.add(stu)
    s.flush()
    _leave(s, stu.id, "approved", date(2026, 5, 20))
    s.commit()

    assert _cancel_pending_student_leaves(s, stu) == 0
