"""tests/test_recruitment_revertable.py

驗 assert_student_revertable：學生有下游業務資料時必須擋下還原。

照 test_recruitment_conversion.py 的 SQLite in-memory pattern。
"""

import os
import sys
from datetime import date
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Student, StudentAttendance
from models.fees import StudentFeeRecord
from services.recruitment_funnel import (
    RecruitmentFunnelError,
    assert_student_revertable,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _make_student(session, sid="115-A-01") -> Student:
    s = Student(
        student_id=sid,
        name=f"測試-{sid}",
        lifecycle_status="enrolled",
        is_active=True,
    )
    session.add(s)
    session.flush()
    return s


class TestAssertRevertable:
    def test_clean_student_passes(self, session):
        s = _make_student(session)
        assert assert_student_revertable(session, s.id) is None

    def test_with_attendance_raises(self, session):
        s = _make_student(session)
        att = StudentAttendance(
            student_id=s.id,
            date=date(2026, 5, 1),
            status="出席",
        )
        session.add(att)
        session.flush()
        with pytest.raises(RecruitmentFunnelError) as exc:
            assert_student_revertable(session, s.id)
        assert exc.value.code == "REVERT_STUDENT_HAS_DATA"

    def test_with_fee_record_raises(self, session):
        s = _make_student(session)
        fee = StudentFeeRecord(
            student_id=s.id,
            student_name="測試學生",
            fee_item_name="學費",
            amount_due=10000,
            status="unpaid",
            period="115-1",
        )
        session.add(fee)
        session.flush()
        with pytest.raises(RecruitmentFunnelError):
            assert_student_revertable(session, s.id)

    def test_non_existing_student_passes(self, session):
        # 學生 id 不存在 → 不擋（spec：白名單只查資料；無下游 = 可 revert）
        assert assert_student_revertable(session, student_id=99999) is None
