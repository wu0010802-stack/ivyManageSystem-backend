"""tests/test_activity_sessions_term_filter_2026_06_29.py

P1（2026-06-29 才藝點名稽核）：場次列表端點忽略學期。

場次（ActivitySession）本身無學期欄位，學期繼承自其課程（ActivityCourse）。
原 list_sessions 完全不依學期過濾 → 切到 114-1 學期仍列出 113-2 場次，
操作員可能在當前學期畫面誤編輯/刪除舊學期場次。

修補：list_sessions 接受 school_year/semester（optional），經已 join 的
ActivityCourse 過濾。未帶參數時行為不變（列全部，向後相容）。
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
from models.base import Base
from models.activity import ActivityCourse, ActivitySession
from models.database import Employee  # noqa: F401 metadata
from models.classroom import Classroom, Student  # noqa: F401 metadata
from api.activity import router as activity_router
from api.auth import router as auth_router, _account_failures, _ip_attempts
from tests.test_activity_pos import _create_admin, _login


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "sessions_term.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_two_term_sessions(sf):
    """114-1 與 113-2 各一門同名課程、各一場次。回傳 (new_session_id, old_session_id)。"""
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        c_new = ActivityCourse(
            name="繪畫", price=1000, school_year=114, semester=1, is_active=True
        )
        c_old = ActivityCourse(
            name="繪畫", price=1000, school_year=113, semester=2, is_active=True
        )
        s.add_all([c_new, c_old])
        s.flush()
        sess_new = ActivitySession(
            course_id=c_new.id, session_date=date(2025, 9, 1), created_by="t"
        )
        sess_old = ActivitySession(
            course_id=c_old.id, session_date=date(2025, 3, 1), created_by="t"
        )
        s.add_all([sess_new, sess_old])
        s.flush()
        ids = (sess_new.id, sess_old.id)
        s.commit()
    return ids


def test_sessions_filtered_by_term(client):
    """帶 school_year+semester → 只回該學期（依課程學期）的場次。"""
    c, sf = client
    new_id, old_id = _seed_two_term_sessions(sf)
    assert _login(c).status_code == 200

    r = c.get(
        "/api/activity/attendance/sessions",
        params={"school_year": 114, "semester": 1},
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    returned_ids = {it["id"] for it in items}
    assert returned_ids == {new_id}, f"應只回 114-1 場次，實得 {returned_ids}"


def test_sessions_without_term_lists_all(client):
    """未帶學期 → 行為不變，列出全部（向後相容）。"""
    c, sf = client
    new_id, old_id = _seed_two_term_sessions(sf)
    assert _login(c).status_code == 200

    r = c.get("/api/activity/attendance/sessions")
    assert r.status_code == 200, r.text
    returned_ids = {it["id"] for it in r.json()["items"]}
    assert returned_ids == {new_id, old_id}
