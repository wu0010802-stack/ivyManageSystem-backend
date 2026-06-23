"""家長端「即將上課」場次端點（第2波，2026-06-23）。

finding #2：家長端 hero upcomingCount 固定為 0（A 波註解：course response 無 start_date
先設 0）。正解非補 course.start_date（model 無此欄），而是查 ActivitySession（逐場
session_date）。本端點回家長子女「已佔位」（enrolled / promoted_pending）課程在
未來 days 天內的場次，前端據此算 upcomingCount（7 天內）與「下次上課」。

唯讀；候補課程不算（未佔位）；只回自己子女；過去場次排除。
"""

import os
import sys
from datetime import time, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router as parent_portal_router
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    RegistrationCourse,
)
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "upcoming.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_portal_router)

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import (
        make_sqlite_parent_db_override,
        register_sqlite_parent_rls_udfs,
    )

    register_sqlite_parent_rls_udfs(db_engine)
    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _setup_family(session, *, line_user_id="UA", student_name="阿活"):
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
    classroom = Classroom(name=f"班_{line_user_id}", is_active=True)
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
    session.add(
        Guardian(
            student_id=student.id,
            user_id=user.id,
            name="父親",
            phone="0911000111",
            relation="父親",
            is_primary=True,
        )
    )
    session.flush()
    return user, student


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


def _enroll(session, student, *, course, status="enrolled"):
    reg = ActivityRegistration(
        student_name=student.name,
        is_active=True,
        school_year=115,
        semester=1,
        student_id=student.id,
        parent_phone="0911000111",
        pending_review=False,
        match_status="manual",
    )
    session.add(reg)
    session.flush()
    session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status=status,
            price_snapshot=course.price,
        )
    )
    session.flush()
    return reg


def _course(session, **kw):
    defaults = dict(
        name="繪畫",
        price=2000,
        capacity=10,
        school_year=115,
        semester=1,
        is_active=True,
        meeting_weekday=2,
        meeting_start_time=time(15, 30),
        meeting_end_time=time(16, 30),
    )
    defaults.update(kw)
    c = ActivityCourse(**defaults)
    session.add(c)
    session.flush()
    return c


def _session_on(session, course, days_from_today):
    today = now_taipei_naive().date()
    s = ActivitySession(
        course_id=course.id, session_date=today + timedelta(days=days_from_today)
    )
    session.add(s)
    session.flush()
    return s


class TestUpcomingSessions:
    def test_returns_future_enrolled_sessions_with_course_info(self, activity_client):
        client, sf = activity_client
        with sf() as session:
            user, student = _setup_family(session)
            c = _course(session, name="繪畫")
            _enroll(session, student, course=c, status="enrolled")
            _session_on(session, c, 0)  # 今天
            _session_on(session, c, 3)  # 3 天後
            session.commit()
            token = _parent_token(user)
            sid, sname = student.id, student.name  # session 關閉前擷取，避免 detached

        resp = client.get(
            "/api/parent/activity/upcoming-sessions",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        item = body["items"][0]
        assert item["course_name"] == "繪畫"
        assert item["student_id"] == sid
        assert item["student_name"] == sname
        assert item["meeting_weekday"] == 2
        assert item["meeting_start_time"] == "15:30"
        assert item["meeting_end_time"] == "16:30"
        # 依日期升冪
        dates = [i["session_date"] for i in body["items"]]
        assert dates == sorted(dates)

    def test_excludes_past_sessions(self, activity_client):
        client, sf = activity_client
        with sf() as session:
            user, student = _setup_family(session)
            c = _course(session)
            _enroll(session, student, course=c)
            _session_on(session, c, -1)  # 昨天
            _session_on(session, c, 2)  # 後天
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/upcoming-sessions", cookies={"access_token": token}
        )
        assert resp.json()["total"] == 1

    def test_excludes_waitlist_courses(self, activity_client):
        # 候補（未佔位）課程的場次不算「即將上課」。
        client, sf = activity_client
        with sf() as session:
            user, student = _setup_family(session)
            c = _course(session)
            _enroll(session, student, course=c, status="waitlist")
            _session_on(session, c, 1)
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/upcoming-sessions", cookies={"access_token": token}
        )
        assert resp.json()["total"] == 0

    def test_promoted_pending_counts(self, activity_client):
        # promoted_pending 已佔位 → 算。
        client, sf = activity_client
        with sf() as session:
            user, student = _setup_family(session)
            c = _course(session)
            _enroll(session, student, course=c, status="promoted_pending")
            _session_on(session, c, 1)
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/upcoming-sessions", cookies={"access_token": token}
        )
        assert resp.json()["total"] == 1

    def test_only_own_children(self, activity_client):
        client, sf = activity_client
        with sf() as session:
            user_a, _ = _setup_family(session, line_user_id="UA", student_name="A")
            _, student_b = _setup_family(session, line_user_id="UB", student_name="B")
            c = _course(session)
            _enroll(session, student_b, course=c)  # 別人的小孩
            _session_on(session, c, 1)
            session.commit()
            token_a = _parent_token(user_a)

        resp = client.get(
            "/api/parent/activity/upcoming-sessions", cookies={"access_token": token_a}
        )
        assert resp.json()["total"] == 0

    def test_days_window_filters(self, activity_client):
        client, sf = activity_client
        with sf() as session:
            user, student = _setup_family(session)
            c = _course(session)
            _enroll(session, student, course=c)
            _session_on(session, c, 3)  # 窗內
            _session_on(session, c, 40)  # 窗外（days=7）
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/upcoming-sessions",
            params={"days": 7},
            cookies={"access_token": token},
        )
        assert resp.json()["total"] == 1

    def test_response_model_declared(self):
        from api.parent_portal.activity import router

        route = next(
            r
            for r in router.routes
            if getattr(r, "path", None) == "/activity/upcoming-sessions"
            and "GET" in getattr(r, "methods", set())
        )
        assert route.response_model is not None
