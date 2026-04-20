"""tests/test_student_change_logs_classroom_filter.py

驗證 /api/students/change-logs 的 classroom_id filter 同時比對
classroom_id / from_classroom_id / to_classroom_id（OR 語意），
並涵蓋 summary、export、權限守衛。
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.student_change_logs import router as change_log_router
from models.database import Base, Classroom, Student, User
from models.student_log import StudentChangeLog
from utils.auth import hash_password
from utils.permissions import Permission

SCHOOL_YEAR = 114
SEMESTER = 2


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "change-logs.sqlite"
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
    app.include_router(change_log_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _add_user(
    session,
    username="admin",
    password="TempPass123",
    perms=Permission.STUDENTS_READ | Permission.STUDENTS_WRITE,
):
    u = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=perms,
        is_active=True,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username="admin", password="TempPass123"):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200
    return r


def _seed(session):
    """
    三個班級 A/B/C、三位學生 s1/s2/s3，四筆 log：
      1. s1 入班 A (classroom_id=A)
      2. s1 轉出 A→B (from=A, to=B, classroom_id 留空代表「仍在異動中」)
      3. s2 入班 B (classroom_id=B)
      4. s3 入班 C (classroom_id=C)
    """
    _add_user(session)
    a = Classroom(
        name="A班", school_year=SCHOOL_YEAR, semester=SEMESTER, is_active=True
    )
    b = Classroom(
        name="B班", school_year=SCHOOL_YEAR, semester=SEMESTER, is_active=True
    )
    c = Classroom(
        name="C班", school_year=SCHOOL_YEAR, semester=SEMESTER, is_active=True
    )
    session.add_all([a, b, c])
    session.flush()

    s1 = Student(student_id="S1", name="小花", classroom_id=b.id, is_active=True)
    s2 = Student(student_id="S2", name="小明", classroom_id=b.id, is_active=True)
    s3 = Student(student_id="S3", name="小華", classroom_id=c.id, is_active=True)
    session.add_all([s1, s2, s3])
    session.flush()

    session.add_all(
        [
            StudentChangeLog(
                student_id=s1.id,
                school_year=SCHOOL_YEAR,
                semester=SEMESTER,
                event_type="入學",
                event_date=date(2026, 2, 1),
                classroom_id=a.id,
                reason="新生報名",
            ),
            StudentChangeLog(
                student_id=s1.id,
                school_year=SCHOOL_YEAR,
                semester=SEMESTER,
                event_type="轉出",
                event_date=date(2026, 3, 15),
                from_classroom_id=a.id,
                to_classroom_id=b.id,
                reason="家庭因素",
            ),
            StudentChangeLog(
                student_id=s2.id,
                school_year=SCHOOL_YEAR,
                semester=SEMESTER,
                event_type="入學",
                event_date=date(2026, 2, 1),
                classroom_id=b.id,
            ),
            StudentChangeLog(
                student_id=s3.id,
                school_year=SCHOOL_YEAR,
                semester=SEMESTER,
                event_type="入學",
                event_date=date(2026, 2, 5),
                classroom_id=c.id,
                notes="=CMD('rm -rf')",  # CSV injection 測試用 payload
            ),
        ]
    )
    session.commit()
    return {"a": a.id, "b": b.id, "c": c.id, "s1": s1.id, "s2": s2.id, "s3": s3.id}


def _params(**kw):
    return {"school_year": SCHOOL_YEAR, "semester": SEMESTER, **kw}


class TestClassroomFilter:
    def test_list_classroom_a_matches_enter_and_transfer_out(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/students/change-logs", params=_params(classroom_id=ids["a"])
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 2
        event_types = sorted(item["event_type"] for item in body["items"])
        assert event_types == ["入學", "轉出"]
        # 轉出事件應帶上 from_classroom_name = A班
        transfer = next(i for i in body["items"] if i["event_type"] == "轉出")
        assert transfer["from_classroom_name"] == "A班"
        assert transfer["to_classroom_name"] == "B班"

    def test_list_classroom_b_matches_transfer_in_and_enter(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/students/change-logs", params=_params(classroom_id=ids["b"])
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 2
        event_types = sorted(item["event_type"] for item in body["items"])
        assert event_types == ["入學", "轉出"]

    def test_list_classroom_c_isolated(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/students/change-logs", params=_params(classroom_id=ids["c"])
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["items"][0]["student_name"] == "小華"

    def test_list_with_pagination_params_matches_live_request(self, client_with_db):
        """完整模擬前端發的 request：帶 page / page_size。"""
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/students/change-logs",
            params={
                "school_year": SCHOOL_YEAR,
                "semester": SEMESTER,
                "classroom_id": ids["a"],
                "page": 1,
                "page_size": 50,
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["total"] == 2


class TestSummary:
    def test_summary_with_classroom_id(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/students/change-logs/summary",
            params=_params(classroom_id=ids["a"]),
        )
        assert res.status_code == 200
        body = res.json()
        assert body["classroom_id"] == ids["a"]
        assert body["total"] == 2
        assert body["summary"]["入學"] == 1
        assert body["summary"]["轉出"] == 1
        assert body["summary"]["畢業"] == 0


class TestExport:
    def test_export_csv_classroom_a(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/students/change-logs/export",
            params=_params(classroom_id=ids["a"]),
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/csv")
        body_text = res.content.decode("utf-8-sig")
        lines = [ln for ln in body_text.splitlines() if ln.strip()]
        # header + 2 rows
        assert len(lines) == 3

    def test_export_csv_escapes_formula_injection(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/students/change-logs/export",
            params=_params(classroom_id=ids["c"]),
        )
        assert res.status_code == 200
        body_text = res.content.decode("utf-8-sig")
        # notes 欄位原本開頭是 = ，應被前綴單引號避免 Excel 當公式執行
        assert "'=CMD('rm -rf')" in body_text
        assert ",=CMD" not in body_text  # 不該有未跳脫的 =


class TestPermission:
    def test_without_students_read_returns_403(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            _seed(s)
            # 覆寫 admin 權限，移除 STUDENTS_READ
            u = s.query(User).filter(User.username == "admin").first()
            u.permissions = Permission.ATTENDANCE_READ
            s.commit()
        _login(client)

        res = client.get("/api/students/change-logs", params=_params())
        assert res.status_code == 403
