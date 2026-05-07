"""Phase 2B: portal_dashboard_service batch overload 單元測試。

測試 5 個函式的 dispatch by input type（int / list[int]）。
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.classroom import LIFECYCLE_ACTIVE
from models.database import Base, Classroom, Student
from services.portal_dashboard_service import (
    compute_allergy_alerts,
    compute_consecutive_absences,
    compute_upcoming_birthdays,
    count_pending_medications,
    has_attendance_today,
)


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "dash_batch.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    sess = sf()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def two_classrooms(db_session):
    c1 = Classroom(name="c1", is_active=True)
    c2 = Classroom(name="c2", is_active=True)
    db_session.add_all([c1, c2])
    db_session.commit()
    # 加學生讓 has_attendance_today 回 False（不是 True-for-empty）
    for c in (c1, c2):
        s = Student(
            student_id=f"S_{c.id}",
            name=f"學生{c.id}",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        db_session.add(s)
    db_session.commit()
    return c1, c2


class TestBatchSignature:
    def test_consecutive_absences_int_returns_list(self, db_session, two_classrooms):
        c1, _ = two_classrooms
        result = compute_consecutive_absences(
            db_session, classroom_id=c1.id, today=date.today()
        )
        assert isinstance(result, list)

    def test_consecutive_absences_list_returns_dict(self, db_session, two_classrooms):
        c1, c2 = two_classrooms
        result = compute_consecutive_absences(
            db_session, classroom_id=[c1.id, c2.id], today=date.today()
        )
        assert isinstance(result, dict)
        assert set(result.keys()) == {c1.id, c2.id}

    def test_consecutive_absences_empty_list_returns_empty_dict(self, db_session):
        assert (
            compute_consecutive_absences(
                db_session, classroom_id=[], today=date.today()
            )
            == {}
        )

    def test_consecutive_absences_list_values_are_lists(
        self, db_session, two_classrooms
    ):
        c1, c2 = two_classrooms
        result = compute_consecutive_absences(
            db_session, classroom_id=[c1.id, c2.id], today=date.today()
        )
        assert all(isinstance(v, list) for v in result.values())

    def test_upcoming_birthdays_int_returns_list(self, db_session, two_classrooms):
        c1, _ = two_classrooms
        result = compute_upcoming_birthdays(
            db_session, classroom_id=c1.id, today=date.today()
        )
        assert isinstance(result, list)

    def test_upcoming_birthdays_list_returns_dict_of_lists(
        self, db_session, two_classrooms
    ):
        c1, c2 = two_classrooms
        result = compute_upcoming_birthdays(
            db_session, classroom_id=[c1.id, c2.id], today=date.today()
        )
        assert isinstance(result, dict)
        assert set(result.keys()) == {c1.id, c2.id}
        assert all(isinstance(v, list) for v in result.values())

    def test_upcoming_birthdays_empty_list_returns_empty_dict(self, db_session):
        assert (
            compute_upcoming_birthdays(db_session, classroom_id=[], today=date.today())
            == {}
        )

    def test_allergy_alerts_int_returns_list(self, db_session, two_classrooms):
        c1, _ = two_classrooms
        result = compute_allergy_alerts(db_session, classroom_id=c1.id)
        assert isinstance(result, list)

    def test_allergy_alerts_list_returns_dict_of_lists(
        self, db_session, two_classrooms
    ):
        c1, c2 = two_classrooms
        result = compute_allergy_alerts(db_session, classroom_id=[c1.id, c2.id])
        assert isinstance(result, dict)
        assert set(result.keys()) == {c1.id, c2.id}
        assert all(isinstance(v, list) for v in result.values())

    def test_allergy_alerts_empty_list_returns_empty_dict(self, db_session):
        assert compute_allergy_alerts(db_session, classroom_id=[]) == {}

    def test_pending_medications_int_returns_int(self, db_session, two_classrooms):
        c1, _ = two_classrooms
        result = count_pending_medications(
            db_session, classroom_id=c1.id, today=date.today()
        )
        assert isinstance(result, int)

    def test_pending_medications_list_returns_dict_of_ints(
        self, db_session, two_classrooms
    ):
        c1, c2 = two_classrooms
        result = count_pending_medications(
            db_session, classroom_id=[c1.id, c2.id], today=date.today()
        )
        assert isinstance(result, dict)
        assert set(result.keys()) == {c1.id, c2.id}
        assert all(isinstance(v, int) for v in result.values())

    def test_pending_medications_empty_list_returns_empty_dict(self, db_session):
        assert (
            count_pending_medications(db_session, classroom_id=[], today=date.today())
            == {}
        )

    def test_has_attendance_today_int_returns_bool(self, db_session, two_classrooms):
        c1, _ = two_classrooms
        result = has_attendance_today(
            db_session, classroom_id=c1.id, today=date.today()
        )
        assert isinstance(result, bool)

    def test_has_attendance_today_list_returns_dict_of_bools(
        self, db_session, two_classrooms
    ):
        c1, c2 = two_classrooms
        result = has_attendance_today(
            db_session, classroom_id=[c1.id, c2.id], today=date.today()
        )
        assert isinstance(result, dict)
        assert set(result.keys()) == {c1.id, c2.id}
        assert all(isinstance(v, bool) for v in result.values())

    def test_has_attendance_today_empty_list_returns_empty_dict(self, db_session):
        assert (
            has_attendance_today(db_session, classroom_id=[], today=date.today()) == {}
        )


from services.contact_book_service import compute_class_completion


class TestContactBookCompletionBatch:
    def test_int_returns_dict_with_roster_keys(self, db_session, two_classrooms):
        c1, _ = two_classrooms
        result = compute_class_completion(
            db_session, classroom_id=c1.id, log_date=date.today()
        )
        assert isinstance(result, dict)
        assert "roster" in result
        assert "draft" in result
        assert "published" in result

    def test_list_returns_dict_of_dicts(self, db_session, two_classrooms):
        c1, c2 = two_classrooms
        result = compute_class_completion(
            db_session, classroom_id=[c1.id, c2.id], log_date=date.today()
        )
        assert isinstance(result, dict)
        assert set(result.keys()) == {c1.id, c2.id}
        assert all(isinstance(v, dict) and "roster" in v for v in result.values())

    def test_empty_list_returns_empty_dict(self, db_session):
        result = compute_class_completion(
            db_session, classroom_id=[], log_date=date.today()
        )
        assert result == {}
