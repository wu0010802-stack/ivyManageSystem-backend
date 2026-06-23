"""P2-1 回歸（2026-06-23 深度 audit）：點名 Excel 匯出 / 點名單 roll PDF / 儀表板 Excel
匯出三個端點漏掛 _export_limiter（同 package 的 registrations export / payment-report 已掛）。
持 ACTIVITY_READ 者可高頻重打 openpyxl/reportlab 全名單生成 → 資源耗盡 + 大量 PII 無限流。

修：三端點掛上與 registrations_static 共用的 _export_limiter（5/60s）。

limiter 為 dependency、在 handler 前執行並計數，故第 6 次必 429（不論 handler 結果）。
SQLite 整合測試，不碰 dev DB。
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
from api.activity import registrations_static as reg_static_mod
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivitySession,
    Base,
    Classroom,  # noqa: F401 metadata
    User,
)
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "export_rl.sqlite"
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
    reg_static_mod._export_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    with TestClient(app) as c:
        yield c, session_factory

    reg_static_mod._export_limiter_instance._timestamps.clear()
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed(session):
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    session.add(
        User(
            username="exp_admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )
    course = ActivityCourse(
        name="圍棋",
        price=1000,
        capacity=30,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    session.add(course)
    session.flush()
    sess = ActivitySession(course_id=course.id, session_date=date(2026, 3, 1))
    session.add(sess)
    session.commit()
    return sess.id, sy, sem


def _login(c):
    r = c.post(
        "/api/auth/login", json={"username": "exp_admin", "password": "Temp123456"}
    )
    assert r.status_code == 200, r.text


def _assert_sixth_is_429(c, url):
    """前 5 次放行、第 6 次 429（limiter=5/60s）。"""
    reg_static_mod._export_limiter_instance._timestamps.clear()
    for i in range(5):
        res = c.get(url)
        assert res.status_code != 429, f"第 {i + 1} 次不應被限流：{res.status_code}"
    res = c.get(url)
    assert (
        res.status_code == 429
    ), f"第 6 次應被限流，實得 {res.status_code}：{res.text}"


def test_attendance_session_export_rate_limited(client):
    c, sf = client
    with sf() as s:
        sess_id, _, _ = _seed(s)
    _login(c)
    _assert_sixth_is_429(c, f"/api/activity/attendance/sessions/{sess_id}/export")


def test_attendance_roll_pdf_rate_limited(client):
    c, sf = client
    with sf() as s:
        sess_id, _, _ = _seed(s)
    _login(c)
    _assert_sixth_is_429(c, f"/api/activity/attendance/sessions/{sess_id}/roll.pdf")


def test_dashboard_table_export_rate_limited(client):
    c, sf = client
    with sf() as s:
        _, sy, sem = _seed(s)
    _login(c)
    _assert_sixth_is_429(
        c, f"/api/activity/dashboard-table/export?school_year={sy}&semester={sem}"
    )
