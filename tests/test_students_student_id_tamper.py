"""PDPA follow-up Task B1（RA-L6）：legacy 學號不可由 client 竄改。

`student_id` 是 server 端顯示快取（由 models/student_events before_flush listener
依 enrollment_seq 重算），不該由 client 設值。問題：listener 對 enrollment_seq
IS NULL 的 legacy 學生跳過重算，導致 update_student 直接持久化 client 傳入的
偽造 student_id。

修：update_student 忽略 client 傳入的 student_id（對所有學生）。
- legacy（enrollment_seq=None）→ 偽造值被忽略，原 student_id 不變。
- 非 legacy（enrollment_seq 有值）→ 本就被 listener 覆寫回算出值。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.students import router as students_router
from models.database import Base, Classroom, Student, User
from utils.auth import hash_password


@pytest.fixture
def students_client(tmp_path):
    db_path = tmp_path / "students_tamper.sqlite"
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

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(students_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin(session):
    u = User(
        username="admin_t",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permission_names=["STUDENTS_WRITE", "STUDENTS_READ"],
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "admin_t", "password": "Passw0rd!"}
    )


def test_legacy_null_seq_student_id_not_persisted(students_client):
    """legacy（enrollment_seq=None）PUT 偽造 student_id → 不持久化。"""
    client, sf = students_client
    with sf() as s:
        _create_admin(s)
        cls = Classroom(name="大班A", is_active=True)
        s.add(cls)
        s.flush()
        # legacy：enrollment_seq 留 None → listener 跳過重算
        stu = Student(
            student_id="LEGACY-001",
            name="王小明",
            classroom_id=cls.id,
            is_active=True,
            enrollment_seq=None,
        )
        s.add(stu)
        s.commit()
        sid = stu.id

    assert _login(client).status_code == 200
    res = client.put(
        f"/api/students/{sid}",
        json={"student_id": "HACKED-999", "name": "王小明改"},
    )
    assert res.status_code == 200, res.text

    with sf() as s:
        stu = s.query(Student).filter(Student.id == sid).first()
        assert (
            stu.student_id == "LEGACY-001"
        ), f"client 傳入的 student_id 不該被持久化，但拿到 {stu.student_id}"
        # 其他欄位照常更新
        assert stu.name == "王小明改"


def test_non_legacy_student_id_overwritten_by_listener(students_client):
    """非 legacy（enrollment_seq 有值）PUT 偽造 student_id → 被 listener 覆寫回算出值。"""
    client, sf = students_client
    with sf() as s:
        _create_admin(s)
        cls = Classroom(name="大班A", is_active=True)
        s.add(cls)
        s.flush()
        stu = Student(
            student_id="OLD-CACHE",
            name="李小華",
            classroom_id=cls.id,
            is_active=True,
            enrollment_seq=5,
            enrollment_school_year=113,
        )
        s.add(stu)
        s.commit()
        sid = stu.id
        # listener 已在 commit 時算出顯示值
        canonical = stu.student_id

    assert _login(client).status_code == 200
    res = client.put(
        f"/api/students/{sid}",
        json={"student_id": "HACKED-999"},
    )
    assert res.status_code == 200, res.text

    with sf() as s:
        stu = s.query(Student).filter(Student.id == sid).first()
        assert stu.student_id != "HACKED-999", "偽造值不可持久化"
        assert (
            stu.student_id == canonical
        ), f"listener 應覆寫回算出值 {canonical}，但拿到 {stu.student_id}"
