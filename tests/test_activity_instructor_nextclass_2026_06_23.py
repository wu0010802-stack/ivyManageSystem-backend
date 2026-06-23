"""才藝課程卡：講師 instructor_name + 下次上課 next_session_date（第3波/雜項，2026-06-23）。

review finding #2 課程卡剩餘兩項：
- 講師：ActivityCourse.instructor_name（自由字串；才藝老師常外聘故非 employee FK）
- 下次上課：_next_session_dates 取各課 >= 今日最早 ActivitySession（GROUP BY min，非 N+1）

catalog（公開端 /public/courses、家長端 list_courses）皆暴露此二欄；admin create/update/
list/detail 可設定/檢視 instructor。
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
from api.activity._shared import _next_session_dates
from models.activity import ActivityCourse, ActivitySession
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token
from utils.taipei_time import today_taipei


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "instr.sqlite"
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


def _setup_parent(session, line_user_id="UA"):
    user = User(
        username=f"p_{line_user_id}",
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
        student_id=f"S_{line_user_id}",
        name="童",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    session.add(
        Guardian(
            student_id=student.id,
            user_id=user.id,
            name="父",
            phone="0911",
            relation="父",
            is_primary=True,
        )
    )
    session.flush()
    return user


def _token(user):
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permission_names": [],
            "token_version": 0,
        }
    )


def _course(session, **kw):
    d = dict(
        name="繪畫",
        price=2000,
        capacity=10,
        school_year=115,
        semester=1,
        is_active=True,
    )
    d.update(kw)
    c = ActivityCourse(**d)
    session.add(c)
    session.flush()
    return c


class TestNextSessionHelper:
    def test_picks_earliest_future_session(self, activity_client):
        _, sf = activity_client
        with sf() as s:
            c = _course(s)
            today = today_taipei()
            s.add(
                ActivitySession(course_id=c.id, session_date=today - timedelta(days=2))
            )
            s.add(
                ActivitySession(course_id=c.id, session_date=today + timedelta(days=5))
            )
            s.add(
                ActivitySession(course_id=c.id, session_date=today + timedelta(days=1))
            )
            s.commit()
            cid = c.id
            m = _next_session_dates(s, [cid])
        assert m[cid] == (today + timedelta(days=1)).isoformat()  # 最早未來場次

    def test_no_future_session_absent(self, activity_client):
        _, sf = activity_client
        with sf() as s:
            c = _course(s)
            s.add(
                ActivitySession(
                    course_id=c.id, session_date=today_taipei() - timedelta(days=1)
                )
            )
            s.commit()
            cid = c.id
            m = _next_session_dates(s, [cid])
        assert cid not in m  # 只有過去場次 → 無下次上課

    def test_empty_ids(self, activity_client):
        _, sf = activity_client
        with sf() as s:
            assert _next_session_dates(s, []) == {}


class TestParentListCoursesInstructorNextClass:
    def test_list_courses_exposes_instructor_and_next_session(self, activity_client):
        client, sf = activity_client
        with sf() as s:
            user = _setup_parent(s)
            c = _course(
                s,
                name="鋼琴",
                instructor_name="王老師",
                meeting_weekday=2,
                meeting_start_time=time(15, 30),
                meeting_end_time=time(16, 30),
            )
            s.add(
                ActivitySession(
                    course_id=c.id, session_date=today_taipei() + timedelta(days=3)
                )
            )
            s.commit()
            token = _token(user)
            nxt = (today_taipei() + timedelta(days=3)).isoformat()

        resp = client.get(
            "/api/parent/activity/courses",
            params={"school_year": 115, "semester": 1},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["instructor_name"] == "王老師"
        assert item["next_session_date"] == nxt

    def test_list_courses_null_instructor_and_no_sessions(self, activity_client):
        client, sf = activity_client
        with sf() as s:
            user = _setup_parent(s)
            _course(s, name="無講師課")
            s.commit()
            token = _token(user)

        resp = client.get(
            "/api/parent/activity/courses",
            params={"school_year": 115, "semester": 1},
            cookies={"access_token": token},
        )
        item = resp.json()["items"][0]
        assert item["instructor_name"] is None
        assert item["next_session_date"] is None
