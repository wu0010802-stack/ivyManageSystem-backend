"""發放月結算前強制快照確認 gate（決策2，spec 2026-06-13）。

unconfirmed_distribution_months：回傳發放月結算所需、但「尚未產生或尚未確認」
的涵蓋月清單。非發放月回 []（不設限）；清單非空時 calculate 端點 raise 422。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, ClassGrade, Classroom, Student
from models.enrollment_snapshot import ClassEnrollmentSnapshot
from services.salary.enrollment_snapshot import (
    generate_snapshot,
    unconfirmed_distribution_months,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "snapshot-gate.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(db_engine)

    yield session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed_one_class(session):
    grade = ClassGrade(name="大班", is_active=True)
    session.add(grade)
    session.flush()
    room = Classroom(name="天堂鳥", grade_id=grade.id, is_active=True)
    session.add(room)
    session.flush()
    session.add(
        Student(
            student_id="G001",
            name="學生1",
            classroom_id=room.id,
            enrollment_date=date(2025, 9, 1),
            is_active=True,
        )
    )
    session.flush()
    return room


def _confirm_all(session, year, month):
    for row in (
        session.query(ClassEnrollmentSnapshot)
        .filter_by(snapshot_year=year, snapshot_month=month)
        .all()
    ):
        row.is_confirmed = True
    session.flush()


class TestUnconfirmedDistributionMonths:
    def test_non_distribution_month_returns_empty(self, db):
        with db() as session:
            _seed_one_class(session)
            # 5 月非發放月 → 不設限
            assert unconfirmed_distribution_months(session, 2026, 5) == []

    def test_distribution_month_no_snapshot_returns_all_covered(self, db):
        with db() as session:
            _seed_one_class(session)
            # 6 月發放，涵蓋 2~5 月，皆無快照 → 全部待辦
            result = unconfirmed_distribution_months(session, 2026, 6)
            assert result == [(2026, 2), (2026, 3), (2026, 4), (2026, 5)]

    def test_distribution_month_partial_confirmed(self, db):
        with db() as session:
            _seed_one_class(session)
            # 產生並確認 2、3 月；4、5 月仍缺
            for m in (2, 3):
                generate_snapshot(session, 2026, m, updated_by="t")
                _confirm_all(session, 2026, m)
            # 4 月產生但未確認
            generate_snapshot(session, 2026, 4, updated_by="t")
            result = unconfirmed_distribution_months(session, 2026, 6)
            assert result == [(2026, 4), (2026, 5)]

    def test_distribution_month_all_confirmed_returns_empty(self, db):
        with db() as session:
            _seed_one_class(session)
            for m in (2, 3, 4, 5):
                generate_snapshot(session, 2026, m, updated_by="t")
                _confirm_all(session, 2026, m)
            assert unconfirmed_distribution_months(session, 2026, 6) == []

    def test_february_covers_prev_december(self, db):
        with db() as session:
            _seed_one_class(session)
            # 2 月發放涵蓋去年 12 + 當年 1 月
            result = unconfirmed_distribution_months(session, 2026, 2)
            assert result == [(2025, 12), (2026, 1)]
