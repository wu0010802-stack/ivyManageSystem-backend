"""tests/test_student_profile.py

驗證學生檔案聚合端點的各 summary 函數與 assemble_profile 整合結果。
"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import (
    LIFECYCLE_ACTIVE,
    Classroom,
    Student,
    StudentAttendance,
    StudentIncident,
)
from models.fees import FeeItem, StudentFeeRecord
from models.guardian import Guardian
from models.student_log import StudentChangeLog
from services.student_profile import (
    _default_attendance_window,
    assemble_profile,
    get_attendance_summary,
    get_fee_summary,
    get_guardians,
    get_timeline,
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


@pytest.fixture
def seed(session):
    c = Classroom(name="太陽班", school_year=114, semester=2)
    session.add(c)
    session.flush()

    student = Student(
        student_id="S001",
        name="小花",
        lifecycle_status=LIFECYCLE_ACTIVE,
        classroom_id=c.id,
        enrollment_date=date(2026, 2, 1),
        allergy="花生",
        medication="氣喘吸入器",
        emergency_contact_name="阿嬤",
        emergency_contact_phone="0911-111-111",
    )
    session.add(student)
    session.flush()

    # Guardians
    g1 = Guardian(
        student_id=student.id,
        name="王爸爸",
        phone="0922-222-222",
        relation="父親",
        is_primary=True,
        can_pickup=True,
        sort_order=0,
    )
    g2 = Guardian(
        student_id=student.id,
        name="王媽媽",
        phone="0933-333-333",
        relation="母親",
        is_primary=False,
        is_emergency=True,
        can_pickup=True,
        sort_order=1,
    )
    g3_deleted = Guardian(
        student_id=student.id,
        name="前監護人",
        relation="其他",
        deleted_at=datetime(2026, 1, 1),
        sort_order=9,
    )
    session.add_all([g1, g2, g3_deleted])

    # Fee items + records
    fee_item = FeeItem(name="學費", amount=10000, period="114-2")
    session.add(fee_item)
    session.flush()
    r1 = StudentFeeRecord(
        student_id=student.id,
        student_name=student.name,
        classroom_name=c.name,
        fee_item_id=fee_item.id,
        fee_item_name=fee_item.name,
        amount_due=10000,
        amount_paid=6000,
        status="unpaid",
        period="114-2",
    )
    session.add(r1)

    # Attendance
    session.add_all(
        [
            StudentAttendance(
                student_id=student.id, date=date(2026, 3, 1), status="出席"
            ),
            StudentAttendance(
                student_id=student.id, date=date(2026, 3, 2), status="病假"
            ),
            StudentAttendance(
                student_id=student.id, date=date(2026, 3, 3), status="出席"
            ),
        ]
    )

    # Incidents
    session.add(
        StudentIncident(
            student_id=student.id,
            incident_type="行為觀察",
            severity="輕微",
            occurred_at=datetime(2026, 3, 2, 10, 0),
            description="與同學爭玩具",
            parent_notified=True,
            parent_notified_at=datetime(2026, 3, 2, 11, 0),
        )
    )

    # Change logs
    session.add_all(
        [
            StudentChangeLog(
                student_id=student.id,
                school_year=114,
                semester=2,
                event_type="入學",
                event_date=date(2026, 2, 1),
                classroom_id=c.id,
                reason="新生報名",
            ),
            StudentChangeLog(
                student_id=student.id,
                school_year=114,
                semester=2,
                event_type="休學",
                event_date=date(2026, 3, 10),
                classroom_id=c.id,
                reason="家庭因素",
            ),
        ]
    )
    session.commit()
    return {"student": student, "classroom": c}


class TestGuardians:
    def test_filters_deleted_and_sorts_primary_first(self, session, seed):
        guardians = get_guardians(session, seed["student"].id)
        assert len(guardians) == 2
        assert guardians[0]["is_primary"] is True
        assert guardians[0]["name"] == "王爸爸"
        assert guardians[1]["name"] == "王媽媽"


class TestAttendanceSummary:
    def test_counts_by_status(self, session, seed):
        summary = get_attendance_summary(
            session, seed["student"].id, date(2026, 3, 1), date(2026, 3, 31)
        )
        assert summary["total_records"] == 3
        assert summary["by_status"]["出席"] == 2
        assert summary["by_status"]["病假"] == 1

    def test_empty_window(self, session, seed):
        summary = get_attendance_summary(
            session, seed["student"].id, date(2027, 1, 1), date(2027, 1, 31)
        )
        assert summary["total_records"] == 0
        assert summary["by_status"] == {}


class TestFeeSummary:
    def test_outstanding_calculation(self, session, seed):
        summary = get_fee_summary(session, seed["student"].id, period="114-2")
        assert summary["total_due"] == 10000
        assert summary["total_paid"] == 6000
        assert summary["outstanding"] == 4000
        assert summary["item_count"] == 1

    def test_none_period_sums_all(self, session, seed):
        # 加一筆他期費用
        fi = FeeItem(name="才藝費", amount=2000, period="114-1")
        session.add(fi)
        session.flush()
        session.add(
            StudentFeeRecord(
                student_id=seed["student"].id,
                student_name=seed["student"].name,
                fee_item_id=fi.id,
                fee_item_name=fi.name,
                amount_due=2000,
                amount_paid=2000,
                status="paid",
                period="114-1",
            )
        )
        session.commit()
        summary = get_fee_summary(session, seed["student"].id, period=None)
        assert summary["item_count"] == 2
        assert summary["total_due"] == 12000
        assert summary["total_paid"] == 8000


class TestTimeline:
    def test_orders_newest_first(self, session, seed):
        rows = get_timeline(session, seed["student"].id)
        assert rows[0]["event_type"] == "休學"
        assert rows[1]["event_type"] == "入學"


class TestAssembleProfile:
    def test_missing_student_returns_none(self, session):
        assert assemble_profile(session, 99999) is None

    def test_full_profile_shape(self, session, seed):
        profile = assemble_profile(
            session,
            seed["student"].id,
            fee_period="114-2",
            attendance_window=(date(2026, 3, 1), date(2026, 3, 31)),
        )
        assert profile is not None
        assert set(profile.keys()) == {
            "basic",
            "lifecycle",
            "health",
            "guardians",
            "attendance_summary",
            "fee_summary",
            "incident_summary",
            "timeline",
        }
        assert profile["basic"]["classroom_name"] == "太陽班"
        assert profile["lifecycle"]["status"] == LIFECYCLE_ACTIVE
        assert profile["health"]["allergy"] == "花生"
        assert len(profile["guardians"]) == 2
        assert profile["fee_summary"]["outstanding"] == 4000
        assert profile["incident_summary"][0]["incident_type"] == "行為觀察"
        assert len(profile["timeline"]) == 2


class TestAttendanceWindowROC:
    def test_window_uses_ad_years_not_roc(self):
        """回歸：resolve_current_academic_term 回傳民國年，_default 必須 +1911。"""
        start, end = _default_attendance_window(today=date(2026, 3, 15))
        # 2026-03-15 屬學期 2（民國 114 學年下學期）→ 2026/02/01 ~ 2026/07/31
        assert start.year >= 2020, f"起始年應為西元，收到 {start}"
        assert end.year >= 2020
        assert start <= end
