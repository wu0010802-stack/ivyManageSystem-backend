"""POS refund diff verify e2e 測試（TestClient）。

對應 spec §8 + §11：
- diff <= 100 → pass
- diff > 100 + 一線員工 → 403
- diff > 100 + ACTIVITY_PAYMENT_APPROVE → pass
- 多 reg 同收據 sum(abs(per-reg-diff)) 累加
- 方向抵消防護：reg1 多 60 + reg2 少 60 → diff=120 簽核
- 用品實退觸發 diff
- NULL sessions reg 採 amount_due fallback
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivitySession,
    Base,
)
from tests.test_activity_pos import _create_admin, _login, _setup_reg


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "refund_diff.sqlite"
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


REFUND_REASON = "家長要求退費，已確認原因符合園所政策。"


def _set_course_sessions(session, course_name: str, sessions: int):
    """把 _setup_reg 預設建出來 sessions=NULL 的 course 補上 sessions。"""
    c = session.query(ActivityCourse).filter(ActivityCourse.name == course_name).first()
    c.sessions = sessions
    session.flush()


def _mark_attendance(session, reg_id: int, course_id: int, n: int):
    for i in range(n):
        s = ActivitySession(course_id=course_id, session_date=date(2026, 5, i + 1))
        session.add(s)
        session.flush()
        session.add(
            ActivityAttendance(
                session_id=s.id,
                registration_id=reg_id,
                is_present=True,
            )
        )
    session.flush()


def _refund_body(reg_id: int, amount: int) -> dict:
    return {
        "items": [{"registration_id": reg_id, "amount": amount}],
        "payment_method": "現金",
        "payment_date": "2026-05-26",
        "type": "refund",
        "notes": REFUND_REASON,
    }


# ── happy paths ────────────────────────────────────────────────────────────


def test_refund_diff_zero_passes_staff(client):
    """員工剛好送 suggested → diff=0 → 一線通過。
    用 course_price=800（<= REFUND_APPROVAL_THRESHOLD=1000）確保守衛一不介入。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        # 不需 _mark_attendance（n=0 attendance 即「未開課」全退）
        s.commit()
        reg_id = reg.id

    _login(c)
    # 0 attendance → suggested=800（not_started 特例全退）；diff=0 → 通過
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 800))
    assert resp.status_code in (200, 201), resp.json()


def test_refund_diff_below_threshold_passes_staff(client):
    """diff=50（< 100）→ 一線通過。
    用 course_price=800 確保守衛一不介入（750 <= 1000）。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    # 0 attendance → suggested=800；員工送 750 → diff=50
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 750))
    assert resp.status_code in (200, 201), resp.json()


def test_refund_diff_over_threshold_blocks_staff(client):
    """diff=200（> 100）+ 無 approve → 403。
    用 course_price=800 確保守衛一不介入（500 <= 1000）；diff=300 > 100 → 守衛三觸發。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    # suggested=800（全退）；員工送 500 → diff=300 > 100 → 守衛三觸發
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 500))
    assert resp.status_code == 403
    assert "偏離" in resp.json()["detail"] or "差" in resp.json()["detail"]


def test_refund_diff_over_threshold_passes_approver(client):
    """diff=300 + ACTIVITY_PAYMENT_APPROVE → pass（守衛三略過）。
    pos_checkout 成功回 201 Created。"""
    c, sf = client
    with sf() as s:
        _create_admin(
            s,
            permission_names=[
                "ACTIVITY_READ",
                "ACTIVITY_WRITE",
                "ACTIVITY_PAYMENT_APPROVE",
            ],
        )
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    # suggested=800（全退）；員工送 500 → diff=300 > 100，但 approver 略過
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 500))
    assert resp.status_code in (200, 201), resp.json()


def test_refund_supply_triggers_diff(client):
    """suggested=0（用品 + course 全退）員工只想退 supply NT$300 → diff 觸發
    （因 course 0 attendance suggested=1500，員工只送 300 → diff=1200）。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=500, paid_amount=2000)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 300))
    assert resp.status_code == 403


def test_refund_null_sessions_uses_amount_due_fallback(client):
    """course.sessions=NULL → suggested 用 amount_due fallback；
    員工少退觸發 diff。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        # _setup_reg 預設不設 course.sessions → NULL
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        s.commit()
        reg_id = reg.id

    _login(c)
    # NULL fallback: suggested=1500；員工送 1000 → diff=500 → 簽核
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 1000))
    assert resp.status_code == 403


def test_refund_multi_reg_diff_accumulates(client):
    """多 reg 同收據：reg1 多退 60 + reg2 少退 60 →
    naive abs(total)=0；spec 算法 sum(abs)=120 → 簽核。
    用 course_price=400 確保收據合計 (460+340=800) <= 1000，守衛一不介入。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg1 = _setup_reg(
            s,
            student_name="A",
            course_price=400,
            supply_price=0,
            paid_amount=400,
            course_name="A 課",
        )
        reg2 = _setup_reg(
            s,
            student_name="B",
            course_price=400,
            supply_price=0,
            paid_amount=400,
            course_name="B 課",
        )
        _set_course_sessions(s, "A 課", 10)
        _set_course_sessions(s, "B 課", 10)
        s.commit()
        rid1, rid2 = reg1.id, reg2.id

    _login(c)
    # 兩 reg suggested 都是 400（0 attendance 全退）
    # reg1 actual=460（+60）；reg2 actual=340（-60）
    # total_actual=800=total_suggested → naive diff=0
    # sum(abs) = 60+60 = 120 → 守衛三觸發
    body = {
        "items": [
            {"registration_id": rid1, "amount": 460},
            {"registration_id": rid2, "amount": 340},
        ],
        "payment_method": "現金",
        "payment_date": "2026-05-26",
        "type": "refund",
        "notes": REFUND_REASON,
    }
    resp = c.post("/api/activity/pos/checkout", json=body)
    assert resp.status_code == 403
    assert "120" in resp.json()["detail"]
