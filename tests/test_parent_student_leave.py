"""家長端學生請假整合測試（Batch 5）。"""

import os
import sys
from datetime import date, datetime, timedelta

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
    Guardian,
    Student,
    StudentAttendance,
    StudentLeaveRequest,
    User,
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
    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)

    # 覆寫 422 handler：Pydantic v2 field_validator raise ValueError 時
    # exc.errors() 帶 raw ValueError 物件，需 jsonable_encoder 轉換。
    # （production envelope handler latent bug，Phase 3 不在範圍內 fix，
    # 本 fixture 在地 patch 讓 leaves.py 的 422 path 可序列化。）
    @app.exception_handler(RequestValidationError)
    async def _patched_validation_handler(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "VALIDATION_ERROR",
                    "errors": jsonable_encoder(exc.errors()),
                }
            },
        )

    app.include_router(parent_portal_router)
    app.include_router(student_leaves_router)

    # Phase 1b+ (2026-05-18): leaves.py now uses Depends(get_parent_db). PG
    # RLS isolation lives in tests/spike_rls/; here we just yield a SQLite
    # session with the PG-equivalent commit/rollback lifecycle.
    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _setup_family(
    session, *, line_user_id="UF", student_name="小明", classroom_name="向日葵"
):
    user = User(
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
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
        permission_names=["*"],
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
            "permission_names": [],
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
            "permission_names": (
                user.permission_names if user.permission_names is not None else -1
            ),
            "token_version": user.token_version or 0,
        }
    )


# 全部用「足夠未來」的工作日避免週末干擾
def _next_monday(start: date) -> date:
    while start.weekday() != 0:
        start += timedelta(days=1)
    return start


class TestCreateLeave:
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

        overlap_body = {
            **body,
            "start_date": (monday + timedelta(days=1)).isoformat(),
            "end_date": (monday + timedelta(days=3)).isoformat(),
        }
        second = client.post(
            "/api/parent/student-leaves",
            json=overlap_body,
            cookies={"access_token": token},
        )
        assert second.status_code == 400


class TestCancelAndIdor:
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


def test_create_leave_auto_approves_and_writes_attendance(leave_client):
    client, session_factory = leave_client
    with session_factory() as s:
        user, _, student, _ = _setup_family(s)
        s.commit()
        token = _parent_token(user)
        student_id = student.id

    mon = _next_monday(date.today() + timedelta(days=7))
    tue = mon + timedelta(days=1)
    payload = {
        "student_id": student_id,
        "leave_type": "病假",
        "start_date": mon.isoformat(),
        "end_date": tue.isoformat(),
        "reason": "感冒",
    }
    res = client.post(
        "/api/parent/student-leaves",
        json=payload,
        cookies={"access_token": token},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "approved"
    assert body["reviewed_at"] is not None

    with session_factory() as s:
        rows = (
            s.query(StudentAttendance)
            .filter_by(student_id=student_id)
            .order_by(StudentAttendance.date)
            .all()
        )
        assert len(rows) == 2
        assert rows[0].date == mon
        assert rows[1].date == tue
        for r in rows:
            assert r.status == "病假"
            assert r.recorded_by is None
            assert r.remark == f"家長申請#{body['id']}"


def test_create_leave_rejects_overlap_with_existing_approved(leave_client):
    client, session_factory = leave_client
    mon = _next_monday(date.today() + timedelta(days=7))
    tue = mon + timedelta(days=1)
    wed = mon + timedelta(days=2)
    with session_factory() as s:
        user, _, student, _ = _setup_family(s)
        s.add(
            StudentLeaveRequest(
                student_id=student.id,
                applicant_user_id=user.id,
                leave_type="事假",
                start_date=mon,
                end_date=tue,
                status="approved",
                reviewed_at=datetime.now(),
            )
        )
        s.commit()
        token = _parent_token(user)
        sid = student.id

    res = client.post(
        "/api/parent/student-leaves",
        json={
            "student_id": sid,
            "leave_type": "病假",
            "start_date": tue.isoformat(),
            "end_date": wed.isoformat(),
        },
        cookies={"access_token": token},
    )
    assert res.status_code == 400
    assert "已成立" in res.json()["detail"]


def test_cancel_only_allowed_for_future_start_date(leave_client):
    client, session_factory = leave_client
    today = date.today()
    with session_factory() as s:
        user, _, student, _ = _setup_family(s)
        # 已開始（今日）的不可 cancel
        past = StudentLeaveRequest(
            student_id=student.id,
            applicant_user_id=user.id,
            leave_type="病假",
            start_date=today,
            end_date=today,
            status="approved",
            reviewed_at=datetime.now(),
        )
        # 未來的可 cancel
        future = StudentLeaveRequest(
            student_id=student.id,
            applicant_user_id=user.id,
            leave_type="事假",
            start_date=today + timedelta(days=3),
            end_date=today + timedelta(days=4),
            status="approved",
            reviewed_at=datetime.now(),
        )
        s.add_all([past, future])
        s.commit()
        token = _parent_token(user)
        past_id, future_id = past.id, future.id

    # 已開始 → 400
    r1 = client.post(
        f"/api/parent/student-leaves/{past_id}/cancel",
        cookies={"access_token": token},
    )
    assert r1.status_code == 400
    detail = r1.json()["detail"]
    assert "已開始" in detail or "無法取消" in detail

    # 未來 → 200，且 attendance 反向清除
    with session_factory() as s:
        from services.student_leave_service import apply_attendance_for_leave

        leave = s.query(StudentLeaveRequest).filter_by(id=future_id).one()
        apply_attendance_for_leave(s, leave)
        s.commit()
    r2 = client.post(
        f"/api/parent/student-leaves/{future_id}/cancel",
        cookies={"access_token": token},
    )
    assert r2.status_code == 200
    with session_factory() as s:
        rec = s.query(StudentLeaveRequest).filter_by(id=future_id).one()
        assert rec.status == "cancelled"
        atts = s.query(StudentAttendance).filter_by(student_id=rec.student_id).all()
        assert atts == []


def test_router_only_exposes_list_get(leave_client):
    """OpenAPI 契約測試：student-leaves router 只剩 GET，移除後不再有 approve/reject。"""
    client, _ = leave_client
    spec = client.get("/openapi.json").json()
    paths = {
        p: sorted(meta.keys())
        for p, meta in spec["paths"].items()
        if "/student-leaves" in p and not p.startswith("/api/parent")
    }
    assert paths == {"/api/student-leaves": ["get"]}, f"unexpected paths: {paths}"


def test_teacher_approve_endpoint_removed(leave_client):
    client, session_factory = leave_client
    with session_factory() as s:
        admin = _create_admin(s)
        s.commit()
        token = create_access_token(
            {
                "user_id": admin.id,
                "employee_id": None,
                "role": "admin",
                "name": admin.username,
                "permission_names": ["*"],
                "token_version": admin.token_version,
            }
        )
    # endpoint 不存在應回 404 或 405
    r = client.post(
        "/api/student-leaves/1/approve",
        cookies={"access_token": token},
    )
    assert r.status_code in (404, 405)


def test_teacher_list_default_returns_approved(leave_client):
    client, session_factory = leave_client
    with session_factory() as s:
        admin = _create_admin(s)
        user, _, student, _ = _setup_family(s)
        s.add(
            StudentLeaveRequest(
                student_id=student.id,
                applicant_user_id=user.id,
                leave_type="病假",
                start_date=date.today() + timedelta(days=3),
                end_date=date.today() + timedelta(days=3),
                status="approved",
                reviewed_at=datetime.now(),
            )
        )
        s.commit()
        token = create_access_token(
            {
                "user_id": admin.id,
                "employee_id": None,
                "role": "admin",
                "name": admin.username,
                "permission_names": ["*"],
                "token_version": admin.token_version,
            }
        )

    r = client.get(
        "/api/student-leaves",
        cookies={"access_token": token},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "approved"
