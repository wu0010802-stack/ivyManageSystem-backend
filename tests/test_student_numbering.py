import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Student


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


class TestStudentEnrollmentColumns:
    def test_columns_exist_and_nullable(self, session):
        stu = Student(student_id="LEGACY-1", name="舊生")
        session.add(stu)
        session.flush()
        assert stu.enrollment_school_year is None
        assert stu.enrollment_seq is None

    def test_student_id_no_longer_unique(self, session):
        session.add(Student(student_id="115-中-05", name="A"))
        session.add(Student(student_id="115-中-05", name="B"))
        session.flush()  # 不應因 unique 而炸

    def test_enrollment_key_composite_unique(self, session):
        session.add(
            Student(
                student_id="x1", name="A", enrollment_school_year=114, enrollment_seq=1
            )
        )
        session.flush()
        session.add(
            Student(
                student_id="x2", name="B", enrollment_school_year=114, enrollment_seq=1
            )
        )
        with pytest.raises(Exception):
            session.flush()
