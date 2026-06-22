"""家長端登入報名須產生 query_token（code review #2，2026-06-22）。

問題：家長端登入報名（parent_portal register）建立的 ActivityRegistration 沒有寫
query_token_hash。公開破壞性 mutation 的身分驗證 `_parent_mutation_identity_ok` 對
「無 query_token_hash」報名退回姓名+生日+電話三欄驗證 → 知道這三項 PII 的陌生人可
未登入修改課程或放棄候補。

修法：家長端報名比照公開報名（public_register）產生明文 token、寫入 query_token_hash
+ query_token_issued_at，並把明文 token 回傳給家長（供管理連結）。此後該報名公開
mutation 強制有效 token，三欄 PII 不再足夠。

DB 隔離：SQLite + monkeypatch base_module + 家長 RLS UDF override（不碰 dev PG）。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity.public import _parent_mutation_identity_ok
from api.parent_portal import parent_router as parent_portal_router
from models.activity import ActivityCourse, ActivityRegistration
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token

PARENT_PHONE = "0911000111"


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "parent_token.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
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
    base_module._SessionFactory = old_sf
    db_engine.dispose()


def _setup_family(session):
    user = User(
        username="parent_line_UA",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id="UA",
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = Classroom(name="活力班", is_active=True)
    session.add(classroom)
    session.flush()
    student = Student(
        student_id="S_活",
        name="阿活",
        birthday=date(2020, 3, 1),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="父親",
        phone=PARENT_PHONE,
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, student


def _create_course(session, *, name="繪畫", price=2000):
    course = ActivityCourse(
        name=name,
        price=price,
        capacity=10,
        school_year=115,
        semester=1,
        allow_waitlist=True,
        is_active=True,
    )
    session.add(course)
    session.flush()
    return course


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


def _register(client, *, student_id, course_id, token):
    return client.post(
        "/api/parent/activity/register",
        json={
            "student_id": student_id,
            "school_year": 115,
            "semester": 1,
            "course_ids": [course_id],
            "supply_ids": [],
        },
        cookies={"access_token": token},
    )


class TestParentRegisterQueryToken:
    def test_register_response_returns_query_token(self, activity_client):
        client, sf = activity_client
        with sf() as s:
            user, student = _setup_family(s)
            course = _create_course(s)
            s.commit()
            tok = _parent_token(user)
            sid, cid = student.id, course.id

        resp = _register(client, student_id=sid, course_id=cid, token=tok)
        assert resp.status_code == 201, resp.text
        assert resp.json().get("query_token"), resp.text

    def test_register_persists_query_token_hash(self, activity_client):
        client, sf = activity_client
        with sf() as s:
            user, student = _setup_family(s)
            course = _create_course(s)
            s.commit()
            tok = _parent_token(user)
            sid, cid = student.id, course.id

        _register(client, student_id=sid, course_id=cid, token=tok)
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(student_id=sid).first()
            assert reg.query_token_hash is not None
            assert reg.query_token_issued_at is not None

    def test_pii_only_no_longer_authorizes_public_mutation(self, activity_client):
        """安全核心：三欄 PII（無 token）不再能對家長端報名做公開 mutation。"""
        client, sf = activity_client
        with sf() as s:
            user, student = _setup_family(s)
            course = _create_course(s)
            s.commit()
            tok = _parent_token(user)
            sid, cid = student.id, course.id

        _register(client, student_id=sid, course_id=cid, token=tok)
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(student_id=sid).first()
            assert (
                _parent_mutation_identity_ok(
                    reg, "阿活", "2020-03-01", PARENT_PHONE, query_token=None
                )
                is False
            )

    def test_returned_token_authorizes_public_mutation(self, activity_client):
        client, sf = activity_client
        with sf() as s:
            user, student = _setup_family(s)
            course = _create_course(s)
            s.commit()
            tok = _parent_token(user)
            sid, cid = student.id, course.id

        token_plain = _register(
            client, student_id=sid, course_id=cid, token=tok
        ).json()["query_token"]
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(student_id=sid).first()
            assert (
                _parent_mutation_identity_ok(
                    reg, "阿活", "2020-03-01", PARENT_PHONE, query_token=token_plain
                )
                is True
            )
