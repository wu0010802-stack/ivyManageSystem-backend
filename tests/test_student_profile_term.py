"""Task 8: _serialize_lifecycle 暴露 enrollment_school_year + enrollment_semester。"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base

# 確保 assemble_profile 所需的所有表都被 Base.metadata 收錄
import models  # noqa: F401 — 觸發 __init__ 中的 bulk import
import models.student_log  # noqa: F401 — StudentChangeLog（__init__ 未收）
import models.guardian  # noqa: F401 — Guardian
import models.classroom  # noqa: F401 — Student 等
import models.fees  # noqa: F401 — StudentFeeRecord / StudentFeeAdjustment

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


def test_profile_lifecycle_exposes_enrollment_semester(session):
    from services.student_profile import assemble_profile

    s = Student(
        student_id="115-T-001",
        name="檔案童",
        lifecycle_status="active",
        enrollment_school_year=115,
        enrollment_semester=1,
    )
    session.add(s)
    session.commit()

    profile = assemble_profile(session, s.id)
    assert profile is not None
    assert profile["lifecycle"]["enrollment_school_year"] == 115
    assert profile["lifecycle"]["enrollment_semester"] == 1
