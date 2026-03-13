"""
tests/test_activity_stats.py — ActivityService 統計邏輯單元測試。

使用 SQLite in-memory 資料庫，不依賴 PostgreSQL，
測試 get_stats() 的數字聚合是否符合預期。
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySupply,
    ParentInquiry,
    RegistrationCourse,
    RegistrationSupply,
)
from services.activity_service import ActivityService


@pytest.fixture
def session():
    """提供 SQLite in-memory session，每個測試獨立建立與銷毀。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def svc():
    return ActivityService()


def _add_course(session, name="美術", price=1000, capacity=30) -> ActivityCourse:
    c = ActivityCourse(name=name, price=price, capacity=capacity)
    session.add(c)
    session.flush()
    return c


def _add_reg(session, student_name="王小明", class_name="大班", is_paid=False, is_active=True) -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name=class_name,
        is_paid=is_paid,
        is_active=is_active,
    )
    session.add(r)
    session.flush()
    return r


def _enroll(session, reg_id: int, course_id: int, price: int = 1000, status: str = "enrolled"):
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course_id,
        status=status,
        price_snapshot=price,
    )
    session.add(rc)
    session.flush()
    return rc


class TestGetStatsBasic:
    def test_empty_db_returns_zeros(self, session, svc):
        """無任何資料時統計全為 0。"""
        result = svc.get_stats(session)
        stats = result["statistics"]
        assert stats["totalRegistrations"] == 0
        assert stats["totalEnrollments"] == 0
        assert stats["totalWaitlist"] == 0
        assert stats["totalRevenue"] == 0
        assert stats["totalUnpaid"] == 0
        assert stats["enrollmentRate"] == 0.0
        assert stats["unreadInquiries"] == 0
        assert result["charts"]["daily"] == []
        assert result["charts"]["topCourses"] == []

    def test_get_stats_summary_keeps_legacy_statistics_shape(self, session, svc):
        """summary API 維持與舊 statistics 相同欄位。"""
        course = _add_course(session, price=1500)
        reg = _add_reg(session, is_paid=True)
        _enroll(session, reg.id, course.id, price=1500)
        session.add(ParentInquiry(name="家長甲", phone="0912", question="Q1", is_read=False))
        session.commit()

        summary = svc.get_stats_summary(session)
        assert set(summary) == {
            "totalRegistrations",
            "totalEnrollments",
            "totalWaitlist",
            "totalSupplyOrders",
            "todayNewRegistrations",
            "totalRevenue",
            "totalUnpaid",
            "enrollmentRate",
            "unreadInquiries",
        }
        assert summary["totalRegistrations"] == 1
        assert summary["unreadInquiries"] == 1

    def test_single_paid_enrollment_counts_revenue(self, session, svc):
        """一筆已繳費報名，totalRevenue 應等於課程快照價。"""
        course = _add_course(session, price=1500)
        reg = _add_reg(session, is_paid=True)
        _enroll(session, reg.id, course.id, price=1500)
        session.commit()

        stats = svc.get_stats(session)["statistics"]
        assert stats["totalRegistrations"] == 1
        assert stats["totalEnrollments"] == 1
        assert stats["totalRevenue"] == 1500
        assert stats["totalUnpaid"] == 0

    def test_unpaid_enrollment_goes_to_unpaid(self, session, svc):
        """未繳費報名計入 totalUnpaid，不計入 totalRevenue。"""
        course = _add_course(session, price=2000)
        reg = _add_reg(session, is_paid=False)
        _enroll(session, reg.id, course.id, price=2000)
        session.commit()

        stats = svc.get_stats(session)["statistics"]
        assert stats["totalRevenue"] == 0
        assert stats["totalUnpaid"] == 2000

    def test_waitlist_counted_separately(self, session, svc):
        """候補狀態計入 totalWaitlist，不計入 totalEnrollments。"""
        course = _add_course(session)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="waitlist")
        session.commit()

        stats = svc.get_stats(session)["statistics"]
        assert stats["totalWaitlist"] == 1
        assert stats["totalEnrollments"] == 0

    def test_soft_deleted_registration_excluded(self, session, svc):
        """is_active=False 的報名不計入統計。"""
        course = _add_course(session)
        reg = _add_reg(session, is_active=False)
        _enroll(session, reg.id, course.id)
        session.commit()

        stats = svc.get_stats(session)["statistics"]
        assert stats["totalRegistrations"] == 0
        assert stats["totalEnrollments"] == 0


class TestGetStatsEnrollmentRate:
    def test_enrollment_rate_calculation(self, session, svc):
        """報名率 = enrolled 數 / 課程容量合計 × 100。"""
        c1 = _add_course(session, name="美術", capacity=10)
        c2 = _add_course(session, name="音樂", capacity=10)

        for i in range(4):
            reg = _add_reg(session, student_name=f"學生{i}")
            _enroll(session, reg.id, c1.id)

        session.commit()

        stats = svc.get_stats(session)["statistics"]
        # 4 enrolled / (10+10) capacity = 20%
        assert stats["enrollmentRate"] == 20.0

    def test_full_enrollment_rate_is_100(self, session, svc):
        """所有名額都報滿，報名率應為 100.0。"""
        course = _add_course(session, name="舞蹈", capacity=2)
        for i in range(2):
            reg = _add_reg(session, student_name=f"滿額{i}")
            _enroll(session, reg.id, course.id)
        session.commit()

        stats = svc.get_stats(session)["statistics"]
        assert stats["enrollmentRate"] == 100.0


class TestGetStatsTopCourses:
    def test_top_courses_ordered_by_enrollment(self, session, svc):
        """熱門課程依報名數倒序排列。"""
        c_art = _add_course(session, name="美術", price=800)
        c_music = _add_course(session, name="音樂", price=900)
        c_dance = _add_course(session, name="舞蹈", price=1000)

        # 美術 3 筆，音樂 2 筆，舞蹈 1 筆
        for i in range(3):
            reg = _add_reg(session, student_name=f"美術生{i}")
            _enroll(session, reg.id, c_art.id, price=800)
        for i in range(2):
            reg = _add_reg(session, student_name=f"音樂生{i}")
            _enroll(session, reg.id, c_music.id, price=900)
        reg = _add_reg(session, student_name="舞蹈生0")
        _enroll(session, reg.id, c_dance.id, price=1000)

        session.commit()

        result = svc.get_stats(session)
        top = result["charts"]["topCourses"]

        assert top[0]["name"] == "美術"
        assert top[0]["count"] == 3
        assert top[1]["name"] == "音樂"
        assert top[1]["count"] == 2
        assert top[2]["name"] == "舞蹈"
        assert top[2]["count"] == 1

    def test_top_courses_limited_to_5(self, session, svc):
        """熱門課程最多回傳 5 筆。"""
        for i in range(7):
            c = _add_course(session, name=f"課程{i}", price=500 + i)
            reg = _add_reg(session, student_name=f"學生{i}")
            _enroll(session, reg.id, c.id)

        session.commit()
        top = svc.get_stats(session)["charts"]["topCourses"]
        assert len(top) <= 5

    def test_daily_chart_is_limited_to_recent_30_days(self, session, svc):
        """每日趨勢只保留最近 30 天，避免載入全歷史資料。"""
        course = _add_course(session)
        start_date = datetime.combine(date.today() - timedelta(days=34), datetime.min.time())

        for i in range(35):
            reg = ActivityRegistration(
                student_name=f"學生{i}",
                birthday="2020-01-01",
                class_name="大班",
                is_paid=False,
                is_active=True,
            )
            session.add(reg)
            session.flush()
            reg.created_at = start_date + timedelta(days=i)
            _enroll(session, reg.id, course.id)

        session.commit()

        charts = svc.get_stats_charts(session)
        assert len(charts["daily"]) == 30
        assert charts["daily"][0]["date"] < charts["daily"][-1]["date"]


class TestGetStatsUnreadInquiries:
    def test_counts_only_unread(self, session, svc):
        """unreadInquiries 只計算 is_read=False 的提問。"""
        session.add(ParentInquiry(name="家長甲", phone="0912", question="Q1", is_read=False))
        session.add(ParentInquiry(name="家長乙", phone="0913", question="Q2", is_read=True))
        session.add(ParentInquiry(name="家長丙", phone="0914", question="Q3", is_read=False))
        session.commit()

        stats = svc.get_stats(session)["statistics"]
        assert stats["unreadInquiries"] == 2


class TestGetStatsQueryCount:
    def test_summary_aggregate_executes_single_sql_statement(self, session, svc):
        """summary 聚合應收斂為單一 SQL。"""
        statements = []

        def record_statement(*_args):
            statements.append(1)

        event.listen(session.bind, "before_cursor_execute", record_statement)
        try:
            svc._compute_stats_summary(session)
        finally:
            event.remove(session.bind, "before_cursor_execute", record_statement)

        assert len(statements) == 1

    def test_chart_aggregates_execute_two_sql_statements(self, session, svc):
        """charts 聚合應只用 daily/top courses 兩段 SQL。"""
        statements = []

        def record_statement(*_args):
            statements.append(1)

        event.listen(session.bind, "before_cursor_execute", record_statement)
        try:
            svc._compute_stats_charts(session)
        finally:
            event.remove(session.bind, "before_cursor_execute", record_statement)

        assert len(statements) == 2


class TestGetStatsMixedRevenue:
    def test_supply_revenue_included_in_paid_total(self, session, svc):
        """已繳費的用品也應計入 totalRevenue。"""
        course = _add_course(session, price=1000)
        supply = ActivitySupply(name="圍裙", price=200)
        session.add(supply)
        session.flush()

        reg = _add_reg(session, is_paid=True)
        _enroll(session, reg.id, course.id, price=1000)
        rs = RegistrationSupply(
            registration_id=reg.id,
            supply_id=supply.id,
            price_snapshot=200,
        )
        session.add(rs)
        session.commit()

        stats = svc.get_stats(session)["statistics"]
        assert stats["totalRevenue"] == 1200
        assert stats["totalUnpaid"] == 0
