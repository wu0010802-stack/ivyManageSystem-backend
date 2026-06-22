"""tests/test_activity_attendance_audit.py

才藝點名端點顯式稽核覆蓋測試。

問題：AuditMiddleware 的 ENTITY_PATTERNS 不涵蓋 /api/activity/attendance/* 路徑
（pattern 為 /api/activity/sessions，對不上實際的 /api/activity/attendance/sessions）。
delete_session 已顯式 write_explicit_audit，但以下端點漏了：
- POST /attendance/sessions          建立場次（P2）
- POST /attendance/sessions/batch    批次建立場次（P2，一次最多 60 場）
- GET  /attendance/sessions/{id}/export    匯出點名 Excel（P1，批次 PII 下載）
- GET  /attendance/sessions/{id}/roll.pdf  點名單 PDF（P1，批次 PII 下載）

修正後：四端點均落 AuditLog（誰、哪課、哪日 / 幾名學生），與 delete_session 對齊。
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
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.audit import AuditLog
from models.database import ActivityCourse, ActivitySession, Base
from tests.test_activity_pos import _create_admin, _login, _setup_reg
from datetime import date


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "attendance_audit.sqlite"
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


def _audits(sf, *, action=None, entity_type="activity_session"):
    with sf() as s:
        q = s.query(AuditLog).filter(AuditLog.entity_type == entity_type)
        if action is not None:
            q = q.filter(AuditLog.action == action)
        return q.all()


def _course_id(sf, name="美術") -> int:
    with sf() as s:
        return s.query(ActivityCourse).filter_by(name=name).first().id


def _make_session(sf, course_id: int) -> int:
    with sf() as s:
        sess = ActivitySession(course_id=course_id, session_date=date(2026, 5, 6))
        s.add(sess)
        s.commit()
        return sess.id


# ── P2：建立場次 ────────────────────────────────────────────────────────────


def test_create_session_writes_audit(client):
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        _setup_reg(s)  # 建出課程「美術」
        s.commit()
    cid = _course_id(sf)

    _login(c)
    resp = c.post(
        "/api/activity/attendance/sessions",
        json={"course_id": cid, "session_date": "2026-05-06"},
    )
    assert resp.status_code == 200, resp.json()
    new_id = resp.json()["id"]

    audits = _audits(sf, action="CREATE")
    assert any(a.entity_id == str(new_id) for a in audits), [
        (a.action, a.entity_id, a.summary) for a in audits
    ]


def test_create_sessions_batch_writes_audit(client):
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        _setup_reg(s)
        s.commit()
    cid = _course_id(sf)

    _login(c)
    resp = c.post(
        "/api/activity/attendance/sessions/batch",
        json={
            "course_id": cid,
            "start_date": "2026-05-04",  # 週一
            "end_date": "2026-05-18",
            "weekday": 0,
        },
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["created_count"] >= 1
    audits = _audits(sf, action="CREATE")
    assert len(audits) >= 1, "批次建立場次未留稽核"


# ── P1：匯出 / 點名單 PDF（批次 PII 下載）──────────────────────────────────


def test_export_session_attendance_writes_audit(client):
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        _setup_reg(s)
        s.commit()
    cid = _course_id(sf)
    sid = _make_session(sf, cid)

    _login(c)
    resp = c.get(f"/api/activity/attendance/sessions/{sid}/export")
    assert resp.status_code == 200, resp.text
    audits = _audits(sf, action="EXPORT")
    assert any(a.entity_id == str(sid) for a in audits), "匯出 Excel 未留稽核"


def test_print_session_roll_pdf_writes_audit(client):
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        _setup_reg(s)
        s.commit()
    cid = _course_id(sf)
    sid = _make_session(sf, cid)

    _login(c)
    resp = c.get(f"/api/activity/attendance/sessions/{sid}/roll.pdf")
    assert resp.status_code == 200, resp.text
    audits = _audits(sf, action="EXPORT")
    assert any(a.entity_id == str(sid) for a in audits), "點名單 PDF 未留稽核"
