"""家長端才藝課程資訊補強（Quick Win A，2026-06-22）。

檢視 finding #2：家長端課程卡只有價格/堂數/容量，缺上課星期/時間/適齡，
且 my-registrations 未帶 weekday/time 導致前端無法做衝堂偵測。

這些欄位 model 早已有（Phase 3，models/activity.py:62-66），公開端
（/public/courses）也已暴露，只有登入家長端 list_courses / my-registrations
沒帶出來。本批把家長端 response 補齊（time 序列化 "HH:MM"，對齊公開端），
順帶給這兩個端點 + register 補 response_model（解契約黑洞）。

策略：純資料曝露，不改報名邏輯；適齡/衝堂為前端 advisory（model 設計：
警告不擋報名）。
"""

import os
import sys
from datetime import time

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
    RegistrationCourse,
)
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "activity_info.sqlite"
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


class TestListCoursesScheduleAgeFields:
    def test_list_courses_exposes_schedule_and_age(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _ = _setup_family(session)
            session.add(
                ActivityCourse(
                    name="繪畫",
                    price=2000,
                    capacity=10,
                    school_year=115,
                    semester=1,
                    is_active=True,
                    min_age_months=36,
                    max_age_months=72,
                    meeting_weekday=2,  # 週三
                    meeting_start_time=time(15, 30),
                    meeting_end_time=time(16, 30),
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/courses",
            params={"school_year": 115, "semester": 1},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["min_age_months"] == 36
        assert item["max_age_months"] == 72
        assert item["meeting_weekday"] == 2
        # 時間序列化為 "HH:MM"（對齊公開端 /public/courses）
        assert item["meeting_start_time"] == "15:30"
        assert item["meeting_end_time"] == "16:30"

    def test_list_courses_null_schedule_fields_are_null(self, activity_client):
        # 向後相容：既有課程未填新欄位 → null，不可炸。
        client, session_factory = activity_client
        with session_factory() as session:
            user, _ = _setup_family(session)
            session.add(
                ActivityCourse(
                    name="舊課",
                    price=1000,
                    capacity=10,
                    school_year=115,
                    semester=1,
                    is_active=True,
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/courses",
            params={"school_year": 115, "semester": 1},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["min_age_months"] is None
        assert item["max_age_months"] is None
        assert item["meeting_weekday"] is None
        assert item["meeting_start_time"] is None
        assert item["meeting_end_time"] is None


class TestMyRegistrationsScheduleFields:
    def test_my_registrations_course_block_includes_schedule(self, activity_client):
        # 衝堂偵測需要：my-registrations 每門課帶 weekday/time，前端才能比對
        # 已報名課程 vs 目錄課程時段。
        client, session_factory = activity_client
        with session_factory() as session:
            user, student = _setup_family(session)
            course = ActivityCourse(
                name="鋼琴",
                price=3000,
                capacity=10,
                school_year=115,
                semester=1,
                is_active=True,
                meeting_weekday=4,  # 週五
                meeting_start_time=time(16, 0),
                meeting_end_time=time(17, 0),
            )
            session.add(course)
            session.flush()
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
                    status="enrolled",
                    price_snapshot=3000,
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/my-registrations",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        c = resp.json()["items"][0]["courses"][0]
        assert c["meeting_weekday"] == 4
        assert c["meeting_start_time"] == "16:00"
        assert c["meeting_end_time"] == "17:00"


class TestResponseModelDeclared:
    """補 response_model 後 OpenAPI 應有具名 schema（非裸 dict），
    前端 codegen 才能產出真型別而非 unknown。"""

    def test_parent_course_list_has_named_response_schema(self):
        from api.parent_portal.activity import list_courses

        # FastAPI 端點以 response_model 宣告契約後，函式被 router 裝飾時會掛在
        # router.routes 上；此處直接驗證端點函式有對應的 response_model 設定。
        from api.parent_portal.activity import router

        route = next(
            r
            for r in router.routes
            if getattr(r, "path", None) == "/activity/courses"
            and "GET" in getattr(r, "methods", set())
        )
        assert route.response_model is not None, "list_courses 應宣告 response_model"

    def test_my_registrations_has_named_response_schema(self):
        from api.parent_portal.activity import router

        route = next(
            r
            for r in router.routes
            if getattr(r, "path", None) == "/activity/my-registrations"
            and "GET" in getattr(r, "methods", set())
        )
        assert (
            route.response_model is not None
        ), "my_registrations 應宣告 response_model"
