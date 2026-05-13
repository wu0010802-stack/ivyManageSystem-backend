"""教師端 GET /api/portal/search 測試。

涵蓋：
- 短 q（< 2 char）回空
- RBAC：teacher 只搜自己班學生
- 終態學生排除
- Guardian phone 一律 mask
- contact_book snippet strip HTML
- 每 section LIMIT 5
- SQL injection-safe
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.portal import router as portal_router
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
)
from models.database import (
    Base,
    Classroom,
    Employee,
    Guardian,
    Student,
    StudentContactBookEntry,
    User,
)
from models.event import Announcement
from models.parent_message import ParentMessage, ParentMessageThread
from utils.auth import create_access_token
from utils.permissions import Permission


@pytest.fixture
def client_and_session(tmp_path):
    db_path = tmp_path / "search.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    import models.base as base_module

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf

    app = FastAPI()
    app.include_router(portal_router)
    client = TestClient(app)
    sess = sf()
    yield client, sess
    sess.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


_teacher_counter = 0


def _seed_teacher(sess, classroom: Classroom):
    global _teacher_counter
    _teacher_counter += 1
    emp = Employee(
        employee_id=f"T{_teacher_counter:03d}", name="王老師", is_active=True
    )
    sess.add(emp)
    sess.flush()
    classroom.head_teacher_id = emp.id
    sess.flush()
    user = User(
        username="wang",
        password_hash="x",
        role="teacher",
        employee_id=emp.id,
        permissions=int(Permission.PARENT_MESSAGES_WRITE),
        is_active=True,
        token_version=0,
    )
    sess.add(user)
    sess.flush()
    token = create_access_token(
        {
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "employee_id": emp.id,
            "permissions": user.permissions,
            "token_version": 0,
        }
    )
    return emp, user, token


def test_short_q_returns_empty(client_and_session):
    client, sess = client_and_session
    classroom = Classroom(name="A", is_active=True)
    sess.add(classroom)
    sess.flush()
    _, _, token = _seed_teacher(sess, classroom)
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=a",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["students"] == []
    assert data["guardians"] == []
    assert data["messages"] == []
    assert data["contact_book"] == []
    assert data["announcements"] == []


def test_returns_own_class_students_only(client_and_session):
    client, sess = client_and_session
    cr_a = Classroom(name="A班", is_active=True)
    cr_b = Classroom(name="B班", is_active=True)
    sess.add_all([cr_a, cr_b])
    sess.flush()
    s_a = Student(
        student_id="A1",
        name="小明",
        classroom_id=cr_a.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    s_b = Student(
        student_id="B1",
        name="小明乙",
        classroom_id=cr_b.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add_all([s_a, s_b])
    sess.flush()
    _, _, token = _seed_teacher(sess, cr_a)
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=小明",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    names = {r["name"] for r in resp.json()["students"]}
    assert "小明" in names
    assert "小明乙" not in names


def test_excludes_terminal_students(client_and_session):
    client, sess = client_and_session
    cr = Classroom(name="A", is_active=True)
    sess.add(cr)
    sess.flush()
    active = Student(
        student_id="A1",
        name="小明",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    grad = Student(
        student_id="A2",
        name="小明畢業",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_GRADUATED,
    )
    sess.add_all([active, grad])
    sess.flush()
    _, _, token = _seed_teacher(sess, cr)
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=小明",
        headers={"Authorization": f"Bearer {token}"},
    )
    names = {r["name"] for r in resp.json()["students"]}
    assert "小明" in names
    assert "小明畢業" not in names


def test_guardian_phone_masked(client_and_session):
    client, sess = client_and_session
    cr = Classroom(name="A", is_active=True)
    sess.add(cr)
    sess.flush()
    student = Student(
        student_id="A1",
        name="小華",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(student)
    sess.flush()
    guardian = Guardian(
        student_id=student.id,
        name="王媽媽",
        phone="0912345678",
        relation="mother",
        is_primary=True,
    )
    sess.add(guardian)
    sess.flush()
    _, _, token = _seed_teacher(sess, cr)
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=王媽媽",
        headers={"Authorization": f"Bearer {token}"},
    )
    guardians = resp.json()["guardians"]
    assert len(guardians) == 1
    assert "*" in guardians[0]["phone_masked"]
    assert "0912345678" != guardians[0]["phone_masked"]


def test_contact_book_snippet_strips_html(client_and_session):
    client, sess = client_and_session
    cr = Classroom(name="A", is_active=True)
    sess.add(cr)
    sess.flush()
    student = Student(
        student_id="A1",
        name="小華",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(student)
    sess.flush()
    entry = StudentContactBookEntry(
        student_id=student.id,
        classroom_id=cr.id,
        log_date=date(2026, 5, 10),
        teacher_note="<p>今天<b>午睡</b>很久</p>",
        learning_highlight="<em>認得 ABC</em>",
        published_at=datetime(2026, 5, 10, 18, 0),
    )
    sess.add(entry)
    sess.flush()
    _, _, token = _seed_teacher(sess, cr)
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=午睡",
        headers={"Authorization": f"Bearer {token}"},
    )
    entries = resp.json()["contact_book"]
    assert len(entries) == 1
    snippet = entries[0]["snippet"]
    assert "午睡" in snippet
    assert "<p>" not in snippet
    assert "<b>" not in snippet


def test_each_section_limited_to_5(client_and_session):
    client, sess = client_and_session
    cr = Classroom(name="A", is_active=True)
    sess.add(cr)
    sess.flush()
    for i in range(10):
        s = Student(
            student_id=f"A{i}",
            name=f"測試學生{i}",
            classroom_id=cr.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
    sess.flush()
    _, _, token = _seed_teacher(sess, cr)
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=測試",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert len(resp.json()["students"]) == 5


def test_sql_injection_safe(client_and_session):
    client, sess = client_and_session
    cr = Classroom(name="A", is_active=True)
    sess.add(cr)
    sess.flush()
    student = Student(
        student_id="A1",
        name="正常學生",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(student)
    sess.flush()
    _, _, token = _seed_teacher(sess, cr)
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=%' OR 1=1--",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json()["students"], list)


def test_messages_only_returns_own_threads(client_and_session):
    """Teacher A 搜不到 teacher B 的 thread。"""
    client, sess = client_and_session
    cr = Classroom(name="A", is_active=True)
    sess.add(cr)
    sess.flush()
    student = Student(
        student_id="A1",
        name="小明",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(student)
    sess.flush()

    # Teacher A
    emp_a, user_a, token_a = _seed_teacher(sess, cr)
    # 為了同一個學生建一個 thread 給 user_a 與 一個給 user_b
    # 另造 user_b 同班教師 (不衝突)
    emp_b = Employee(name="B老師", is_active=True, employee_id="T002")
    sess.add(emp_b)
    sess.flush()
    user_b = User(
        username="bee",
        password_hash="x",
        role="teacher",
        employee_id=emp_b.id,
        permissions=int(Permission.PARENT_MESSAGES_WRITE),
        is_active=True,
        token_version=0,
    )
    sess.add(user_b)
    sess.flush()
    parent_user = User(
        username="parent1",
        password_hash="x",
        role="parent",
        is_active=True,
        token_version=0,
    )
    sess.add(parent_user)
    sess.flush()
    thread_a = ParentMessageThread(
        parent_user_id=parent_user.id,
        teacher_user_id=user_a.id,
        student_id=student.id,
    )
    thread_b = ParentMessageThread(
        parent_user_id=parent_user.id,
        teacher_user_id=user_b.id,
        student_id=student.id,
    )
    sess.add_all([thread_a, thread_b])
    sess.flush()
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=小明",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    msg_thread_ids = {m["thread_id"] for m in resp.json()["messages"]}
    assert thread_a.id in msg_thread_ids
    assert thread_b.id not in msg_thread_ids


def test_message_snippet_strips_html(client_and_session):
    """ParentMessage.body 的 HTML 在 snippet 應該被 strip。"""
    client, sess = client_and_session
    cr = Classroom(name="A", is_active=True)
    sess.add(cr)
    sess.flush()
    student = Student(
        student_id="A1",
        name="小華",
        classroom_id=cr.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(student)
    sess.flush()
    _, user, token = _seed_teacher(sess, cr)
    parent_user = User(
        username="parent2",
        password_hash="x",
        role="parent",
        is_active=True,
        token_version=0,
    )
    sess.add(parent_user)
    sess.flush()
    thread = ParentMessageThread(
        parent_user_id=parent_user.id,
        teacher_user_id=user.id,
        student_id=student.id,
    )
    sess.add(thread)
    sess.flush()
    msg = ParentMessage(
        thread_id=thread.id,
        sender_user_id=user.id,
        sender_role="teacher",
        body="<p>明天的<b>戶外</b>活動</p>",
    )
    sess.add(msg)
    sess.flush()
    sess.commit()

    resp = client.get(
        "/api/portal/search?q=戶外",
        headers={"Authorization": f"Bearer {token}"},
    )
    messages = resp.json()["messages"]
    assert len(messages) == 1
    snippet = messages[0]["snippet"]
    assert "戶外" in snippet
    assert "<p>" not in snippet
    assert "<b>" not in snippet
