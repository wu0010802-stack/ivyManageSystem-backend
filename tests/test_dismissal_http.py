"""
tests/test_dismissal_http.py — 接送通知 HTTP 端點測試

測試範圍：
- create_dismissal_call：正常建立、學生不存在、班級不符、重複通知
- list_dismissal_calls：日期篩選、狀態篩選、班級篩選
- cancel_dismissal_call：pending/acknowledged 可取消，completed/cancelled 不可
"""

import os
import sys
import asyncio
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

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
from models.dismissal import StudentDismissalCall


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session():
    """SQLite in-memory，使用 StaticPool 確保 session.close() 後資料不丟失。"""
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
    """1 班級、2 學生、1 管理員帳號。"""
    grade = ClassGrade(name="大班", sort_order=1)
    session.add(grade)
    session.flush()

    classroom = Classroom(
        name="向日葵班", school_year=2025, semester=2, grade_id=grade.id
    )
    session.add(classroom)
    session.flush()

    s1 = Student(student_id="S001", name="小明", classroom_id=classroom.id)
    s2 = Student(student_id="S002", name="小華", classroom_id=classroom.id)
    session.add_all([s1, s2])
    session.flush()

    admin = User(username="admin", password_hash="x", role="admin", permissions=-1)
    session.add(admin)
    session.commit()

    return {
        "classroom": classroom,
        "s1": s1,
        "s2": s2,
        "admin": admin,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _admin_user(user_id=1):
    return {"user_id": user_id, "username": "admin", "permissions": -1}


def _mock_manager():
    mgr = MagicMock()
    mgr.broadcast = AsyncMock()
    return mgr


def _create_call_direct(session, student, classroom, status="pending",
                        requested_at=None, user_id=1) -> StudentDismissalCall:
    call = StudentDismissalCall(
        student_id=student.id,
        classroom_id=classroom.id,
        requested_by_user_id=user_id,
        status=status,
        requested_at=requested_at or datetime.now(),
    )
    session.add(call)
    session.commit()
    return call


# ---------------------------------------------------------------------------
# TestCreateDismissalCall
# ---------------------------------------------------------------------------

class TestCreateDismissalCall:
    def _run(self, session, student_id, classroom_id, note=None):
        from api.dismissal_calls import create_dismissal_call, DismissalCallCreate
        body = DismissalCallCreate(student_id=student_id, classroom_id=classroom_id, note=note)
        session.close = MagicMock()
        mgr = _mock_manager()
        with patch("api.dismissal_calls.get_session", return_value=session), \
             patch("api.dismissal_calls._get_manager", return_value=mgr):
            return asyncio.run(
                create_dismissal_call(body=body, current_user=_admin_user())
            ), mgr

    def test_creates_pending_call_for_valid_student(self, session, seed_data):
        """正常建立後狀態應為 pending，且 DB 寫入一筆記錄。"""
        result, _ = self._run(session, seed_data["s1"].id, seed_data["classroom"].id)

        assert result["status"] == "pending"
        assert result["student_name"] == "小明"
        assert result["classroom_name"] == "向日葵班"
        assert session.query(StudentDismissalCall).count() == 1

    def test_broadcast_called_on_create(self, session, seed_data):
        """建立成功後應廣播 dismissal_call_created 事件。"""
        _, mgr = self._run(session, seed_data["s1"].id, seed_data["classroom"].id)
        mgr.broadcast.assert_called_once()
        call_args = mgr.broadcast.call_args
        assert call_args[0][1]["type"] == "dismissal_call_created"

    def test_student_not_found_raises_404(self, session, seed_data):
        """不存在的學生 ID 應回傳 404。"""
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, student_id=99999, classroom_id=seed_data["classroom"].id)
        assert exc_info.value.status_code == 404

    def test_student_not_in_classroom_raises_400(self, session, seed_data):
        """學生班級與請求班級不符應回傳 400。"""
        other_grade = ClassGrade(name="中班", sort_order=2)
        session.add(other_grade)
        session.flush()
        other_cls = Classroom(name="玫瑰班", school_year=2025, semester=2, grade_id=other_grade.id)
        session.add(other_cls)
        session.commit()

        with pytest.raises(HTTPException) as exc_info:
            self._run(session, seed_data["s1"].id, other_cls.id)
        assert exc_info.value.status_code == 400

    def test_duplicate_pending_raises_409(self, session, seed_data):
        """同學生已有 pending 通知時應回傳 409。"""
        _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "pending")
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, seed_data["s1"].id, seed_data["classroom"].id)
        assert exc_info.value.status_code == 409

    def test_duplicate_acknowledged_raises_409(self, session, seed_data):
        """同學生已有 acknowledged 通知時應回傳 409。"""
        _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "acknowledged")
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, seed_data["s1"].id, seed_data["classroom"].id)
        assert exc_info.value.status_code == 409

    def test_completed_call_allows_new_one(self, session, seed_data):
        """已 completed 的通知不阻擋新通知建立（業務上可再次呼叫）。"""
        _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "completed")
        result, _ = self._run(session, seed_data["s1"].id, seed_data["classroom"].id)
        assert result["status"] == "pending"


# ---------------------------------------------------------------------------
# TestListDismissalCalls
# ---------------------------------------------------------------------------

class TestListDismissalCalls:
    def _run(self, session, target_date=None, status=None, classroom_id=None):
        from api.dismissal_calls import list_dismissal_calls
        session.close = MagicMock()
        with patch("api.dismissal_calls.get_session", return_value=session):
            return list_dismissal_calls(
                target_date=target_date,
                status=status,
                classroom_id=classroom_id,
                current_user=_admin_user(),
            )

    def test_returns_todays_calls_by_default(self, session, seed_data):
        """不指定日期時只回傳今日的通知。"""
        today_call = _create_call_direct(
            session, seed_data["s1"], seed_data["classroom"],
            requested_at=datetime.now()
        )
        yesterday_call = _create_call_direct(
            session, seed_data["s2"], seed_data["classroom"],
            requested_at=datetime.now() - timedelta(days=1)
        )
        result = self._run(session)
        ids = {r["id"] for r in result}
        assert today_call.id in ids
        assert yesterday_call.id not in ids

    def test_filters_by_classroom_id(self, session, seed_data):
        """指定 classroom_id 只回傳該班級的通知。"""
        other_grade = ClassGrade(name="中班", sort_order=2)
        session.add(other_grade)
        session.flush()
        other_cls = Classroom(name="玫瑰班", school_year=2025, semester=2, grade_id=other_grade.id)
        session.add(other_cls)
        other_student = Student(student_id="S099", name="外班生", classroom_id=other_cls.id)
        session.add(other_student)
        session.commit()

        call_a = _create_call_direct(session, seed_data["s1"], seed_data["classroom"])
        call_other = _create_call_direct(session, other_student, other_cls)

        result = self._run(session, classroom_id=seed_data["classroom"].id)
        ids = {r["id"] for r in result}
        assert call_a.id in ids
        assert call_other.id not in ids

    def test_filters_by_status(self, session, seed_data):
        """status 篩選應只回傳指定狀態的通知。"""
        pending_call = _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "pending")
        completed_call = _create_call_direct(session, seed_data["s2"], seed_data["classroom"], "completed")

        result = self._run(session, status="pending")
        ids = {r["id"] for r in result}
        assert pending_call.id in ids
        assert completed_call.id not in ids

    def test_invalid_date_format_raises_400(self, session, seed_data):
        """日期格式錯誤應回傳 400。"""
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, target_date="2025/09/01")
        assert exc_info.value.status_code == 400

    def test_returns_empty_list_when_no_calls(self, session, seed_data):
        """無通知時應回傳空列表。"""
        result = self._run(session)
        assert result == []


# ---------------------------------------------------------------------------
# TestCancelDismissalCall
# ---------------------------------------------------------------------------

class TestCancelDismissalCall:
    def _run(self, session, call_id):
        from api.dismissal_calls import cancel_dismissal_call
        session.close = MagicMock()
        mgr = _mock_manager()
        with patch("api.dismissal_calls.get_session", return_value=session), \
             patch("api.dismissal_calls._get_manager", return_value=mgr):
            return asyncio.run(
                cancel_dismissal_call(call_id=call_id, current_user=_admin_user())
            ), mgr

    def test_cancel_pending_call_succeeds(self, session, seed_data):
        """pending 狀態的通知可以取消。"""
        call = _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "pending")
        result, _ = self._run(session, call.id)
        assert result["status"] == "cancelled"

    def test_cancel_acknowledged_call_succeeds(self, session, seed_data):
        """acknowledged 狀態的通知也可以取消。"""
        call = _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "acknowledged")
        result, _ = self._run(session, call.id)
        assert result["status"] == "cancelled"

    def test_broadcast_called_on_cancel(self, session, seed_data):
        """取消成功後應廣播 dismissal_call_cancelled 事件。"""
        call = _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "pending")
        _, mgr = self._run(session, call.id)
        mgr.broadcast.assert_called_once()
        assert mgr.broadcast.call_args[0][1]["type"] == "dismissal_call_cancelled"

    def test_cancel_completed_raises_422(self, session, seed_data):
        """已 completed 的通知不可取消。"""
        call = _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "completed")
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, call.id)
        assert exc_info.value.status_code == 422

    def test_cancel_already_cancelled_raises_422(self, session, seed_data):
        """已 cancelled 的通知再次取消應回傳 422。"""
        call = _create_call_direct(session, seed_data["s1"], seed_data["classroom"], "cancelled")
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, call.id)
        assert exc_info.value.status_code == 422

    def test_cancel_not_found_raises_404(self, session, seed_data):
        """不存在的通知 ID 應回傳 404。"""
        with pytest.raises(HTTPException) as exc_info:
            self._run(session, call_id=99999)
        assert exc_info.value.status_code == 404
