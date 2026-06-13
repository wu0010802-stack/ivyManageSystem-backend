"""月度在籍人數快照（L2，spec 2026-06-13-enrollment-count-correctness）。

結算用人數「看得到、改得了、鎖得住」：產生快照 → HR 檢視/手調 → 薪資計算
讀快照；無快照 fallback 即時計算（零漂移）。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, ClassGrade, Classroom, Employee, Student
from services.salary_engine import SalaryEngine


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "enrollment-snapshot.sqlite"
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


def _seed_class(session, name="天堂鳥", enrollment=12, with_teacher=False):
    grade = ClassGrade(name="大班", is_active=True)
    session.add(grade)
    session.flush()
    room = Classroom(name=name, grade_id=grade.id, is_active=True)
    session.add(room)
    session.flush()
    teacher = None
    if with_teacher:
        teacher = Employee(
            employee_id="T800",
            name="快照老師",
            title="幼兒園教師",
            position="幼兒園教師",
            employee_type="regular",
            base_salary=30000,
            hire_date=date(2025, 1, 1),
            is_active=True,
        )
        session.add(teacher)
        session.flush()
        room.head_teacher_id = teacher.id
        teacher.classroom_id = room.id
    for idx in range(enrollment):
        session.add(
            Student(
                student_id=f"SN{idx:03d}",
                name=f"快照學生{idx}",
                classroom_id=room.id,
                enrollment_date=date(2025, 9, 1),
                is_active=True,
            )
        )
    session.flush()
    return room, teacher


class TestGenerateAndResolve:
    def test_generate_creates_class_and_school_rows(self, db):
        from services.salary.enrollment_snapshot import (
            generate_snapshot,
            get_snapshot_counts,
        )

        with db() as session:
            room, _ = _seed_class(session, enrollment=12)
            result = generate_snapshot(session, 2026, 3, updated_by="tester")
            session.flush()

            counts = get_snapshot_counts(session, 2026, 3)
            assert counts is not None
            assert counts["school"] == 12
            assert counts["classes"] == {room.id: 12}
            assert result["generated"] >= 2  # 班級列 + 全校列

    def test_get_snapshot_counts_none_when_empty(self, db):
        from services.salary.enrollment_snapshot import get_snapshot_counts

        with db() as session:
            assert get_snapshot_counts(session, 2026, 3) is None

    def test_resolve_prefers_snapshot_and_falls_back_to_live(self, db):
        from services.salary.enrollment_snapshot import (
            generate_snapshot,
            resolve_bonus_counts,
        )

        with db() as session:
            room, _ = _seed_class(session, enrollment=12)

            # 無快照 → 即時計算（零漂移）
            school, classes = resolve_bonus_counts(session, 2026, 3)
            assert school == 12
            assert classes == {room.id: 12}

            # 產快照後手調 → resolver 以快照為準
            generate_snapshot(session, 2026, 3, updated_by="tester")
            session.flush()
            from models.enrollment_snapshot import ClassEnrollmentSnapshot

            row = (
                session.query(ClassEnrollmentSnapshot)
                .filter(
                    ClassEnrollmentSnapshot.snapshot_year == 2026,
                    ClassEnrollmentSnapshot.snapshot_month == 3,
                    ClassEnrollmentSnapshot.classroom_id == room.id,
                )
                .one()
            )
            row.student_count = 10
            session.flush()

            school2, classes2 = resolve_bonus_counts(session, 2026, 3)
            assert classes2 == {room.id: 10}

    def test_regenerate_updates_unconfirmed_keeps_confirmed(self, db):
        from models.enrollment_snapshot import ClassEnrollmentSnapshot
        from services.salary.enrollment_snapshot import generate_snapshot

        with db() as session:
            room, _ = _seed_class(session, enrollment=12)
            generate_snapshot(session, 2026, 3, updated_by="tester")
            session.flush()

            # 確認班級列並手調為 11
            row = (
                session.query(ClassEnrollmentSnapshot)
                .filter(ClassEnrollmentSnapshot.classroom_id == room.id)
                .one()
            )
            row.student_count = 11
            row.is_confirmed = True
            session.flush()

            # 新增一位學生後重產：未確認列（全校）更新、已確認列保留手調值
            session.add(
                Student(
                    student_id="SN999",
                    name="新生",
                    classroom_id=room.id,
                    enrollment_date=date(2026, 3, 1),
                    is_active=True,
                )
            )
            session.flush()
            generate_snapshot(session, 2026, 3, updated_by="tester")
            session.flush()

            session.refresh(row)
            assert float(row.student_count) == 11  # 已確認，不被覆寫
            school_row = (
                session.query(ClassEnrollmentSnapshot)
                .filter(ClassEnrollmentSnapshot.classroom_id.is_(None))
                .one()
            )
            assert float(school_row.student_count) == 13  # 未確認，跟著重產


class TestEngineUsesSnapshot:
    def test_festival_breakdown_uses_snapshot_count(self, db):
        """快照手調 12→10 後，節慶獎金按 10 人計（基數 2000、目標 12 → 1667）。"""
        from services.salary.enrollment_snapshot import generate_snapshot

        engine = SalaryEngine(load_from_db=False)
        with db() as session:
            room, teacher = _seed_class(session, enrollment=12, with_teacher=True)

            # 無快照：即時 12 人 → 2000 × 12/12 = 2000
            bd = engine.calculate_festival_bonus_breakdown(
                teacher.id, 2026, 3, _ctx={"session": session, "employee": teacher}
            )
            assert bd["festivalBonus"] == 2000
            assert bd["currentEnrollment"] == 12

            # 產快照並手調為 10 → 2000 × 10/12 = 1667
            generate_snapshot(session, 2026, 3, updated_by="tester")
            session.flush()
            from models.enrollment_snapshot import ClassEnrollmentSnapshot

            row = (
                session.query(ClassEnrollmentSnapshot)
                .filter(ClassEnrollmentSnapshot.classroom_id == room.id)
                .one()
            )
            row.student_count = 10
            session.flush()

            bd2 = engine.calculate_festival_bonus_breakdown(
                teacher.id, 2026, 3, _ctx={"session": session, "employee": teacher}
            )
            assert bd2["currentEnrollment"] == 10
            assert bd2["festivalBonus"] == 1667
