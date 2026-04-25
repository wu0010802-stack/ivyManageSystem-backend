"""家長端學生請假整合測試（Batch 5）。"""

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
from api.student_leaves import router as student_leaves_router
from models.database import (
    Base,
    Classroom,
    Employee,
    Guardian,
    Holiday,
    Student,
    StudentAttendance,
    StudentLeaveRequest,
    User,
    WorkdayOverride,
)
from utils.auth import create_access_token, hash_password


@pytest.fixture
def leave_client(tmp_path):
    db_path = tmp_path / "leave.sqlite"
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
    app.include_router(student_leaves_router)
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _setup_family(session, *, line_user_id="UF", student_name="小明", classroom_name="向日葵"):
    user = User(
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
        name="父親",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, guardian, student, classroom


def _create_admin(session) -> User:
    user = User(
        employee_id=None,
        username="admin",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permissions=-1,
        is_active=True,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _parent_token(user: User) -> str:
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


def _admin_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permissions": user.permissions if user.permissions is not None else -1,
            "token_version": user.token_version or 0,
        }
    )


# 全部用「足夠未來」的工作日避免週末干擾
def _next_monday(start: date) -> date:
    while start.weekday() != 0:
        start += timedelta(days=1)
    return start


class TestCreateLeave:
    def test_happy_path_creates_pending(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            session.commit()
            token = _parent_token(user)
            student_id = student.id

        monday = _next_monday(date.today() + timedelta(days=7))
        resp = client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_id,
                "leave_type": "病假",
                "start_date": monday.isoformat(),
                "end_date": (monday + timedelta(days=2)).isoformat(),
                "reason": "感冒",
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "pending"
        assert resp.json()["leave_type"] == "病假"

    def test_invalid_leave_type_400(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            session.commit()
            token = _parent_token(user)
            student_id = student.id

        resp = client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_id,
                "leave_type": "曠課",
                "start_date": "2026-05-01",
                "end_date": "2026-05-01",
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 422  # pydantic 驗證

    def test_overlap_with_pending_returns_400(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            session.commit()
            token = _parent_token(user)
            student_id = student.id

        monday = _next_monday(date.today() + timedelta(days=7))
        body = {
            "student_id": student_id,
            "leave_type": "事假",
            "start_date": monday.isoformat(),
            "end_date": (monday + timedelta(days=1)).isoformat(),
        }
        first = client.post(
            "/api/parent/student-leaves", json=body, cookies={"access_token": token}
        )
        assert first.status_code == 201

        overlap_body = {**body, "start_date": (monday + timedelta(days=1)).isoformat(),
                        "end_date": (monday + timedelta(days=3)).isoformat()}
        second = client.post(
            "/api/parent/student-leaves",
            json=overlap_body,
            cookies={"access_token": token},
        )
        assert second.status_code == 400


class TestApproveAndAttendance:
    def test_approve_creates_attendance_for_workdays_only(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            parent_user, _, student, _ = _setup_family(session)
            admin = _create_admin(session)
            session.commit()
            parent_token = _parent_token(parent_user)
            admin_token = _admin_token(admin)
            student_id = student.id
            admin_id = admin.id

        # 申請 2026-04-22 (三) ~ 2026-04-26 (日) 5 天，週末應排除 → 3 個工作日
        # 但需確認 2026-04-22~26 區間沒落在 today-30 之外（今天 2026-04-25）
        resp = client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_id,
                "leave_type": "病假",
                "start_date": "2026-04-22",
                "end_date": "2026-04-26",
                "reason": "感冒",
            },
            cookies={"access_token": parent_token},
        )
        assert resp.status_code == 201
        leave_id = resp.json()["id"]

        approve = client.post(
            f"/api/student-leaves/{leave_id}/approve",
            json={"review_note": "OK"},
            cookies={"access_token": admin_token},
        )
        assert approve.status_code == 200
        assert approve.json()["affected_days"] == 3  # 三、四、五

        with session_factory() as session:
            attendances = (
                session.query(StudentAttendance)
                .filter(StudentAttendance.student_id == student_id)
                .order_by(StudentAttendance.date.asc())
                .all()
            )
            assert len(attendances) == 3
            for a in attendances:
                assert a.status == "病假"
                assert a.remark == f"家長申請#{leave_id}"
                assert a.recorded_by == admin_id

    def test_approve_overrides_existing_attendance(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            parent_user, _, student, _ = _setup_family(session)
            admin = _create_admin(session)
            # 預先寫一筆當日「出席」紀錄，模擬老師已點名
            session.add(
                StudentAttendance(
                    student_id=student.id,
                    date=date(2026, 4, 22),
                    status="出席",
                    remark="老師原本紀錄",
                    recorded_by=admin.id,
                )
            )
            session.commit()
            parent_token = _parent_token(parent_user)
            admin_token = _admin_token(admin)
            student_id = student.id

        client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_id,
                "leave_type": "事假",
                "start_date": "2026-04-22",
                "end_date": "2026-04-22",
            },
            cookies={"access_token": parent_token},
        )
        leave = client.get(
            "/api/parent/student-leaves", cookies={"access_token": parent_token}
        ).json()["items"][0]
        leave_id = leave["id"]

        client.post(
            f"/api/student-leaves/{leave_id}/approve",
            cookies={"access_token": admin_token},
        )
        with session_factory() as session:
            row = (
                session.query(StudentAttendance)
                .filter(
                    StudentAttendance.student_id == student_id,
                    StudentAttendance.date == date(2026, 4, 22),
                )
                .first()
            )
            assert row.status == "事假"
            assert row.remark == f"家長申請#{leave_id}"

    def test_reject_after_approve_reverts_attendance(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            parent_user, _, student, _ = _setup_family(session)
            admin = _create_admin(session)
            # 老師原本沒紀錄
            session.commit()
            parent_token = _parent_token(parent_user)
            admin_token = _admin_token(admin)
            student_id = student.id

        client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_id,
                "leave_type": "病假",
                "start_date": "2026-04-22",
                "end_date": "2026-04-22",
            },
            cookies={"access_token": parent_token},
        )
        leave = client.get(
            "/api/parent/student-leaves", cookies={"access_token": parent_token}
        ).json()["items"][0]
        leave_id = leave["id"]

        client.post(
            f"/api/student-leaves/{leave_id}/approve",
            cookies={"access_token": admin_token},
        )
        with session_factory() as session:
            assert (
                session.query(StudentAttendance)
                .filter(StudentAttendance.student_id == student_id)
                .count()
                == 1
            )

        client.post(
            f"/api/student-leaves/{leave_id}/reject",
            json={"review_note": "改判"},
            cookies={"access_token": admin_token},
        )
        with session_factory() as session:
            assert (
                session.query(StudentAttendance)
                .filter(StudentAttendance.student_id == student_id)
                .count()
                == 0
            )

    def test_reject_only_clears_remark_owned_by_leave(self, leave_client):
        """approve 後，若教師後手新增了一筆同日獨立紀錄（remark 不同），
        reject 反向清除時不可誤殺。"""
        client, session_factory = leave_client
        with session_factory() as session:
            parent_user, _, student, _ = _setup_family(session)
            admin = _create_admin(session)
            session.commit()
            parent_token = _parent_token(parent_user)
            admin_token = _admin_token(admin)
            student_id = student.id

        # approve 一個 4-22 ~ 4-22 的請假
        client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_id,
                "leave_type": "病假",
                "start_date": "2026-04-22",
                "end_date": "2026-04-22",
            },
            cookies={"access_token": parent_token},
        )
        leave_id = client.get(
            "/api/parent/student-leaves", cookies={"access_token": parent_token}
        ).json()["items"][0]["id"]
        client.post(
            f"/api/student-leaves/{leave_id}/approve",
            cookies={"access_token": admin_token},
        )

        # 教師後手把 attendance.remark 改成自己的紀錄（模擬：覆寫了 remark）
        with session_factory() as session:
            row = (
                session.query(StudentAttendance)
                .filter(
                    StudentAttendance.student_id == student_id,
                    StudentAttendance.date == date(2026, 4, 22),
                )
                .first()
            )
            row.remark = "教師獨立判斷"
            session.commit()

        # reject：因 remark 已不再吻合，應該保留該紀錄
        client.post(
            f"/api/student-leaves/{leave_id}/reject",
            cookies={"access_token": admin_token},
        )
        with session_factory() as session:
            assert (
                session.query(StudentAttendance)
                .filter(StudentAttendance.student_id == student_id)
                .count()
                == 1
            )

    def test_holiday_excluded_from_attendance(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            parent_user, _, student, _ = _setup_family(session)
            admin = _create_admin(session)
            # 2026-04-23 設為國定假日
            session.add(
                Holiday(date=date(2026, 4, 23), name="假設國定假日", is_active=True)
            )
            session.commit()
            parent_token = _parent_token(parent_user)
            admin_token = _admin_token(admin)
            student_id = student.id

        client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_id,
                "leave_type": "病假",
                "start_date": "2026-04-22",
                "end_date": "2026-04-24",
            },
            cookies={"access_token": parent_token},
        )
        leave_id = client.get(
            "/api/parent/student-leaves", cookies={"access_token": parent_token}
        ).json()["items"][0]["id"]
        approve = client.post(
            f"/api/student-leaves/{leave_id}/approve",
            cookies={"access_token": admin_token},
        )
        # 4-22(三) + 4-24(五) = 2 天；4-23 為假日排除
        assert approve.json()["affected_days"] == 2
        with session_factory() as session:
            dates = sorted(
                r.date for r in session.query(StudentAttendance).all()
            )
            assert dates == [date(2026, 4, 22), date(2026, 4, 24)]


class TestCancelAndIdor:
    def test_cancel_pending(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            session.commit()
            token = _parent_token(user)
            student_id = student.id

        monday = _next_monday(date.today() + timedelta(days=7))
        client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_id,
                "leave_type": "事假",
                "start_date": monday.isoformat(),
                "end_date": monday.isoformat(),
            },
            cookies={"access_token": token},
        )
        leave_id = client.get(
            "/api/parent/student-leaves", cookies={"access_token": token}
        ).json()["items"][0]["id"]
        resp = client.post(
            f"/api/parent/student-leaves/{leave_id}/cancel",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        with session_factory() as session:
            row = (
                session.query(StudentLeaveRequest)
                .filter(StudentLeaveRequest.id == leave_id)
                .first()
            )
            assert row.status == "cancelled"

    def test_cannot_apply_for_other_child(self, leave_client):
        client, session_factory = leave_client
        with session_factory() as session:
            user_a, _, _, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A班"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B班"
            )
            session.commit()
            token_a = _parent_token(user_a)
            student_b_id = student_b.id

        monday = _next_monday(date.today() + timedelta(days=7))
        resp = client.post(
            "/api/parent/student-leaves",
            json={
                "student_id": student_b_id,
                "leave_type": "病假",
                "start_date": monday.isoformat(),
                "end_date": monday.isoformat(),
            },
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403
