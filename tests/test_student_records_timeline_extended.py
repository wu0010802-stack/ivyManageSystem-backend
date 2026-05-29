"""tests/test_student_records_timeline_extended.py — timeline 3 source 擴充。

來源：funnel_event / classroom_transfer / payment
"""

from datetime import date, datetime

import pytest

# 必要 model 中央 import — Base.metadata.create_all 才會建表
import models.student_log  # noqa: F401
import models.student_transfer  # noqa: F401
import models.recruitment  # noqa: F401
import models.fees  # noqa: F401
import models.classroom  # noqa: F401


def _create_student_basic(session):
    from models.classroom import Student, Classroom, ClassGrade

    g = ClassGrade(name="小班-TL", sort_order=2, is_active=True)
    session.add(g)
    session.flush()
    cr = Classroom(name="小班A-TL", school_year=113, semester=1, grade_id=g.id)
    session.add(cr)
    session.flush()
    s = Student(
        student_id="113-TL-99",
        name="時間軸測試生",
        classroom_id=cr.id,
        lifecycle_status="active",
        enrollment_date=date(2024, 8, 15),
    )
    session.add(s)
    session.flush()
    return s, cr


def test_timeline_includes_funnel_events(test_db_session):
    from services.student_records_timeline import list_timeline
    from models.recruitment import RecruitmentEventLog, RecruitmentVisit

    s, _ = _create_student_basic(test_db_session)
    rv = RecruitmentVisit(
        child_name="時間軸測試生", month="113.07", visit_date="2024/07/12"
    )
    test_db_session.add(rv)
    test_db_session.flush()
    test_db_session.add(
        RecruitmentEventLog(
            recruitment_visit_id=rv.id,
            student_id=s.id,
            event_type="visit_logged",
            to_stage="visited",
            created_at=datetime(2024, 7, 12, 10, 0),
        )
    )
    test_db_session.commit()

    result = list_timeline(test_db_session, student_id=s.id, types=["funnel_event"])
    items = result["items"]
    assert len(items) >= 1
    assert items[0]["record_type"] == "funnel_event"


def test_timeline_includes_classroom_transfers(test_db_session):
    from services.student_records_timeline import list_timeline
    from models.student_transfer import StudentClassroomTransfer

    s, cr = _create_student_basic(test_db_session)
    test_db_session.add(
        StudentClassroomTransfer(
            student_id=s.id,
            to_classroom_id=cr.id,
            transferred_at=datetime(2024, 8, 15),
        )
    )
    test_db_session.commit()

    result = list_timeline(
        test_db_session, student_id=s.id, types=["classroom_transfer"]
    )
    items = result["items"]
    assert len(items) >= 1
    assert items[0]["record_type"] == "classroom_transfer"


def test_timeline_includes_payments(test_db_session):
    from services.student_records_timeline import list_timeline
    from models.fees import StudentFeeRecord, StudentFeePayment

    s, cr = _create_student_basic(test_db_session)
    rec = StudentFeeRecord(
        student_id=s.id,
        student_name=s.name,
        classroom_name=cr.name,
        fee_item_name="註冊費",
        amount_due=5000,
        amount_paid=5000,
        status="paid",
        payment_date=date(2024, 8, 1),
        period="113-1",
    )
    test_db_session.add(rec)
    test_db_session.flush()
    test_db_session.add(
        StudentFeePayment(record_id=rec.id, amount=5000, payment_date=date(2024, 8, 1))
    )
    test_db_session.commit()

    result = list_timeline(test_db_session, student_id=s.id, types=["payment"])
    items = result["items"]
    assert len(items) >= 1
    assert items[0]["record_type"] == "payment"
    assert "5000" in items[0]["summary"]


def test_timeline_combined_sources_sorted_descending(test_db_session):
    """混合多 source — 時間倒序。"""
    from services.student_records_timeline import list_timeline
    from models.recruitment import RecruitmentEventLog, RecruitmentVisit
    from models.student_transfer import StudentClassroomTransfer

    s, cr = _create_student_basic(test_db_session)
    rv = RecruitmentVisit(child_name=s.name, month="113.07", visit_date="2024/07/12")
    test_db_session.add(rv)
    test_db_session.flush()
    test_db_session.add(
        RecruitmentEventLog(
            recruitment_visit_id=rv.id,
            student_id=s.id,
            event_type="visit_logged",
            to_stage="visited",
            created_at=datetime(2024, 7, 12, 10, 0),
        )
    )
    test_db_session.add(
        StudentClassroomTransfer(
            student_id=s.id,
            to_classroom_id=cr.id,
            transferred_at=datetime(2024, 8, 15),
        )
    )
    test_db_session.commit()

    result = list_timeline(
        test_db_session,
        student_id=s.id,
        types=["funnel_event", "classroom_transfer"],
    )
    items = result["items"]
    assert len(items) >= 2
    # 倒序：8/15 在 7/12 之前
    # 時間軸已做 ISO 字串轉換，用字串比較
    assert items[0]["occurred_at"] >= items[1]["occurred_at"]


def test_timeline_record_types_includes_three_new(test_db_session):
    """RECORD_TYPES 已加新 source。"""
    from services.student_records_timeline import RECORD_TYPES

    assert "funnel_event" in RECORD_TYPES
    assert "classroom_transfer" in RECORD_TYPES
    assert "payment" in RECORD_TYPES
