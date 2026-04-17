"""才藝點名「按班級分組 + student_id 冗餘寫入」整合測試。"""

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
from models.classroom import Classroom, Student  # noqa: F401 metadata

from api.activity._shared import _build_session_detail_response


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


def _make_classroom(s, name, active=True):
    c = Classroom(name=name, is_active=active)
    s.add(c)
    s.flush()
    return c


def _make_course(s, name="圍棋"):
    c = ActivityCourse(name=name, price=1000, capacity=30, is_active=True)
    s.add(c)
    s.flush()
    return c


def _make_session(s, course_id):
    sess = ActivitySession(
        course_id=course_id, session_date=date.today(), created_by="test"
    )
    s.add(sess)
    s.flush()
    return sess


def _make_registration(
    s,
    *,
    name,
    class_name,
    classroom_id=None,
    student_id=None,
    parent_phone="0912345678",
):
    r = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name=class_name,
        classroom_id=classroom_id,
        student_id=student_id,
        parent_phone=parent_phone,
        is_active=True,
    )
    s.add(r)
    s.flush()
    return r


def _enroll(s, reg_id, course_id):
    s.add(
        RegistrationCourse(
            registration_id=reg_id,
            course_id=course_id,
            status="enrolled",
            price_snapshot=1000,
        )
    )
    s.flush()


class TestGroupByClassroom:
    def test_session_detail_group_by_classroom_returns_groups(self, session):
        course = _make_course(session)
        c1 = _make_classroom(session, "大象班")
        c2 = _make_classroom(session, "長頸鹿班")
        r1 = _make_registration(
            session, name="A", class_name="大象班", classroom_id=c1.id
        )
        r2 = _make_registration(
            session, name="B", class_name="大象班", classroom_id=c1.id
        )
        r3 = _make_registration(
            session, name="C", class_name="長頸鹿班", classroom_id=c2.id
        )
        for r in (r1, r2, r3):
            _enroll(session, r.id, course.id)
        sess = _make_session(session, course.id)
        session.commit()

        result = _build_session_detail_response(session, sess, group_by="classroom")

        assert "groups" in result
        names = [g["classroom_name"] for g in result["groups"]]
        # 按班級名排序
        assert names == ["大象班", "長頸鹿班"]
        sizes = [len(g["students"]) for g in result["groups"]]
        assert sizes == [2, 1]

    def test_unclassified_group_for_registrations_without_classroom(self, session):
        course = _make_course(session)
        c1 = _make_classroom(session, "大象班")
        r_in = _make_registration(
            session, name="在校生", class_name="大象班", classroom_id=c1.id
        )
        r_out = _make_registration(
            session, name="未分班", class_name="家長亂填", classroom_id=None
        )
        _enroll(session, r_in.id, course.id)
        _enroll(session, r_out.id, course.id)
        sess = _make_session(session, course.id)
        session.commit()

        result = _build_session_detail_response(session, sess, group_by="classroom")

        groups = result["groups"]
        # 未分班應排在末尾
        assert groups[-1]["classroom_name"] == "未分班"
        assert groups[-1]["classroom_id"] is None
        assert len(groups[-1]["students"]) == 1

    def test_no_group_by_returns_flat_students_only(self, session):
        course = _make_course(session)
        c1 = _make_classroom(session, "大象班")
        r = _make_registration(
            session, name="A", class_name="大象班", classroom_id=c1.id
        )
        _enroll(session, r.id, course.id)
        sess = _make_session(session, course.id)
        session.commit()

        result = _build_session_detail_response(session, sess)
        assert "groups" not in result
        assert len(result["students"]) == 1

    def test_group_by_uses_real_classroom_name_over_snapshot(self, session):
        """class_name 字串與 Classroom.name 不一致時，groups 用 Classroom.name。"""
        course = _make_course(session)
        c1 = _make_classroom(session, "大象班")
        _make_registration(
            session,
            name="A",
            class_name="舊快照名",  # 與 Classroom.name 不同
            classroom_id=c1.id,
        )
        # 需要 enroll 才會出現在 session detail
        last_reg = (
            session.query(ActivityRegistration)
            .order_by(ActivityRegistration.id.desc())
            .first()
        )
        _enroll(session, last_reg.id, course.id)
        sess = _make_session(session, course.id)
        session.commit()

        result = _build_session_detail_response(session, sess, group_by="classroom")

        assert len(result["groups"]) == 1
        assert result["groups"][0]["classroom_name"] == "大象班"


class TestAttendanceStudentIdRedundancy:
    def test_session_detail_exposes_student_id_and_classroom_id(self, session):
        course = _make_course(session)
        c1 = _make_classroom(session, "大象班")
        # 建立一個 fake student fixture id；不強求 Student 實體存在，student_id 只是冗餘
        r = _make_registration(
            session,
            name="A",
            class_name="大象班",
            classroom_id=c1.id,
            student_id=42,
        )
        _enroll(session, r.id, course.id)
        sess = _make_session(session, course.id)
        session.commit()

        result = _build_session_detail_response(session, sess)
        s = result["students"][0]
        assert s["student_id"] == 42
        assert s["classroom_id"] == c1.id
