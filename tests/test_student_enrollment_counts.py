"""薪資側在籍人數計算正確性測試（spec 2026-06-13-enrollment-count-correctness）。

L1a：退學/轉出（withdrawal_date）學生不得計入在籍人數。
L1b：班級人數須依轉班歷史（StudentClassroomTransfer）反查目標日所屬班級。
"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, ClassGrade, Classroom, Student
from models.student_transfer import StudentClassroomTransfer
from services.student_enrollment import (
    classroom_student_count_map,
    count_students_active_on,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "enrollment-counts.sqlite"
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


def _make_classroom(session, name):
    grade = ClassGrade(name=f"{name}年級", is_active=True)
    session.add(grade)
    session.flush()
    room = Classroom(name=name, grade_id=grade.id, is_active=True)
    session.add(room)
    session.flush()
    return room


def _make_student(session, sid, classroom, **kw):
    student = Student(
        student_id=sid,
        name=f"學生{sid}",
        classroom_id=classroom.id if classroom else None,
        enrollment_date=kw.pop("enrollment_date", date(2025, 9, 1)),
        is_active=kw.pop("is_active", True),
        **kw,
    )
    session.add(student)
    session.flush()
    return student


class TestWithdrawalDateExcluded:
    """L1a：withdrawal_date 必須讓學生自該日（含）起不在籍。"""

    def test_withdrawn_student_not_counted_after_withdrawal(self, db):
        with db() as session:
            room = _make_classroom(session, "天堂鳥")
            _make_student(
                session,
                "S001",
                room,
                withdrawal_date=date(2026, 3, 10),
                is_active=False,
            )

            # 退學前的月底：在籍
            assert count_students_active_on(session, date(2026, 2, 28)) == 1
            # 退學後的月底：不在籍（修正前因不看 withdrawal_date 會誤算 1）
            assert count_students_active_on(session, date(2026, 3, 31)) == 0
            assert classroom_student_count_map(session, date(2026, 3, 31)) == {}

    def test_withdrawal_day_itself_not_counted(self, db):
        """對齊 year_end _enrolled_on_filter：withdrawal_date > 基準日才算在籍。"""
        with db() as session:
            room = _make_classroom(session, "茉莉")
            _make_student(session, "S002", room, withdrawal_date=date(2026, 3, 31))
            assert count_students_active_on(session, date(2026, 3, 31)) == 0
            assert count_students_active_on(session, date(2026, 3, 30)) == 1

    def test_null_withdrawal_unchanged(self, db):
        with db() as session:
            room = _make_classroom(session, "牡丹")
            _make_student(session, "S003", room)
            assert count_students_active_on(session, date(2026, 3, 31)) == 1


class TestTransferHistoryAwareCounts:
    """L1b：classroom_student_count_map 依轉班歷史歸班。"""

    def test_count_before_transfer_uses_from_classroom(self, db):
        """4/15 從 A 轉到 B：3 月底應數在 A（修正前用現態 classroom_id 會數進 B）。"""
        with db() as session:
            room_a = _make_classroom(session, "A班")
            room_b = _make_classroom(session, "B班")
            student = _make_student(session, "S010", room_b)  # 現態已在 B
            session.add(
                StudentClassroomTransfer(
                    student_id=student.id,
                    from_classroom_id=room_a.id,
                    to_classroom_id=room_b.id,
                    transferred_at=datetime(2026, 4, 15, 10, 0),
                )
            )
            session.flush()

            assert classroom_student_count_map(session, date(2026, 3, 31)) == {
                room_a.id: 1
            }
            assert classroom_student_count_map(session, date(2026, 4, 30)) == {
                room_b.id: 1
            }

    def test_multiple_transfers_pick_latest_before_date(self, db):
        """A→B（2月）、B→C（5月）：4 月底在 B、5 月底在 C、1 月底在 A。"""
        with db() as session:
            room_a = _make_classroom(session, "甲班")
            room_b = _make_classroom(session, "乙班")
            room_c = _make_classroom(session, "丙班")
            student = _make_student(
                session, "S011", room_c, enrollment_date=date(2025, 9, 1)
            )
            session.add_all(
                [
                    StudentClassroomTransfer(
                        student_id=student.id,
                        from_classroom_id=room_a.id,
                        to_classroom_id=room_b.id,
                        transferred_at=datetime(2026, 2, 10, 9, 0),
                    ),
                    StudentClassroomTransfer(
                        student_id=student.id,
                        from_classroom_id=room_b.id,
                        to_classroom_id=room_c.id,
                        transferred_at=datetime(2026, 5, 3, 9, 0),
                    ),
                ]
            )
            session.flush()

            assert classroom_student_count_map(session, date(2026, 1, 31)) == {
                room_a.id: 1
            }
            assert classroom_student_count_map(session, date(2026, 4, 30)) == {
                room_b.id: 1
            }
            assert classroom_student_count_map(session, date(2026, 5, 31)) == {
                room_c.id: 1
            }

    def test_no_transfer_history_falls_back_to_current(self, db):
        with db() as session:
            room = _make_classroom(session, "丁班")
            _make_student(session, "S012", room)
            assert classroom_student_count_map(session, date(2026, 3, 31)) == {
                room.id: 1
            }
