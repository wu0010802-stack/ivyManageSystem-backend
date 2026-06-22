"""自動畢業排程：才藝報名 sync 失敗時不可靜默吞掉。

follow-up（最終審查 P2）：run_auto_graduation 原本把 sync_registrations_on_
student_deactivate 的例外（含並發 409）以 logger.exception 吞掉並仍 succeeded+=1，
學生畢業但才藝報名未沖帳、無任何結構化 surface → 幽靈未沖帳金額無人跟進。

修法：學生仍畢業（學年末本應畢業），但 sync 失敗記入 result['sync_failed'] 並
logger.warning，供監控/人工跟進。
"""

from datetime import date

from models.classroom import (
    ClassGrade,
    Classroom,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
    Student,
)
from models.student_log import StudentChangeLog  # noqa: F401  註冊 table 供 create_all


def _seed_grad_student(session):
    grade = ClassGrade(name="大班", sort_order=3, is_graduation_grade=True)
    session.add(grade)
    session.flush()
    room = Classroom(name="畢業班", school_year=114, semester=2, grade_id=grade.id)
    session.add(room)
    session.flush()
    st = Student(
        student_id="GS1",
        name="畢業生",
        classroom_id=room.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
        enrollment_date=date(2023, 9, 1),
    )
    session.add(st)
    session.flush()
    sid = st.id
    session.commit()
    return sid


def test_sync_failure_surfaced_in_result_not_swallowed(test_db_session, monkeypatch):
    """sync 失敗（如並發 409）時學生仍畢業，但失敗必須記入 result['sync_failed']
    供人工跟進，而非靜默吞掉。"""
    sid = _seed_grad_student(test_db_session)

    import api.activity._shared as shared
    from fastapi import HTTPException

    def _boom(session, student_id, **kw):
        raise HTTPException(status_code=409, detail="偵測到並發才藝收款，請稍候重試")

    monkeypatch.setattr(shared, "sync_registrations_on_student_deactivate", _boom)

    from services.graduation_scheduler import run_auto_graduation

    result = run_auto_graduation(effective_date=date(2026, 7, 31))

    # 學生仍畢業（學年末本應畢業）
    assert result["succeeded"] == 1
    test_db_session.expire_all()
    st = test_db_session.query(Student).get(sid)
    assert st.lifecycle_status == LIFECYCLE_GRADUATED
    # 但 sync 失敗必須被 surface（非靜默吞掉）
    assert any(f["student_id"] == sid for f in result.get("sync_failed", [])), result
