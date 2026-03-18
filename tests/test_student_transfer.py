"""
tests/test_student_transfer.py — 學生轉班邏輯測試

測試範圍：
- get_classroom_student_ids_at_date()：歷史日期班級歸屬溯源
- bulk_transfer_students endpoint：轉班記錄寫入、相同班級略過、錯誤處理
"""

import os
import sys
import asyncio
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.employee import Employee
from models.auth import User
from models.classroom import Classroom, Student, ClassGrade
from models.student_transfer import StudentClassroomTransfer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    """SQLite in-memory（StaticPool，允許多次 session.close() 後仍可查詢）。"""
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
def seed_data(session):
    """兩個班級、三個學生（s1、s2 在 classA，s3 在 classB）。"""
    grade = ClassGrade(name="中班", sort_order=2)
    session.add(grade)
    session.flush()

    class_a = Classroom(name="玫瑰班", school_year=2025, semester=2, grade_id=grade.id)
    class_b = Classroom(name="百合班", school_year=2025, semester=2, grade_id=grade.id)
    session.add_all([class_a, class_b])
    session.flush()

    s1 = Student(student_id="S001", name="小明", classroom_id=class_a.id)
    s2 = Student(student_id="S002", name="小華", classroom_id=class_a.id)
    s3 = Student(student_id="S003", name="小美", classroom_id=class_b.id)
    session.add_all([s1, s2, s3])

    admin = User(username="admin", password_hash="x", role="admin", permissions=-1)
    session.add(admin)
    session.commit()

    return {
        "class_a": class_a,
        "class_b": class_b,
        "s1": s1,
        "s2": s2,
        "s3": s3,
        "admin": admin,
    }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _add_transfer(session, student, from_cls, to_cls, at: datetime):
    t = StudentClassroomTransfer(
        student_id=student.id,
        from_classroom_id=from_cls.id,
        to_classroom_id=to_cls.id,
        transferred_at=at,
    )
    session.add(t)
    session.commit()
    return t


# ---------------------------------------------------------------------------
# get_classroom_student_ids_at_date()
# ---------------------------------------------------------------------------

class TestGetClassroomStudentIdsAtDate:
    """歷史日期班級歸屬溯源邏輯。"""

    def test_no_transfer_returns_students_in_current_classroom(self, session, seed_data):
        """從未轉班的學生，直接以 classroom_id 判斷歸屬。"""
        from api.students import get_classroom_student_ids_at_date
        result = get_classroom_student_ids_at_date(
            session, seed_data["class_a"].id, date.today()
        )
        assert set(result) == {seed_data["s1"].id, seed_data["s2"].id}

    def test_after_transfer_student_appears_in_new_classroom(self, session, seed_data):
        """轉班後，學生應出現在新班級，不在舊班級。"""
        from api.students import get_classroom_student_ids_at_date
        transfer_time = datetime(2025, 9, 1, 10, 0, 0)
        _add_transfer(session, seed_data["s1"], seed_data["class_a"], seed_data["class_b"], transfer_time)
        # 更新 classroom_id（模擬 bulk_transfer 的效果）
        seed_data["s1"].classroom_id = seed_data["class_b"].id
        session.commit()

        query_date = date(2025, 9, 15)
        result_a = get_classroom_student_ids_at_date(session, seed_data["class_a"].id, query_date)
        result_b = get_classroom_student_ids_at_date(session, seed_data["class_b"].id, query_date)

        assert seed_data["s1"].id not in result_a, "轉班後不應在原班"
        assert seed_data["s1"].id in result_b, "轉班後應在新班"

    def test_between_two_transfers_student_in_intermediate_classroom(self, session, seed_data):
        """兩次轉班：查詢第一次轉班後、第二次轉班前，學生應在中間班級。"""
        from api.students import get_classroom_student_ids_at_date
        t1 = datetime(2025, 8, 15)   # class_a → class_b
        t2 = datetime(2025, 11, 1)   # class_b → class_a（轉回）

        _add_transfer(session, seed_data["s1"], seed_data["class_a"], seed_data["class_b"], t1)
        _add_transfer(session, seed_data["s1"], seed_data["class_b"], seed_data["class_a"], t2)
        seed_data["s1"].classroom_id = seed_data["class_a"].id
        session.commit()

        # 查詢 t1 之後、t2 之前 → 應在 class_b
        query_date = date(2025, 10, 31)
        result_b = get_classroom_student_ids_at_date(session, seed_data["class_b"].id, query_date)
        assert seed_data["s1"].id in result_b, "t1–t2 期間查詢應在中間班級"

        # 查詢 t2 之後 → 已轉回 class_a
        result_a = get_classroom_student_ids_at_date(session, seed_data["class_a"].id, date(2025, 11, 15))
        assert seed_data["s1"].id in result_a

    def test_inactive_student_excluded_from_no_transfer_path(self, session, seed_data):
        """is_active=False 的學生（從未轉班路徑）不應出現在結果中。"""
        from api.students import get_classroom_student_ids_at_date
        seed_data["s2"].is_active = False
        session.commit()

        result = get_classroom_student_ids_at_date(
            session, seed_data["class_a"].id, date.today()
        )
        assert seed_data["s2"].id not in result

    def test_student_transferred_out_not_in_original_classroom(self, session, seed_data):
        """有轉班記錄且轉出的學生，在轉班後不應出現在原班查詢結果中。"""
        from api.students import get_classroom_student_ids_at_date
        t = datetime(2025, 9, 1)
        _add_transfer(session, seed_data["s2"], seed_data["class_a"], seed_data["class_b"], t)
        seed_data["s2"].classroom_id = seed_data["class_b"].id
        session.commit()

        result = get_classroom_student_ids_at_date(session, seed_data["class_a"].id, date(2025, 10, 1))
        assert seed_data["s2"].id not in result

    def test_multiple_transfers_uses_latest_before_date(self, session, seed_data):
        """多次轉班時，應以查詢日期前的最後一筆為準。"""
        from api.students import get_classroom_student_ids_at_date
        t1 = datetime(2025, 8, 1)
        t2 = datetime(2025, 10, 1)
        # 第一次：A → B
        _add_transfer(session, seed_data["s1"], seed_data["class_a"], seed_data["class_b"], t1)
        # 第二次：B → A（轉回來）
        _add_transfer(session, seed_data["s1"], seed_data["class_b"], seed_data["class_a"], t2)
        seed_data["s1"].classroom_id = seed_data["class_a"].id
        session.commit()

        # 查詢第一次轉班後、第二次轉班前 → 應在 class_b
        result_b = get_classroom_student_ids_at_date(session, seed_data["class_b"].id, date(2025, 9, 1))
        assert seed_data["s1"].id in result_b

        # 查詢第二次轉班後 → 應回到 class_a
        result_a = get_classroom_student_ids_at_date(session, seed_data["class_a"].id, date(2025, 11, 1))
        assert seed_data["s1"].id in result_a


# ---------------------------------------------------------------------------
# bulk_transfer_students endpoint
# ---------------------------------------------------------------------------

class TestBulkTransferStudents:
    """透過 endpoint 函式測試轉班業務邏輯。"""

    def _run(self, session, student_ids, target_classroom_id, user_id=1):
        from api.students import bulk_transfer_students, StudentBulkTransfer
        item = StudentBulkTransfer(
            student_ids=student_ids,
            target_classroom_id=target_classroom_id,
        )
        current_user = {"user_id": user_id, "username": "admin", "permissions": -1}
        session.close = MagicMock()
        with patch("api.students.get_session", return_value=session):
            return asyncio.run(bulk_transfer_students(item=item, current_user=current_user))

    def test_creates_transfer_record_and_updates_classroom(self, session, seed_data):
        """基本轉班：寫入 StudentClassroomTransfer，更新 classroom_id。"""
        result = self._run(
            session,
            student_ids=[seed_data["s1"].id],
            target_classroom_id=seed_data["class_b"].id,
        )
        assert result["moved_count"] == 1

        records = session.query(StudentClassroomTransfer).all()
        assert len(records) == 1
        assert records[0].student_id == seed_data["s1"].id
        assert records[0].from_classroom_id == seed_data["class_a"].id
        assert records[0].to_classroom_id == seed_data["class_b"].id

        session.refresh(seed_data["s1"])
        assert seed_data["s1"].classroom_id == seed_data["class_b"].id

    def test_same_classroom_transfer_skipped(self, session, seed_data):
        """轉到相同班級的學生不應寫入記錄。"""
        result = self._run(
            session,
            student_ids=[seed_data["s1"].id],
            target_classroom_id=seed_data["class_a"].id,  # 同班
        )
        assert result["moved_count"] == 0
        assert session.query(StudentClassroomTransfer).count() == 0

    def test_mixed_same_and_different_classroom(self, session, seed_data):
        """批次轉班時，只有真的換班的學生被計入。"""
        result = self._run(
            session,
            student_ids=[seed_data["s1"].id, seed_data["s3"].id],
            target_classroom_id=seed_data["class_b"].id,
        )
        # s1：class_a → class_b（移動）；s3：class_b → class_b（跳過）
        assert result["moved_count"] == 1

    def test_inactive_target_classroom_raises_400(self, session, seed_data):
        """停用的目標班級應回傳 400。"""
        seed_data["class_b"].is_active = False
        session.commit()
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, student_ids=[seed_data["s1"].id], target_classroom_id=seed_data["class_b"].id)
        assert exc_info.value.status_code == 400

    def test_missing_student_raises_404(self, session, seed_data):
        """找不到學生應回傳 404。"""
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, student_ids=[99999], target_classroom_id=seed_data["class_b"].id)
        assert exc_info.value.status_code == 404

    def test_empty_student_ids_raises_400(self, session, seed_data):
        """空的學生列表應回傳 400。"""
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, student_ids=[], target_classroom_id=seed_data["class_b"].id)
        assert exc_info.value.status_code == 400
