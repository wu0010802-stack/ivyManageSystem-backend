"""tests/test_recruitment_conversion.py

驗證招生訪視 → 正式學生 轉化服務的原子性、重複偵測、學號唯一性。
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
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ENROLLED,
    Classroom,
    Student,
)
from models.guardian import Guardian
from models.recruitment import RecruitmentVisit
from models.student_log import StudentChangeLog
from services.recruitment_conversion import (
    RecruitmentConversionError,
    convert_recruitment_to_student,
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
def classroom(session):
    c = Classroom(name="小班-甲", school_year=114, semester=1)
    session.add(c)
    session.flush()
    return c


@pytest.fixture
def visit(session):
    v = RecruitmentVisit(
        month="115.03",
        child_name="王小寶",
        birthday=date(2022, 1, 1),
        grade="小班",
        phone="0912-345-678",
        address="高雄市某路 1 號",
        has_deposit=True,
    )
    session.add(v)
    session.commit()
    return v


class TestConvertHappyPath:
    def test_creates_student_guardian_changelog(self, session, visit, classroom):
        result = convert_recruitment_to_student(
            session,
            recruitment_visit_id=visit.id,
            student_id_code="S2026001",
            classroom_id=classroom.id,
            enrollment_date=date(2026, 8, 1),
        )
        session.commit()

        student = session.query(Student).filter_by(id=result.student_id).one()
        assert student.name == "王小寶"
        assert student.birthday == date(2022, 1, 1)
        assert student.classroom_id == classroom.id
        assert student.lifecycle_status == LIFECYCLE_ENROLLED
        assert student.recruitment_visit_id == visit.id
        assert student.parent_phone == "0912-345-678"  # 快照欄位同步

        guardians = session.query(Guardian).filter_by(student_id=student.id).all()
        assert len(guardians) == 1
        assert guardians[0].is_primary is True
        assert guardians[0].can_pickup is True
        assert guardians[0].phone == "0912-345-678"

        logs = session.query(StudentChangeLog).filter_by(student_id=student.id).all()
        assert len(logs) == 1
        assert logs[0].event_type == "入學"
        assert logs[0].reason == "招生轉化"

        # visit 被標記為已錄取
        assert visit.enrolled is True

    def test_active_initial_status(self, session, visit, classroom):
        result = convert_recruitment_to_student(
            session,
            recruitment_visit_id=visit.id,
            student_id_code="S2026002",
            classroom_id=classroom.id,
            initial_lifecycle_status=LIFECYCLE_ACTIVE,
        )
        session.commit()
        student = session.query(Student).filter_by(id=result.student_id).one()
        assert student.lifecycle_status == LIFECYCLE_ACTIVE


class TestConvertValidation:
    def test_missing_visit(self, session):
        with pytest.raises(RecruitmentConversionError, match="招生訪視不存在"):
            convert_recruitment_to_student(
                session,
                recruitment_visit_id=99999,
                student_id_code="SX",
            )

    def test_duplicate_conversion_rejected(self, session, visit, classroom):
        convert_recruitment_to_student(
            session,
            recruitment_visit_id=visit.id,
            student_id_code="S2026001",
            classroom_id=classroom.id,
        )
        session.commit()

        with pytest.raises(
            RecruitmentConversionError, match="此招生訪視已轉化為學生"
        ):
            convert_recruitment_to_student(
                session,
                recruitment_visit_id=visit.id,
                student_id_code="S2026002",
                classroom_id=classroom.id,
            )

    def test_duplicate_student_code_rejected(self, session, visit, classroom):
        existing = Student(
            student_id="S2026001",
            name="已存在",
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        session.add(existing)
        session.commit()

        with pytest.raises(RecruitmentConversionError, match="學號已存在"):
            convert_recruitment_to_student(
                session,
                recruitment_visit_id=visit.id,
                student_id_code="S2026001",
                classroom_id=classroom.id,
            )

    def test_empty_code_rejected(self, session, visit):
        with pytest.raises(RecruitmentConversionError, match="學號不可為空"):
            convert_recruitment_to_student(
                session,
                recruitment_visit_id=visit.id,
                student_id_code="   ",
            )

    def test_invalid_initial_status_rejected(self, session, visit):
        with pytest.raises(RecruitmentConversionError):
            convert_recruitment_to_student(
                session,
                recruitment_visit_id=visit.id,
                student_id_code="SX",
                initial_lifecycle_status="prospect",  # 不允許
            )


class TestConvertAtomicity:
    def test_rollback_leaves_no_orphan(self, session, visit, classroom, monkeypatch):
        """模擬中段失敗：手動 rollback 後不應有殘留 student/guardian/log。"""
        try:
            # 強制觸發錯誤：給重複學號
            existing = Student(
                student_id="DUP",
                name="x",
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
            session.add(existing)
            session.commit()
            before_students = session.query(Student).count()
            before_guardians = session.query(Guardian).count()
            before_logs = session.query(StudentChangeLog).count()

            with pytest.raises(RecruitmentConversionError):
                convert_recruitment_to_student(
                    session,
                    recruitment_visit_id=visit.id,
                    student_id_code="DUP",
                    classroom_id=classroom.id,
                )
            session.rollback()

            assert session.query(Student).count() == before_students
            assert session.query(Guardian).count() == before_guardians
            assert session.query(StudentChangeLog).count() == before_logs
        finally:
            session.rollback()
