"""
test_pos_cash_only.py — 驗證 POS 系列端點僅接受現金。

對齊 spec: docs/superpowers/specs/2026-05-06-pos-cash-only-design.md
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
from models.database import Base
from utils.permissions import Permission

# 直接引用現有 helper（私有函式，但可從同套件 import）
from tests.test_activity_pos import (
    _create_admin,
    _login,
    _setup_reg,
)

# ── Fixture ──────────────────────────────────────────────────────────────


@pytest.fixture
def pos_cash_client(tmp_path):
    """提供已登入 client + session_factory（同 pos_client 模式）。"""
    db_path = tmp_path / "pos_cash.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
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


# ── 測試 ─────────────────────────────────────────────────────────────────


def test_pos_checkout_rejects_transfer(pos_cash_client):
    """payment_method='轉帳' 應被 Pydantic Literal 驗證拒絕，回傳 422。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生甲")
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_method": "轉帳",
            "payment_date": date.today().isoformat(),
            "type": "payment",
        },
    )
    assert res.status_code == 422, res.text


def test_pos_checkout_rejects_other(pos_cash_client):
    """payment_method='其他' 應被 Pydantic Literal 驗證拒絕，回傳 422。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生乙")
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_method": "其他",
            "payment_date": date.today().isoformat(),
            "type": "payment",
        },
    )
    assert res.status_code == 422, res.text


def test_pos_checkout_default_cash_succeeds(pos_cash_client):
    """不傳 payment_method 應預設『現金』成功，回應 201 且 payment_method='現金'。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生丙")
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 2000}],
            "payment_date": date.today().isoformat(),
            "type": "payment",
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["payment_method"] == "現金"


def test_pos_checkout_explicit_cash_succeeds(pos_cash_client):
    """明確傳入 payment_method='現金' 應成功，回應 201。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生丁")
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 2000}],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "type": "payment",
        },
    )
    assert res.status_code == 201, res.text


# ── Task 2: registrations.py 三端點收口 ───────────────────────────────────


def test_add_registration_payment_rejects_transfer(pos_cash_client):
    """POST /registrations/{id}/payments 拒絕『轉帳』。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生戊")
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        f"/api/activity/registrations/{reg_id}/payments",
        json={
            "type": "payment",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "payment_method": "轉帳",
            "notes": "test",
        },
    )
    assert res.status_code == 422, res.text


def test_add_registration_payment_default_cash(pos_cash_client):
    """POST /registrations/{id}/payments 不傳 payment_method 應預設『現金』成功。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生己")
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        f"/api/activity/registrations/{reg_id}/payments",
        json={
            "type": "payment",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "notes": "test",
        },
    )
    assert res.status_code in (200, 201), res.text


def test_update_payment_paid_rejects_transfer(pos_cash_client):
    """PUT /registrations/{id}/payment 標記 is_paid=True 補齊欠費，
    payment_method='轉帳' 應在 schema 層被拒（422）。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生庚")
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.put(
        f"/api/activity/registrations/{reg_id}/payment",
        json={
            "is_paid": True,
            "payment_method": "轉帳",
            "payment_reason": "家長已付（測試補齊用）",
        },
    )
    assert res.status_code == 422, res.text


# ── Task 3: MIN_REFUND_REASON_LENGTH 5 → 15 ─────────────────────────────


def test_pos_refund_rejects_14_char_reason(pos_cash_client):
    """退費備註 14 字應被 400 拒絕（門檻 ≥ 15 字）。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生辛", paid_amount=2000, is_paid=True)
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 100}],
            "payment_date": date.today().isoformat(),
            "type": "refund",
            "notes": "x" * 14,
        },
    )
    assert res.status_code == 400, res.text
    assert "15" in res.json()["detail"]


def test_pos_refund_accepts_15_char_reason(pos_cash_client):
    """退費備註恰 15 字應通過。"""
    client, sf = pos_cash_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="測試生壬", paid_amount=2000, is_paid=True)
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 100}],
            "payment_date": date.today().isoformat(),
            "type": "refund",
            "notes": "家長要求退費學費調整事由說明清楚",  # 15 字
        },
    )
    assert res.status_code == 201, res.text


# ── Task 4: 簽核盤點門檻守衛（NT$3,000） ─────────────────────────────────


def _approve_admin(session):
    """建立具 ACTIVITY_PAYMENT_APPROVE 權限的管理員（簽核日結需要）。"""
    return _create_admin(
        session,
        permissions=Permission.ACTIVITY_READ
        | Permission.ACTIVITY_WRITE
        | Permission.ACTIVITY_PAYMENT_APPROVE,
    )


def test_daily_close_below_threshold_skips_cash_count(pos_cash_client):
    """淨現金 < NT$3,000 時，actual_cash_count 可省略。"""
    client, sf = pos_cash_client
    target = date.today().isoformat()
    with sf() as s:
        _approve_admin(s)
        reg = _setup_reg(s, student_name="測試生癸", course_price=2000, supply_price=0)
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    # 收 2,000（< 3,000 門檻）
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 2000}],
            "payment_date": target,
        },
    )
    assert res.status_code == 201, res.text

    # 不傳 actual_cash_count 應通過
    res = client.post(
        f"/api/activity/pos/daily-close/{target}",
        json={"note": "small day"},
    )
    assert res.status_code == 201, res.text


def test_daily_close_at_threshold_requires_cash_count(pos_cash_client):
    """淨現金 ≥ NT$3,000 不傳 actual_cash_count 時，回 400。"""
    client, sf = pos_cash_client
    target = date.today().isoformat()
    with sf() as s:
        _approve_admin(s)
        reg = _setup_reg(
            s, student_name="測試生甲二", course_price=3000, supply_price=0
        )
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 3000}],
            "payment_date": target,
        },
    )
    assert res.status_code == 201, res.text

    res = client.post(
        f"/api/activity/pos/daily-close/{target}",
        json={"note": "no count"},
    )
    assert res.status_code == 400, res.text
    assert "盤點" in res.json()["detail"] or "actual_cash_count" in res.json()["detail"]


def test_daily_close_at_threshold_with_cash_count_succeeds(pos_cash_client):
    """淨現金 ≥ NT$3,000 且填 actual_cash_count → 201，cash_variance 計算正確。"""
    client, sf = pos_cash_client
    target = date.today().isoformat()
    with sf() as s:
        _approve_admin(s)
        reg = _setup_reg(
            s, student_name="測試生乙二", course_price=3500, supply_price=0
        )
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 3500}],
            "payment_date": target,
        },
    )
    assert res.status_code == 201, res.text

    res = client.post(
        f"/api/activity/pos/daily-close/{target}",
        json={"note": "ok", "actual_cash_count": 3500},
    )
    assert res.status_code == 201, res.text
    assert res.json()["cash_variance"] == 0
