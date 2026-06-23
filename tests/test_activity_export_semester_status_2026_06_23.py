"""tests/test_activity_export_semester_status_2026_06_23.py

兩個 P2 修補的回歸測試（2026-06-23 code review）：

1. 報名名單匯出忽略學期範圍：
   GET /registrations/export 原本沒有 school_year/semester 參數，
   _build_registration_filter_query 在學期為 None 時不過濾 → 畫面看 114-1
   匯出卻含所有 active 學期。修後補上參數並套 resolve_academic_term_filters
   （未給時預設當前學期，與列表端點一致）。

2. 報名名單匯出付款狀態仍是舊二分法：
   匯出只看 reg.is_paid 輸出「已繳費/未繳費」，部分繳費被匯成未繳、超繳被匯成
   已繳。修後改用 _batch_calc_total_amounts + _derive_payment_status 輸出五態
   label（與 payment-report 繳費總覽一致）。

SQLite 整合測試，不碰 dev DB。
"""

import io
import os
import sys

import openpyxl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import registrations_static as reg_static_mod
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    RegistrationCourse,
    User,
)
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "export_sem.sqlite"
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


def _admin(s):
    s.add(
        User(
            username="exp_admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )


def _login(c):
    r = c.post(
        "/api/auth/login", json={"username": "exp_admin", "password": "Temp123456"}
    )
    assert r.status_code == 200, r.text


def _make_reg(s, *, name, sy, sem, price, paid):
    course = ActivityCourse(
        name=f"課_{name}",
        price=price,
        capacity=30,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    s.add(course)
    s.flush()
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=paid,
        is_paid=(price > 0 and paid >= price),
        is_active=True,
        school_year=sy,
        semester=sem,
        remark="",
    )
    s.add(reg)
    s.flush()
    s.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=price,
        )
    )
    s.flush()
    return reg


def _load_rows(res):
    """讀回匯出的「報名名單」工作表資料列（去掉表頭）。"""
    wb = openpyxl.load_workbook(io.BytesIO(res.content))
    ws = wb["報名名單"]
    return list(ws.iter_rows(min_row=2, values_only=True))


# ── Finding 1：匯出依學期過濾 ──────────────────────────────────────────────


def test_export_filters_by_explicit_semester(client):
    """指定 school_year=114&semester=1 時，匯出只含該學期報名。"""
    c, sf = client
    with sf() as s:
        _admin(s)
        _make_reg(s, name="甲學期生", sy=114, sem=1, price=1000, paid=1000)
        _make_reg(s, name="乙學期生", sy=114, sem=2, price=1000, paid=1000)
        s.commit()
    _login(c)

    res = c.get("/api/activity/registrations/export?school_year=114&semester=1")
    assert res.status_code == 200, res.text
    names = {r[1] for r in _load_rows(res)}
    assert names == {"甲學期生"}, f"匯出應只含 114-1，實際：{names}"


def test_export_defaults_to_current_term(client):
    """不帶學期參數時，預設當前學期（與列表端點 resolve_academic_term_filters 一致），
    不再傾印所有 active 學期。"""
    c, sf = client
    sy, sem = resolve_current_academic_term()
    with sf() as s:
        _admin(s)
        _make_reg(s, name="本期生", sy=sy, sem=sem, price=1000, paid=1000)
        _make_reg(s, name="他期生", sy=sy - 1, sem=sem, price=1000, paid=1000)
        s.commit()
    _login(c)

    res = c.get("/api/activity/registrations/export")
    assert res.status_code == 200, res.text
    names = {r[1] for r in _load_rows(res)}
    assert names == {"本期生"}, f"無參數應預設當前學期，實際：{names}"


# ── Finding 2：匯出付款狀態五態 ────────────────────────────────────────────


def test_export_payment_status_five_states(client):
    """部分繳費 / 超額繳費 / 免繳 不再被壓成「已繳費/未繳費」二分法。"""
    c, sf = client
    with sf() as s:
        _admin(s)
        _make_reg(s, name="部分繳費生", sy=114, sem=1, price=1000, paid=500)
        _make_reg(s, name="超繳生", sy=114, sem=1, price=1000, paid=1500)
        _make_reg(s, name="免繳生", sy=114, sem=1, price=0, paid=0)
        _make_reg(s, name="繳清生", sy=114, sem=1, price=1000, paid=1000)
        s.commit()
    _login(c)

    res = c.get("/api/activity/registrations/export?school_year=114&semester=1")
    assert res.status_code == 200, res.text
    status_by_name = {r[1]: r[4] for r in _load_rows(res)}
    assert status_by_name["部分繳費生"] == "部分繳費", status_by_name
    assert status_by_name["超繳生"] == "超額繳費", status_by_name
    assert status_by_name["免繳生"] == "免繳", status_by_name
    assert status_by_name["繳清生"] == "已繳清", status_by_name
