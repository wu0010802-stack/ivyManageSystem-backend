"""tests/test_activity_void_negative_ledger_2026_06_29.py

稽核（2026-06-29，業主裁示：採納提案）：作廢付款若使金流淨額 < 0
（有效退費 > 有效付款）須「拒絕作廢」，要求先作廢相關退費，而非舊版的
「允許作廢 + warning + 把 cached paid_amount 夾到 0」。

問題情境：付款 1000、退費 800，作廢「付款」後 → 有效付款 0 − 有效退費 800
= 淨額 −800；舊版僅 logger.warning + reg.paid_amount = max(0, −800) = 0，
留下孤兒退費，ledger（財報分別加總付款/退費）與 cached paid_amount 對不上，
兩套帳不一致。

修補：void 重算後若 new_paid < 0 → raise 400，付款不作廢、paid_amount 不變。
正確處理流程：先作廢退費，再作廢付款。

DB 隔離：SQLite + monkeypatch base_module（不碰 dev PG）。
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
from models.database import ActivityPaymentRecord, ActivityRegistration, Base

from tests.test_activity_pos import _create_admin, _login, _setup_reg

APPROVE_PERMS = ["ACTIVITY_READ", "ACTIVITY_WRITE", "ACTIVITY_PAYMENT_APPROVE"]


@pytest.fixture
def void_client(tmp_path):
    db_path = tmp_path / "void_negative_ledger.sqlite"
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


def _seed_record(session, reg_id, *, ptype, amount):
    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type=ptype,
        amount=amount,
        payment_date=date.today(),
        payment_method="現金",
        notes=f"{ptype} NT${amount}",
        operator="seed_admin",
    )
    session.add(rec)
    session.flush()
    return rec.id


def _void(client, reg_id, payment_id, reason="家長改用銀行轉帳退款，需重整金流"):
    return client.request(
        "DELETE",
        f"/api/activity/registrations/{reg_id}/payments/{payment_id}",
        json={"reason": reason},
    )


def _payment_active(sf, payment_id):
    with sf() as s:
        rec = s.get(ActivityPaymentRecord, payment_id)
        return rec is not None and rec.voided_at is None


def _paid_amount(sf, reg_id):
    with sf() as s:
        return s.get(ActivityRegistration, reg_id).paid_amount


# ── 主案例：作廢付款使淨額 < 0 → 須拒絕 ─────────────────────────────────────────


def test_void_payment_rejected_when_net_would_go_negative(void_client):
    """付款 1000 + 退費 800，作廢付款後淨額 −800 < 0 → 須擋為 400，付款不作廢。"""
    client, sf = void_client
    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="王測試", paid_amount=200)
        s.commit()
        reg_id = reg.id
        payment_id = _seed_record(s, reg_id, ptype="payment", amount=1000)
        _seed_record(s, reg_id, ptype="refund", amount=800)
        s.commit()

    assert _login(client).status_code == 200

    res = _void(client, reg_id, payment_id)
    assert res.status_code == 400, (
        "作廢付款使淨額 < 0（孤兒退費）應被擋為 400，"
        f"卻得 {res.status_code}：{res.text}"
    )
    # 付款未被作廢、paid_amount 不變
    assert _payment_active(sf, payment_id), "被拒絕後付款不應被作廢"
    assert _paid_amount(sf, reg_id) == 200, "被拒絕後 paid_amount 不應變動"


# ── 回歸：正常作廢仍可進行 ──────────────────────────────────────────────────────


def test_void_payment_allowed_when_no_refund(void_client):
    """付款 1000、無退費，作廢付款 → 淨額 0 ≥ 0 → 仍 200，正常沖回。"""
    client, sf = void_client
    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="陳測試", paid_amount=1000)
        s.commit()
        reg_id = reg.id
        payment_id = _seed_record(s, reg_id, ptype="payment", amount=1000)
        s.commit()

    assert _login(client).status_code == 200

    res = _void(client, reg_id, payment_id)
    assert res.status_code == 200, res.text
    assert not _payment_active(sf, payment_id)
    assert _paid_amount(sf, reg_id) == 0


def test_void_refund_then_payment_workflow(void_client):
    """提案要求的處理流程：先作廢退費（淨額回正）→ 再作廢付款（淨額 0）→ 兩步皆 200。"""
    client, sf = void_client
    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="林測試", paid_amount=200)
        s.commit()
        reg_id = reg.id
        payment_id = _seed_record(s, reg_id, ptype="payment", amount=1000)
        refund_id = _seed_record(s, reg_id, ptype="refund", amount=800)
        s.commit()

    assert _login(client).status_code == 200

    # 先作廢退費 → 淨額 = 1000 − 0 = 1000 ≥ 0 → 允許
    res = _void(client, reg_id, refund_id, reason="退費登記錯誤，先作廢退費")
    assert res.status_code == 200, res.text
    assert _paid_amount(sf, reg_id) == 1000

    # 再作廢付款 → 淨額 = 0 − 0 = 0 ≥ 0 → 允許
    res = _void(client, reg_id, payment_id, reason="款項全數退回，作廢付款")
    assert res.status_code == 200, res.text
    assert _paid_amount(sf, reg_id) == 0
