"""churn_service tests

注意：LeaveRecord 為員工假單，學生請假直接透過 StudentAttendance.status 記錄。
  - "缺席" = 無故缺席 → 計入連續缺勤
  - "病假" / "事假" = 有故缺席 → 不計入連續缺勤，視為「有效請假覆蓋」
  - "出席" / "遲到" = 有來 → 中斷缺勤串
"""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.classroom import Classroom, Student, StudentAttendance
from models.fees import FeeItem, StudentFeeRecord
from models.student_log import StudentChangeLog


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _classroom(session, name="小班A"):
    import uuid

    suffix = uuid.uuid4().hex[:6]
    c = Classroom(name=f"{name}-{suffix}", is_active=True)
    session.add(c)
    session.commit()
    return c


def _student(session, *, name, classroom, status="active"):
    import uuid

    suffix = uuid.uuid4().hex[:6]
    s = Student(
        name=name,
        student_id=f"S-{name}-{suffix}",
        classroom_id=classroom.id,
        lifecycle_status=status,
        is_active=(status in ("active", "on_leave")),
        enrollment_date=date(2025, 9, 1),
    )
    session.add(s)
    session.commit()
    return s


def _attendance(session, *, student, day, status):
    a = StudentAttendance(student_id=student.id, date=day, status=status)
    session.add(a)
    session.commit()


def test_consecutive_absence_3_days_triggers(session):
    from services.analytics.churn_service import detect_signal_consecutive_absence

    cls = _classroom(session)
    s = _student(session, name="A", classroom=cls)
    other = _student(session, name="B", classroom=cls)

    # 4/20 (Mon) 4/21 (Tue) 4/22 (Wed) 連續缺席（工作日）
    for d in (date(2026, 4, 20), date(2026, 4, 21), date(2026, 4, 22)):
        _attendance(session, student=s, day=d, status="缺席")
        _attendance(
            session, student=other, day=d, status="出席"
        )  # 另一人有來，不觸發整班漏點名

    triggered = detect_signal_consecutive_absence(
        session,
        today=date(2026, 4, 23),  # Thu
    )
    assert s.id in {t["student_id"] for t in triggered}


def test_consecutive_absence_2_days_no_trigger(session):
    from services.analytics.churn_service import detect_signal_consecutive_absence

    cls = _classroom(session)
    s = _student(session, name="A", classroom=cls)
    other = _student(session, name="B", classroom=cls)

    for d in (date(2026, 4, 21), date(2026, 4, 22)):
        _attendance(session, student=s, day=d, status="缺席")
        _attendance(session, student=other, day=d, status="出席")

    triggered = detect_signal_consecutive_absence(
        session,
        today=date(2026, 4, 23),
    )
    assert s.id not in {t["student_id"] for t in triggered}


def test_absence_with_approved_leave_no_trigger(session):
    """病假/事假 status 記錄不視為連續缺勤，不觸發預警。

    設計決策：學生請假直接反映在 StudentAttendance.status（"病假"/"事假"），
    沒有獨立的學生假單表，因此不需要查 LeaveRecord（員工專用）。
    """
    from services.analytics.churn_service import detect_signal_consecutive_absence

    cls = _classroom(session)
    s = _student(session, name="A", classroom=cls)
    other = _student(session, name="B", classroom=cls)

    # 4/20: 病假；4/21~4/22: 缺席 → 缺席不連續達 3 天（病假中斷了算法）
    _attendance(session, student=s, day=date(2026, 4, 20), status="病假")
    _attendance(session, student=s, day=date(2026, 4, 21), status="缺席")
    _attendance(session, student=s, day=date(2026, 4, 22), status="缺席")
    for d in (date(2026, 4, 20), date(2026, 4, 21), date(2026, 4, 22)):
        _attendance(session, student=other, day=d, status="出席")

    triggered = detect_signal_consecutive_absence(
        session,
        today=date(2026, 4, 23),
    )
    assert s.id not in {t["student_id"] for t in triggered}


def test_class_unrecorded_day_skipped(session):
    """整班 active 學生當日皆 absent → 視為老師沒點名，不觸發。"""
    from services.analytics.churn_service import detect_signal_consecutive_absence

    cls = _classroom(session)
    a = _student(session, name="A", classroom=cls)
    b = _student(session, name="B", classroom=cls)

    # 三天，全班皆 缺席 → 應整批跳過
    for d in (date(2026, 4, 20), date(2026, 4, 21), date(2026, 4, 22)):
        _attendance(session, student=a, day=d, status="缺席")
        _attendance(session, student=b, day=d, status="缺席")

    triggered = detect_signal_consecutive_absence(
        session,
        today=date(2026, 4, 23),
    )
    assert a.id not in {t["student_id"] for t in triggered}
    assert b.id not in {t["student_id"] for t in triggered}


def test_on_leave_30_days_triggers(session):
    from services.analytics.churn_service import detect_signal_long_on_leave

    cls = _classroom(session)
    s = _student(session, name="長假學生", classroom=cls, status="on_leave")
    log = StudentChangeLog(
        student_id=s.id,
        school_year=114,  # 民國 114 = 西元 2025
        semester=2,
        event_type="休學",
        event_date=date(2026, 3, 20),  # today=4/23 → 34 天
    )
    session.add(log)
    session.commit()

    triggered = detect_signal_long_on_leave(session, today=date(2026, 4, 23))
    assert s.id in {t["student_id"] for t in triggered}


def test_on_leave_29_days_no_trigger(session):
    from services.analytics.churn_service import detect_signal_long_on_leave

    cls = _classroom(session)
    s = _student(session, name="短假學生", classroom=cls, status="on_leave")
    log = StudentChangeLog(
        student_id=s.id,
        school_year=114,
        semester=2,
        event_type="休學",
        event_date=date(2026, 3, 26),  # 28 天
    )
    session.add(log)
    session.commit()

    triggered = detect_signal_long_on_leave(session, today=date(2026, 4, 23))
    assert s.id not in {t["student_id"] for t in triggered}


def test_fee_overdue_triggers(session):
    from services.analytics.churn_service import detect_signal_fee_overdue

    cls = _classroom(session)
    s = _student(session, name="欠費學生", classroom=cls)

    fi = FeeItem(name="月費", amount=5000, period="2025-2", is_active=True)
    session.add(fi)
    session.commit()
    rec = StudentFeeRecord(
        student_id=s.id,
        student_name=s.name,
        classroom_name="小班A",
        fee_item_id=fi.id,
        fee_item_name=fi.name,
        amount_due=5000,
        payment_date=None,  # 未繳
        period="2025-2",
    )
    session.add(rec)
    session.commit()

    # 學期始 2026-02-01 + 14 天 = 2/15；today=2026-04-23 → 已逾期
    # 當期：today=2026-04-23 → 4 月 in [2,7] → semester=2, year_roc=114 → period="2025-2" ✓
    triggered = detect_signal_fee_overdue(session, today=date(2026, 4, 23))
    assert s.id in {t["student_id"] for t in triggered}


def test_fee_paid_no_trigger(session):
    from services.analytics.churn_service import detect_signal_fee_overdue

    cls = _classroom(session)
    s = _student(session, name="已繳學生", classroom=cls)
    fi = FeeItem(name="月費", amount=5000, period="2025-2", is_active=True)
    session.add(fi)
    session.commit()
    rec = StudentFeeRecord(
        student_id=s.id,
        student_name=s.name,
        classroom_name="小班A",
        fee_item_id=fi.id,
        fee_item_name=fi.name,
        amount_due=5000,
        payment_date=date(2026, 2, 10),  # 已繳
        period="2025-2",
    )
    session.add(rec)
    session.commit()

    triggered = detect_signal_fee_overdue(session, today=date(2026, 4, 23))
    assert s.id not in {t["student_id"] for t in triggered}
