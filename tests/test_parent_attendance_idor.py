"""家長端個人資料 / 出席 + IDOR 防護測試（Batch 3）。

涵蓋：
- /api/parent/me、/my-children happy path
- /api/parent/attendance/daily、/monthly happy path + 不傳/錯日期
- IDOR：用他人小孩的 student_id 撞所有 endpoint → 403
"""

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router as parent_portal_router
from models.database import (
    Base,
    Classroom,
    Guardian,
    Student,
    StudentAttendance,
    User,
)
from utils.auth import create_access_token


@pytest.fixture
def parent_attend_client(tmp_path):
    db_path = tmp_path / "parent-attend.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    app = FastAPI()
    app.include_router(parent_portal_router)
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _create_parent_with_child(
    session,
    *,
    line_user_id: str,
    student_name: str,
    classroom_name: str = "向日葵班",
):
    user = User(
        employee_id=None,
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()

    classroom = (
        session.query(Classroom).filter(Classroom.name == classroom_name).first()
    )
    if not classroom:
        classroom = Classroom(name=classroom_name, is_active=True)
        session.add(classroom)
        session.flush()

    student = Student(
        student_id=f"S_{student_name}",
        name=student_name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="家長",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, guardian, student, classroom


def _make_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permissions": 0,
            "token_version": user.token_version or 0,
        }
    )


# ── Happy path ───────────────────────────────────────────────────────


class TestProfileHappyPath:
    def test_me_returns_basic_profile(self, parent_attend_client):
        client, session_factory = parent_attend_client
        with session_factory() as session:
            user, _, _, _ = _create_parent_with_child(
                session, line_user_id="U001", student_name="阿明"
            )
            session.commit()
            token = _make_token(user)

        resp = client.get("/api/parent/me", cookies={"access_token": token})
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "parent"
        assert data["line_user_id"] == "U001"
        assert data["can_push"] is False  # follow event 尚未發生

    def test_my_children_lists_only_owned_students(self, parent_attend_client):
        client, session_factory = parent_attend_client
        with session_factory() as session:
            user_a, _, _, _ = _create_parent_with_child(
                session, line_user_id="U_A", student_name="A1", classroom_name="A班"
            )
            # 另一家庭：不應出現在 A 家長的 my-children
            _create_parent_with_child(
                session, line_user_id="U_B", student_name="B1", classroom_name="B班"
            )
            session.commit()
            token_a = _make_token(user_a)

        resp = client.get("/api/parent/my-children", cookies={"access_token": token_a})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "A1"
        assert items[0]["classroom_name"] == "A班"


class TestAttendanceHappyPath:
    def test_daily_attendance_returns_record(self, parent_attend_client):
        client, session_factory = parent_attend_client
        target_date = date(2026, 4, 20)
        with session_factory() as session:
            user, _, student, _ = _create_parent_with_child(
                session, line_user_id="U_AT", student_name="阿出席"
            )
            session.add(
                StudentAttendance(
                    student_id=student.id,
                    date=target_date,
                    status="出席",
                    remark="無",
                )
            )
            session.commit()
            token = _make_token(user)
            student_id = student.id

        resp = client.get(
            "/api/parent/attendance/daily",
            params={"student_id": student_id, "date": "2026-04-20"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "出席"
        assert data["date"] == "2026-04-20"

    def test_daily_attendance_no_record_returns_null(self, parent_attend_client):
        client, session_factory = parent_attend_client
        with session_factory() as session:
            user, _, student, _ = _create_parent_with_child(
                session, line_user_id="U_NR", student_name="無紀錄"
            )
            session.commit()
            token = _make_token(user)
            student_id = student.id

        resp = client.get(
            "/api/parent/attendance/daily",
            params={"student_id": student_id, "date": "2026-04-20"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] is None

    def test_monthly_attendance_aggregates_counts(self, parent_attend_client):
        client, session_factory = parent_attend_client
        with session_factory() as session:
            user, _, student, _ = _create_parent_with_child(
                session, line_user_id="U_MO", student_name="月份"
            )
            for d, status in [
                (date(2026, 4, 1), "出席"),
                (date(2026, 4, 2), "缺席"),
                (date(2026, 4, 3), "病假"),
                (date(2026, 4, 4), "出席"),
                (date(2026, 5, 1), "出席"),  # 不該計入 4 月
            ]:
                session.add(
                    StudentAttendance(
                        student_id=student.id, date=d, status=status, remark=""
                    )
                )
            session.commit()
            token = _make_token(user)
            student_id = student.id

        resp = client.get(
            "/api/parent/attendance/monthly",
            params={"student_id": student_id, "year": 2026, "month": 4},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["recorded_days"] == 4
        assert data["counts"]["出席"] == 2
        assert data["counts"]["缺席"] == 1
        assert data["counts"]["病假"] == 1

    def test_invalid_date_format_returns_400(self, parent_attend_client):
        client, session_factory = parent_attend_client
        with session_factory() as session:
            user, _, student, _ = _create_parent_with_child(
                session, line_user_id="U_BD", student_name="壞日"
            )
            session.commit()
            token = _make_token(user)
            student_id = student.id

        resp = client.get(
            "/api/parent/attendance/daily",
            params={"student_id": student_id, "date": "2026/04/20"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 400


# ── IDOR 防護 ────────────────────────────────────────────────────────


class TestParentIdor:
    """每個接受 student_id 的 endpoint 必須阻擋跨家長存取。"""

    def test_daily_attendance_other_child_returns_403(self, parent_attend_client):
        client, session_factory = parent_attend_client
        with session_factory() as session:
            user_a, _, _, _ = _create_parent_with_child(
                session, line_user_id="U_PA", student_name="A_kid"
            )
            _, _, student_b, _ = _create_parent_with_child(
                session, line_user_id="U_PB", student_name="B_kid"
            )
            session.commit()
            token_a = _make_token(user_a)
            student_b_id = student_b.id

        resp = client.get(
            "/api/parent/attendance/daily",
            params={"student_id": student_b_id, "date": "2026-04-20"},
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403

    def test_monthly_attendance_other_child_returns_403(self, parent_attend_client):
        client, session_factory = parent_attend_client
        with session_factory() as session:
            user_a, _, _, _ = _create_parent_with_child(
                session, line_user_id="U_PA2", student_name="A_kid2"
            )
            _, _, student_b, _ = _create_parent_with_child(
                session, line_user_id="U_PB2", student_name="B_kid2"
            )
            session.commit()
            token_a = _make_token(user_a)
            student_b_id = student_b.id

        resp = client.get(
            "/api/parent/attendance/monthly",
            params={"student_id": student_b_id, "year": 2026, "month": 4},
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403

    def test_after_guardian_soft_delete_loses_access(self, parent_attend_client):
        """軟刪 Guardian 後家長即失去該學生的存取（_get_parent_student_ids 過濾 deleted_at IS NULL）。"""
        client, session_factory = parent_attend_client
        with session_factory() as session:
            user, guardian, student, _ = _create_parent_with_child(
                session, line_user_id="U_SD", student_name="軟刪"
            )
            session.commit()
            token = _make_token(user)
            student_id = student.id
            guardian_id = guardian.id

        # 軟刪
        with session_factory() as session:
            from datetime import datetime as _dt
            g = session.query(Guardian).filter(Guardian.id == guardian_id).first()
            g.deleted_at = _dt.now()
            session.commit()

        resp = client.get(
            "/api/parent/attendance/daily",
            params={"student_id": student_id, "date": "2026-04-20"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
