"""
test_pos_idempotent_voided.py — 驗證 POS checkout idempotent replay 排除 voided 紀錄。

對齊 spec C5（2026-05-06 稽核報告）：voided 紀錄不可被當作 replay 結果回傳，
否則客戶端會誤以為交易仍生效，造成漏收。
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
from models.database import ActivityPaymentRecord, Base
from utils.permissions import Permission

# 引用既有 helper
from tests.test_activity_pos import (
    _add_payment,  # noqa: F401
    _create_admin,
    _login,
    _make_reg_minimal,
    _setup_reg,
)

APPROVE_PERMS = (
    Permission.ACTIVITY_READ
    | Permission.ACTIVITY_WRITE
    | Permission.ACTIVITY_PAYMENT_APPROVE
)


@pytest.fixture
def void_client(tmp_path):
    """提供 client + session_factory；admin 預設帶 ACTIVITY_PAYMENT_APPROVE 以便 void。"""
    db_path = tmp_path / "voided_replay.sqlite"
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


def _checkout_with_key(client, reg_id, idk, amount=500):
    """對 reg_id 收款 amount 並帶 idempotency_key。"""
    return client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": amount}],
            "payment_date": date.today().isoformat(),
            "type": "payment",
            "idempotency_key": idk,
        },
    )


def test_replay_after_void_returns_409(void_client):
    """收款 → void → 同 key 重送 → 409，不可被當 replay 通過。"""
    client, sf = void_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="王測試", paid_amount=0)
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200

    # 收款一筆，帶 idempotency_key
    idk = "voidedreplay_aaaaa12345"
    res = _checkout_with_key(client, reg_id, idk, amount=500)
    assert res.status_code == 201, res.text
    payment_id = res.json()["items"][0][
        "registration_id"
    ]  # placeholder; will fetch from DB

    # 從 DB 拉到剛建立的 payment_id（response 不直接給）
    with sf() as s:
        rec = (
            s.query(ActivityPaymentRecord)
            .filter(ActivityPaymentRecord.idempotency_key == idk)
            .first()
        )
        assert rec is not None
        payment_id = rec.id

    # void 該筆 payment（需要 ACTIVITY_PAYMENT_APPROVE）
    res = client.request(
        "DELETE",
        f"/api/activity/registrations/{reg_id}/payments/{payment_id}",
        json={"reason": "誤刷退款測試 spec C5"},
    )
    assert res.status_code == 200, res.text

    # 同 key 重送 → 409（不應 replay 已 voided 紀錄）
    res = _checkout_with_key(client, reg_id, idk, amount=500)
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert "idempotency_key" in detail or "作廢" in detail
    # 確保 detail 不洩漏 receipt_no 或金額（避免讓客戶端誤判）
    assert "POS-" not in detail


def test_replay_normal_unchanged(void_client):
    """收款 → 同 key 重送（未 void） → 200 + replay 標記。"""
    client, sf = void_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="李測試", paid_amount=0)
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200

    idk = "normalreplay_bbbbb67890"
    res1 = _checkout_with_key(client, reg_id, idk, amount=500)
    assert res1.status_code == 201, res1.text
    receipt_no_first = res1.json()["receipt_no"]

    # 同 key 重送 → 應為 idempotent replay 200
    res2 = _checkout_with_key(client, reg_id, idk, amount=500)
    assert res2.status_code == 201, res2.text  # checkout 端點固定 201
    body = res2.json()
    assert body["receipt_no"] == receipt_no_first
    assert body.get("idempotent_replay") is True


def test_voided_replay_does_not_double_charge(void_client):
    """收款 → void → 同 key 重送 → 409 確保未產生新 payment（避免 paid_amount 被誤更新）。"""
    client, sf = void_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="陳測試", paid_amount=0)
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200

    idk = "doublecharge_ccccc54321"
    res = _checkout_with_key(client, reg_id, idk, amount=500)
    assert res.status_code == 201

    with sf() as s:
        rec = (
            s.query(ActivityPaymentRecord)
            .filter(ActivityPaymentRecord.idempotency_key == idk)
            .first()
        )
        payment_id = rec.id

    res = client.request(
        "DELETE",
        f"/api/activity/registrations/{reg_id}/payments/{payment_id}",
        json={"reason": "誤刷退款測試 spec C5 案例二"},
    )
    assert res.status_code == 200

    # 此時 reg.paid_amount 應已歸零（軟刪重算）
    with sf() as s:
        from models.database import ActivityRegistration

        reg_after = s.query(ActivityRegistration).filter_by(id=reg_id).one()
        assert reg_after.paid_amount == 0

    # 同 key 重送 → 409
    res = _checkout_with_key(client, reg_id, idk, amount=500)
    assert res.status_code == 409

    # 確認沒有新 payment 紀錄被建立（key 唯一，且 voided 那筆仍在）
    with sf() as s:
        count = (
            s.query(ActivityPaymentRecord)
            .filter(ActivityPaymentRecord.idempotency_key == idk)
            .count()
        )
        assert count == 1  # 只有原始那筆 voided
