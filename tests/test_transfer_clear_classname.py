"""test_transfer_clear_classname.py — 驗證轉班至無班級時 class_name 同步清空。

P3 bug：sync_registrations_on_student_transfer 轉至 new_classroom_id=None 時
  class_name 殘留舊班名（「大象班」），classroom_id 已設 None → 跨欄位不一致。

修前：`if new_classroom_name: r.class_name = ...` → new_classroom_name 為 None 時不更新
修後：一律 `r.class_name = new_classroom_name` → None/有效班名均同步
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
    from models.academic_term import AcademicTerm  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    try:
        yield engine, session
    finally:
        session.close()


def _seed_student_with_reg(session, classroom_name: str = "大象班"):
    """建一個學生 + 指定班級 + 當學期啟用報名，回傳 (student_id, reg_id, classroom_id)。"""
    from models.database import ActivityRegistration, Classroom, Student
    from utils.academic import resolve_current_academic_term

    classroom = Classroom(name=classroom_name, is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="S-transfer-test",
        name="測試生",
        birthday=date(2020, 3, 15),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    sy, sem = resolve_current_academic_term()
    reg = ActivityRegistration(
        student_name="測試生",
        class_name=classroom_name,
        classroom_id=classroom.id,
        school_year=sy,
        semester=sem,
        student_id=student.id,
        is_active=True,
        paid_amount=0,
        match_status="matched",
        pending_review=False,
    )
    session.add(reg)
    session.commit()
    return student.id, reg.id, classroom.id


class TestTransferClearClassName:
    """轉班至無班級（new_classroom_id=None）時，class_name 必須同步清空。"""

    def test_transfer_to_no_class_clears_class_name(self, sqlite_session):
        """RED → GREEN：
        原在「大象班」的學生，轉至 new_classroom_id=None，
        報名 class_name 應清空（None），classroom_id 也應為 None。
        修前：class_name 殘留「大象班」（跨欄位不一致），本測試失敗。
        修後：兩欄均 None，本測試通過。
        """
        from models.database import ActivityRegistration
        from services.activity_student_sync import (
            sync_registrations_on_student_transfer,
        )

        _engine, session = sqlite_session
        student_id, reg_id, _classroom_id = _seed_student_with_reg(session)

        # 前置條件確認：報名帶有舊班名
        reg_before = session.get(ActivityRegistration, reg_id)
        assert reg_before.class_name == "大象班", "前置條件：class_name 應為「大象班」"
        assert reg_before.classroom_id is not None, "前置條件：classroom_id 不應為 None"

        # 轉至無班級
        count = sync_registrations_on_student_transfer(session, student_id, None)
        session.flush()

        assert count == 1, f"預期更新 1 筆報名，實際 {count}"

        session.expire_all()
        reg_after = session.get(ActivityRegistration, reg_id)
        assert reg_after.classroom_id is None, "classroom_id 應設為 None"
        assert (
            reg_after.class_name is None
        ), f"class_name 應清空為 None（修前殘留「{reg_after.class_name}」）"

    def test_transfer_to_valid_class_updates_class_name(self, sqlite_session):
        """防迴歸：轉至有效 B 班時，class_name 更新為 B 班名、classroom_id 更新為 B 班 id。"""
        from models.database import ActivityRegistration, Classroom
        from services.activity_student_sync import (
            sync_registrations_on_student_transfer,
        )

        _engine, session = sqlite_session
        student_id, reg_id, _classroom_a_id = _seed_student_with_reg(
            session, classroom_name="大象班"
        )

        # 建立 B 班
        classroom_b = Classroom(name="老虎班", is_active=True)
        session.add(classroom_b)
        session.flush()

        count = sync_registrations_on_student_transfer(
            session, student_id, classroom_b.id
        )
        session.flush()

        assert count == 1, f"預期更新 1 筆，實際 {count}"

        session.expire_all()
        reg_after = session.get(ActivityRegistration, reg_id)
        assert reg_after.classroom_id == classroom_b.id, "classroom_id 應更新為 B 班 id"
        assert (
            reg_after.class_name == "老虎班"
        ), f"class_name 應更新為「老虎班」，實際「{reg_after.class_name}」"
