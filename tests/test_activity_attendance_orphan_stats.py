"""
tests/test_activity_attendance_orphan_stats.py
──────────────────────────────────────────────
驗證場次列表統計（build_session_rows_with_stats）與儀表板統計
（ActivityService.get_attendance_stats）不會將「孤兒點名」計入。

孤兒點名情境：
  (a) 學生報名後整筆軟刪（is_active=False），但 ActivityAttendance row 未刪。
      → build_session_rows_with_stats 的 recorded/present 應與詳情頁一致（孤兒不計入）。
  (b) 報名被駁回（is_active=False, match_status='rejected'），但仍有點名記錄。
      → get_attendance_stats 不計入孤兒；有效出席率不因孤兒膨脹。

2026-06-22 P2：統計口徑對齊修補回歸測試。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.activity import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    RegistrationCourse,
)
from models.classroom import Student  # noqa: F401 — ensure metadata loaded

from api.activity._shared import (
    _build_session_detail_response,
    build_session_rows_with_stats,
)
from services.activity_service import ActivityService

TERM = {"school_year": 114, "semester": 1}


def _query_session_rows(session, course_id):
    """模擬 attendance.py list_sessions 的查詢，帶 course_name label，
    供 build_session_rows_with_stats 使用。"""
    return (
        session.query(
            ActivitySession.id,
            ActivitySession.course_id,
            ActivitySession.session_date,
            ActivitySession.notes,
            ActivitySession.created_by,
            ActivitySession.created_at,
            ActivityCourse.name.label("course_name"),
        )
        .join(ActivityCourse, ActivitySession.course_id == ActivityCourse.id)
        .filter(ActivitySession.course_id == course_id)
        .all()
    )


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def svc():
    return ActivityService()


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_course(s, name="圍棋", **kwargs) -> ActivityCourse:
    c = ActivityCourse(
        name=name,
        price=1000,
        capacity=30,
        is_active=True,
        school_year=TERM["school_year"],
        semester=TERM["semester"],
        **kwargs,
    )
    s.add(c)
    s.flush()
    return c


def _make_session(s, course_id) -> ActivitySession:
    sess = ActivitySession(
        course_id=course_id, session_date=date.today(), created_by="test"
    )
    s.add(sess)
    s.flush()
    return sess


def _make_reg(
    s,
    *,
    name="王小明",
    is_active=True,
    match_status="matched",
) -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        is_active=is_active,
        match_status=match_status,
    )
    s.add(r)
    s.flush()
    return r


def _enroll(s, reg_id: int, course_id: int, status: str = "enrolled"):
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course_id,
        status=status,
        price_snapshot=1000,
    )
    s.add(rc)
    s.flush()
    return rc


def _attend(
    s, session_id: int, reg_id: int, is_present: bool = True
) -> ActivityAttendance:
    a = ActivityAttendance(
        session_id=session_id,
        registration_id=reg_id,
        is_present=is_present,
        notes="",
        recorded_by="test",
    )
    s.add(a)
    s.flush()
    return a


# ── (a) build_session_rows_with_stats 對齊詳情頁 ─────────────────────────────


class TestBuildSessionRowsOrphan:
    """build_session_rows_with_stats 不計入孤兒點名，與 _build_session_detail_response 一致。"""

    def test_soft_deleted_registration_excluded_from_list_stats(self, session):
        """整筆軟刪（is_active=False）的報名點名不計入列表統計。

        修前：build_session_rows_with_stats 只 COUNT ActivityAttendance，孤兒被計入。
        修後：列表 present_count == 詳情頁 present_count == 0（孤兒排除）。
        """
        course = _make_course(session)
        sess = _make_session(session, course.id)

        # 建一筆正常報名並點名出席
        reg_active = _make_reg(session, name="在籍生")
        _enroll(session, reg_active.id, course.id)
        _attend(session, sess.id, reg_active.id, is_present=True)

        # 建一筆軟刪報名並留下孤兒點名
        reg_deleted = _make_reg(session, name="已刪除生", is_active=False)
        _enroll(session, reg_deleted.id, course.id)
        _attend(session, sess.id, reg_deleted.id, is_present=True)

        session.commit()

        # 列表統計（模擬 list_sessions 的帶 course_name JOIN 查詢）
        rows = _query_session_rows(session, course.id)
        list_stats = build_session_rows_with_stats(session, rows)
        assert len(list_stats) == 1
        row = list_stats[0]

        # 詳情頁統計
        detail = _build_session_detail_response(session, sess)

        # 兩者 present_count 必須一致（孤兒不算）
        assert row["present_count"] == detail["present_count"], (
            f"list present_count={row['present_count']} != "
            f"detail present_count={detail['present_count']}; 孤兒點名被計入列表"
        )
        # 且值正確（只有 1 筆有效出席）
        assert detail["present_count"] == 1
        assert row["present_count"] == 1

        # recorded_count 同樣應一致（只計有效報名的點名）
        assert row["recorded_count"] == detail["total"], (
            f"list recorded_count={row['recorded_count']} != "
            f"detail total={detail['total']}"
        )

    def test_rejected_registration_excluded_from_list_stats(self, session):
        """被駁回（match_status='rejected', is_active=False）的報名點名不計入列表統計。"""
        course = _make_course(session)
        sess = _make_session(session, course.id)

        # 正常報名並點名
        reg_ok = _make_reg(session, name="正常生")
        _enroll(session, reg_ok.id, course.id)
        _attend(session, sess.id, reg_ok.id, is_present=True)

        # 被駁回報名留下孤兒點名
        reg_rejected = _make_reg(
            session, name="被駁生", is_active=False, match_status="rejected"
        )
        _enroll(session, reg_rejected.id, course.id, status="enrolled")
        _attend(session, sess.id, reg_rejected.id, is_present=True)

        session.commit()

        rows = _query_session_rows(session, course.id)
        list_stats = build_session_rows_with_stats(session, rows)
        row = list_stats[0]
        detail = _build_session_detail_response(session, sess)

        assert row["present_count"] == detail["present_count"]
        assert row["present_count"] == 1  # 只有正常生


# ── (b) get_attendance_stats 不計孤兒 ────────────────────────────────────────


class TestGetAttendanceStatsOrphan:
    """ActivityService.get_attendance_stats 儀表板出席率不含孤兒點名。"""

    def test_rejected_orphan_excluded_from_dashboard_stats(self, session, svc):
        """被駁回報名的孤兒點名不應計入 get_attendance_stats total/present。

        修前：get_attendance_stats JOIN path CourseSession→Attendance 沒驗報名有效性，
              孤兒被算進 total → 出席率膨脹（或壓低）。
        修後：total/present 只算有效報名（is_active=True、match_status!='rejected'、
              RegistrationCourse.status='enrolled'）。
        """
        course = _make_course(session)
        sess = _make_session(session, course.id)

        # 1 筆有效報名，出席
        reg_ok = _make_reg(session, name="有效生")
        _enroll(session, reg_ok.id, course.id)
        _attend(session, sess.id, reg_ok.id, is_present=True)

        # 1 筆駁回孤兒，出席（is_active=False, match_status='rejected'）
        reg_rej = _make_reg(
            session, name="駁回生", is_active=False, match_status="rejected"
        )
        _enroll(session, reg_rej.id, course.id, status="enrolled")
        _attend(session, sess.id, reg_rej.id, is_present=False)  # 甚至缺席孤兒

        session.commit()

        result = svc.get_attendance_stats(session, **TERM)

        # 只有 1 筆有效點名（reg_ok, is_present=True）
        by_course = result["by_course"]
        assert len(by_course) == 1
        entry = by_course[0]

        # avg_rate 應為 1.0（1 出席 / 1 有效），而非 0.5（1/2 含孤兒）
        assert (
            entry["avg_rate"] == 1.0
        ), f"avg_rate={entry['avg_rate']} != 1.0；孤兒點名被計入儀表板統計"

    def test_soft_deleted_orphan_excluded_from_dashboard_stats(self, session, svc):
        """整筆軟刪（is_active=False）的孤兒點名不計入儀表板出席統計。"""
        course = _make_course(session)
        sess = _make_session(session, course.id)

        # 1 筆有效報名，缺席
        reg_ok = _make_reg(session, name="有效生")
        _enroll(session, reg_ok.id, course.id)
        _attend(session, sess.id, reg_ok.id, is_present=False)

        # 1 筆軟刪孤兒，出席（不應計入）
        reg_del = _make_reg(session, name="刪除生", is_active=False)
        _enroll(session, reg_del.id, course.id, status="enrolled")
        _attend(session, sess.id, reg_del.id, is_present=True)

        session.commit()

        result = svc.get_attendance_stats(session, **TERM)
        by_course = result["by_course"]
        assert len(by_course) == 1
        entry = by_course[0]

        # avg_rate 應為 0.0（1 缺席 / 1 有效），不受孤兒出席影響
        assert (
            entry["avg_rate"] == 0.0
        ), f"avg_rate={entry['avg_rate']} != 0.0；孤兒出席點名被計入儀表板統計"
