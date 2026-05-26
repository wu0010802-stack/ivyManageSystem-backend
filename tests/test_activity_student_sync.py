"""sync_registrations_on_student_deactivate SAVEPOINT 回歸測試。

當批次軟刪過程中某筆失敗時：
- 不應汙染 SQLAlchemy session（後續筆仍可寫入）
- 失敗那筆的 is_active 必須維持原值（SAVEPOINT rollback）
- 其他筆正常完成軟刪
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def sqlite_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models.database import Base
    from models.academic_term import (
        AcademicTerm,
    )  # 註冊到 Base.metadata 以建 academic_terms 表  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    try:
        yield engine, session
    finally:
        session.close()


def _seed_three_regs(session):
    from models.database import ActivityRegistration, Classroom, Student

    classroom = Classroom(name="班A", is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="S100",
        name="王小明",
        birthday=date(2020, 5, 10),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    regs = []
    for i in range(3):
        r = ActivityRegistration(
            student_name=f"王小明{i}",
            class_name="班A",
            classroom_id=classroom.id,
            school_year=sy,
            semester=sem,
            student_id=student.id,
            is_active=True,
            paid_amount=0,
            match_status="matched",
            pending_review=False,
        )
        session.add(r)
        regs.append(r)
    session.commit()
    return student.id, [r.id for r in regs]


class TestPartialFailureSavepoint:
    def test_failure_on_one_reg_does_not_corrupt_others(
        self, monkeypatch, sqlite_session
    ):
        from services import activity_student_sync as ass
        from models.database import ActivityRegistration

        _engine, session = sqlite_session
        student_id, reg_ids = _seed_three_regs(session)
        failing_id = reg_ids[1]

        original = ass._soft_delete_single_registration

        def patched(session, reg, **kwargs):
            if reg.id == failing_id:
                raise RuntimeError("人為失敗")
            return original(session, reg, **kwargs)

        monkeypatch.setattr(ass, "_soft_delete_single_registration", patched)

        deleted = ass.sync_registrations_on_student_deactivate(session, student_id)

        # 兩筆成功軟刪
        assert deleted == 2, f"預期 2 筆成功，實際 {deleted}"

        # 重撈確認狀態
        session.expire_all()
        statuses = {
            r.id: r.is_active
            for r in session.query(ActivityRegistration)
            .filter(ActivityRegistration.id.in_(reg_ids))
            .all()
        }
        assert statuses[reg_ids[0]] is False
        assert (
            statuses[failing_id] is True
        ), "SAVEPOINT 未生效：失敗那筆的 is_active 應該維持 True"
        assert statuses[reg_ids[2]] is False

    def test_all_success_returns_full_count(self, sqlite_session):
        from services import activity_student_sync as ass

        _engine, session = sqlite_session
        student_id, _ = _seed_three_regs(session)

        deleted = ass.sync_registrations_on_student_deactivate(session, student_id)
        assert deleted == 3
