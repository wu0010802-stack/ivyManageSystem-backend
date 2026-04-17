"""
tests/test_activity_student_match.py — 才藝三欄比對與電話正規化單元測試。

不依賴 FastAPI，直接對 _shared 的純函式做驗證。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Classroom, Student  # noqa: F401  — 確保 metadata
from api.activity._shared import (
    _normalize_phone,
    _match_student_with_parent_phone,
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


def _make_classroom(session, name="大象班") -> Classroom:
    c = Classroom(name=name, is_active=True)
    session.add(c)
    session.flush()
    return c


def _make_student(
    session,
    *,
    student_id="S001",
    name="王小明",
    birthday=date(2020, 5, 10),
    classroom_id=None,
    parent_phone="0912345678",
    emergency_contact_phone=None,
    is_active=True,
) -> Student:
    s = Student(
        student_id=student_id,
        name=name,
        birthday=birthday,
        classroom_id=classroom_id,
        parent_phone=parent_phone,
        emergency_contact_phone=emergency_contact_phone,
        is_active=is_active,
    )
    session.add(s)
    session.flush()
    return s


class TestNormalizePhone:
    def test_strips_dashes_spaces_parens(self):
        assert _normalize_phone(" 0912-345-678 ") == "0912345678"
        assert _normalize_phone("(02) 1234-5678") == "0212345678"

    def test_returns_none_on_empty(self):
        assert _normalize_phone("") is None
        assert _normalize_phone(None) is None
        assert _normalize_phone("   ") is None


class TestMatchStudentWithParentPhone:
    def test_match_unique_by_three_fields(self, session):
        classroom = _make_classroom(session)
        stu = _make_student(session, classroom_id=classroom.id)
        sid, cid = _match_student_with_parent_phone(
            session, "王小明", "2020-05-10", "0912345678"
        )
        assert sid == stu.id
        assert cid == classroom.id

    def test_match_uses_emergency_contact_phone_fallback(self, session):
        classroom = _make_classroom(session)
        stu = _make_student(
            session,
            classroom_id=classroom.id,
            parent_phone="0911111111",
            emergency_contact_phone="0922222222",
        )
        sid, cid = _match_student_with_parent_phone(
            session, "王小明", "2020-05-10", "0922-222-222"
        )
        assert sid == stu.id
        assert cid == classroom.id

    def test_phone_normalization_tolerates_dashes(self, session):
        classroom = _make_classroom(session)
        _make_student(
            session,
            classroom_id=classroom.id,
            parent_phone="0912-345-678",
        )
        sid, cid = _match_student_with_parent_phone(
            session, "王小明", "2020-05-10", "0912345678"
        )
        assert sid is not None
        assert cid == classroom.id

    def test_returns_none_when_phone_mismatch(self, session):
        classroom = _make_classroom(session)
        _make_student(session, classroom_id=classroom.id, parent_phone="0911111111")
        sid, cid = _match_student_with_parent_phone(
            session, "王小明", "2020-05-10", "0999999999"
        )
        assert sid is None and cid is None

    def test_returns_none_when_name_mismatch(self, session):
        classroom = _make_classroom(session)
        _make_student(session, classroom_id=classroom.id)
        sid, cid = _match_student_with_parent_phone(
            session, "王大明", "2020-05-10", "0912345678"
        )
        assert sid is None and cid is None

    def test_returns_none_when_inactive(self, session):
        classroom = _make_classroom(session)
        _make_student(session, classroom_id=classroom.id, is_active=False)
        sid, cid = _match_student_with_parent_phone(
            session, "王小明", "2020-05-10", "0912345678"
        )
        assert sid is None and cid is None

    def test_returns_none_when_ambiguous_two_hits(self, session):
        classroom = _make_classroom(session)
        _make_student(
            session,
            student_id="S001",
            classroom_id=classroom.id,
            parent_phone="0912345678",
        )
        _make_student(
            session,
            student_id="S002",
            classroom_id=classroom.id,
            parent_phone="0912345678",
        )
        sid, cid = _match_student_with_parent_phone(
            session, "王小明", "2020-05-10", "0912345678"
        )
        assert sid is None and cid is None

    def test_returns_none_on_bad_birthday(self, session):
        classroom = _make_classroom(session)
        _make_student(session, classroom_id=classroom.id)
        sid, cid = _match_student_with_parent_phone(
            session, "王小明", "not-a-date", "0912345678"
        )
        assert sid is None and cid is None

    def test_returns_none_when_phone_empty(self, session):
        classroom = _make_classroom(session)
        _make_student(session, classroom_id=classroom.id)
        sid, cid = _match_student_with_parent_phone(session, "王小明", "2020-05-10", "")
        assert sid is None and cid is None
