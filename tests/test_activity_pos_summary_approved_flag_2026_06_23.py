"""P2-5 回歸（2026-06-23 深度 audit）：POS daily-summary 加 is_approved 旗標。

daily-summary 一律即時重算（compute_daily_snapshot）。已簽核日所有寫入路徑都被
_require_daily_close_unlocked 擋（live≡frozen），故數字不會分歧；但前端無從得知該日
是否已簽核（凍結權威值在 reconciliation）。加 is_approved 旗標讓前端可顯示「已簽核」
並在需要時切到 reconciliation 凍結值。
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
from models.database import ActivityPosDailyClose, Base, User
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "pos_approved_flag.sqlite"
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
    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _admin(session):
    session.add(
        User(
            username="pos_admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )
    session.commit()


def _login(c):
    return c.post(
        "/api/auth/login", json={"username": "pos_admin", "password": "TempPass123"}
    )


def test_daily_summary_is_approved_false_when_no_close(client):
    c, sf = client
    with sf() as s:
        _admin(s)
    assert _login(c).status_code == 200
    today = date.today().isoformat()
    res = c.get(f"/api/activity/pos/daily-summary?date={today}")
    assert res.status_code == 200, res.text
    assert res.json()["is_approved"] is False


def test_daily_summary_is_approved_true_when_closed(client):
    c, sf = client
    target = date.today()
    with sf() as s:
        _admin(s)
        s.add(
            ActivityPosDailyClose(
                close_date=target,
                approver_username="pos_admin",
                payment_total=0,
                refund_total=0,
                net_total=0,
                transaction_count=0,
                by_method_json="{}",
            )
        )
        s.commit()
    assert _login(c).status_code == 200
    res = c.get(f"/api/activity/pos/daily-summary?date={target.isoformat()}")
    assert res.status_code == 200, res.text
    assert res.json()["is_approved"] is True
