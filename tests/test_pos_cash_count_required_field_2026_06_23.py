"""tests/test_pos_cash_count_required_field_2026_06_23.py

P2 修補（2026-06-23 code review）：POS 日結盤點門檻前後端語意不同。

後端 approve 用「現金毛流量」（payment_gross + refund_gross）判斷是否強制盤點，
但 GET /pos/daily-close/{date} 回應的 by_method 只是淨額（payment - refund），
前端只能拿到淨額去判門檻 → 例：收現 10,000 + 退現 8,000，淨額 2,000 < 3,000
前端放行送 actual_cash_count=null，後端毛流量 18,000 >= 3,000 卻 400，
老闆在確認後才失敗。

修法：GET 回應補一個後端權威的 cash_count_required: bool（由毛流量門檻計算），
前端直接用該欄位決定是否必填，不再自行從淨額推算。

SQLite 整合測試，不碰 dev DB。
"""

import os
import sys
from datetime import date, datetime

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
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    Base,
    RegistrationCourse,
)

from tests.test_activity_pos import _create_admin, _login

APPROVE_PERMS = ["ACTIVITY_READ", "ACTIVITY_WRITE", "ACTIVITY_PAYMENT_APPROVE"]


@pytest.fixture
def threshold_client(tmp_path):
    db_path = tmp_path / "cash_required.sqlite"
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
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _insert_payment(s, reg_id, amount, rec_type="payment"):
    s.add(
        ActivityPaymentRecord(
            registration_id=reg_id,
            type=rec_type,
            amount=amount,
            payment_date=date.today(),
            payment_method="現金",
            operator="pos_admin",
            notes="",
            created_at=datetime.now(),
        )
    )


def _make_reg(s, name="測試生"):
    course = s.query(ActivityCourse).filter(ActivityCourse.name == "測試課").first()
    if not course:
        course = ActivityCourse(
            name="測試課",
            price=10000,
            capacity=30,
            allow_waitlist=True,
            school_year=114,
            semester=1,
        )
        s.add(course)
        s.flush()
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=0,
        is_paid=False,
        is_active=True,
        school_year=114,
        semester=1,
    )
    s.add(reg)
    s.flush()
    s.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=10000,
        )
    )
    s.flush()
    return reg


# ── 毛流量高、淨額低 → cash_count_required 應為 True（與後端 approve 門檻一致）──


def test_preview_exposes_cash_count_required_true(threshold_client):
    """收現 10,000 + 退現 8,000：淨額 2,000、毛流量 18,000。
    GET preview 的 cash_count_required 應為 True（前端據此必填），
    by_method 現金仍是淨額 2,000（不改顯示口徑）。"""
    client, sf = threshold_client
    target = date.today().isoformat()

    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _make_reg(s, "毛流量測試生")
        _insert_payment(s, reg.id, 10000, "payment")
        _insert_payment(s, reg.id, 8000, "refund")
        s.commit()

    assert _login(client).status_code == 200

    res = client.get(f"/api/activity/pos/daily-close/{target}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert (
        body["cash_count_required"] is True
    ), f"毛流量 18,000 >= 門檻，cash_count_required 應為 True，實際：{body}"
    # 淨額顯示口徑不變
    assert body["by_method"].get("現金") == 2000, body


# ── 毛流量低 → cash_count_required 應為 False ───────────────────────────────


def test_preview_cash_count_required_false_low_flow(threshold_client):
    """收現 1,000（淨額/毛流量皆 1,000 < 門檻）→ cash_count_required 為 False。"""
    client, sf = threshold_client
    target = date.today().isoformat()

    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _make_reg(s, "低流量測試生")
        _insert_payment(s, reg.id, 1000, "payment")
        s.commit()

    assert _login(client).status_code == 200

    res = client.get(f"/api/activity/pos/daily-close/{target}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert (
        body["cash_count_required"] is False
    ), f"毛流量 1,000 < 門檻，cash_count_required 應為 False，實際：{body}"
