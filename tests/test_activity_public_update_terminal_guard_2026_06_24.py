"""tests/test_activity_public_update_terminal_guard_2026_06_24.py

P2（2026-06-24 才藝模組稽核）：家長公開頁 /public/update 自助加課缺終態學生守衛。

情境：學生在校時公開報名並自動匹配（reg.student_id 綁定、pending_review=False）；
之後離校/畢業/轉出（Student.is_active=False，但 ActivityRegistration.is_active 仍為
True、報名列未軟刪）。家長持有效 query_token 在原報名上新增一門有名額的課程時，因
reg.pending_review=False 會跳過 re-match，stale student_id 原封保留，_attach_courses
直接寫 status='enrolled'，全程無 Student.is_active 檢查 → 長出「幽靈 enrolled」：
佔課程容量、灌水 enrollmentRate/total_enrollments、產生欠款，卻因讀取側
（_build_session_detail_response 等）以 Student.is_active IS True 過濾而永不出現在
點名名冊/出席統計。

此為 add_registration_course / confirm_waitlist_promotion / promote_waitlist /
_auto_promote_first_waitlist / restore_registration 五處既有終態守衛的唯一漏網路徑。
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
from api.activity import router as activity_router
from api.activity.public import (
    _public_query_limiter_instance,
    _public_register_limiter_instance,
)
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySupply,
    Base,
    Classroom,
    RegistrationCourse,
    Student,
)


@pytest.fixture
def client_and_sf(tmp_path):
    db_path = tmp_path / "terminal_guard.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed(session):
    sy, sem = _term()
    classroom = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
    session.add(classroom)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋", price=1000, school_year=sy, semester=sem, is_active=True
        )
    )
    session.add(
        ActivityCourse(
            name="畫畫", price=800, school_year=sy, semester=sem, is_active=True
        )
    )
    session.add(
        ActivitySupply(
            name="畫具", price=200, school_year=sy, semester=sem, is_active=True
        )
    )
    session.add(
        Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 5, 10),
            classroom_id=classroom.id,
            parent_phone="0912345678",
            is_active=True,
        )
    )
    session.commit()
    return classroom.id


def _register(client):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "0912345678",
            "class": "海豚班",
            "courses": [{"name": "圍棋", "price": "1000"}],
            "supplies": [],
        },
    )


def _query(client):
    return client.post(
        "/api/activity/public/query",
        json={"name": "王小明", "birthday": "2020-05-10", "parent_phone": "0912345678"},
    )


def _add_painting_course(client, *, reg_id, class_name, token):
    """在既有報名上新增「畫畫」課程（圍棋保留）。"""
    return client.post(
        "/api/activity/public/update",
        json={
            "id": reg_id,
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "0912345678",
            "class": class_name,
            "courses": [
                {"name": "圍棋", "price": "1000"},
                {"name": "畫畫", "price": "800"},
            ],
            "supplies": [],
            "query_token": token,
        },
    )


def test_public_update_blocks_adding_course_to_terminal_student(client_and_sf):
    """已離校/畢業/轉出（Student.is_active=False）學生的 matched 報名，
    家長自助加課應被拒（400），且不長出幽靈 enrolled。"""
    client, sf = client_and_sf
    with sf() as s:
        _seed(s)

    token = _register(client).json()["query_token"]
    q = _query(client).json()
    reg_id = q["id"]

    # 學生離校：Student.is_active=False（報名列 ActivityRegistration.is_active 仍 True）
    with sf() as s:
        student = s.query(Student).filter(Student.student_id == "S001").one()
        student.is_active = False
        s.commit()

    res = _add_painting_course(
        client, reg_id=reg_id, class_name=q["class_name"], token=token
    )

    assert res.status_code == 400, res.text
    assert "離校" in res.json()["detail"] or "無法追加" in res.json()["detail"]

    # 不可長出「畫畫」這筆幽靈 enrolled
    with sf() as s:
        painting = s.query(ActivityCourse).filter(ActivityCourse.name == "畫畫").one()
        phantom = (
            s.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg_id,
                RegistrationCourse.course_id == painting.id,
            )
            .first()
        )
        assert phantom is None, "終態學生不應長出新的 RegistrationCourse"


def test_public_update_allows_adding_course_to_active_student(client_and_sf):
    """守衛不可誤傷在籍學生：active 學生自助加課仍 200。"""
    client, sf = client_and_sf
    with sf() as s:
        _seed(s)

    token = _register(client).json()["query_token"]
    q = _query(client).json()

    res = _add_painting_course(
        client, reg_id=q["id"], class_name=q["class_name"], token=token
    )

    assert res.status_code == 200, res.text
    course_names = {c["name"] for c in res.json()["courses"]}
    assert course_names == {"圍棋", "畫畫"}
