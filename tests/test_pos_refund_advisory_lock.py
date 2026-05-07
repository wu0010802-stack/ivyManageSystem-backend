"""
test_pos_refund_advisory_lock.py — 驗證 POS refund 路徑會取得 per-reg advisory lock。

對齊 spec C4（2026-05-06 稽核報告）：refund 累積閾值守衛在 _lock_regs 之後讀
prior_refund_map；同 reg 並發兩筆小額退費可能各自看到 prior=舊值並通過閾值，
advisory lock 強制序列化同 reg 的退費流程。

SQLite 環境下 advisory lock 是 no-op（_is_postgres=False），因此本測試聚焦於
「helper 是否被正確 wire 進 pos checkout 的 refund 路徑」這個結構性保證。
真實並發行為由生產 PostgreSQL 環境保障。
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

from tests.test_activity_pos import _create_admin, _login, _setup_reg

APPROVE_PERMS = (
    Permission.ACTIVITY_READ
    | Permission.ACTIVITY_WRITE
    | Permission.ACTIVITY_PAYMENT_APPROVE
)


@pytest.fixture
def lock_client(tmp_path):
    db_path = tmp_path / "advisory_lock.sqlite"
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


def test_refund_acquires_advisory_lock_per_registration(lock_client, monkeypatch):
    """單筆 refund 應對該 reg 呼叫 acquire_activity_refund_lock 一次。"""
    client, sf = lock_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="王退費", paid_amount=2000, is_paid=True)
        s.commit()
        reg_id = reg.id

    calls: list[int] = []
    from utils import advisory_lock as advisory_lock_mod

    real_fn = advisory_lock_mod.acquire_activity_refund_lock

    def spy(session, registration_id: int):
        calls.append(registration_id)
        return real_fn(session, registration_id)

    # 同時 patch 模組內部與 pos.py 已 import 的引用
    monkeypatch.setattr(advisory_lock_mod, "acquire_activity_refund_lock", spy)
    from api.activity import pos as pos_mod

    monkeypatch.setattr(pos_mod, "acquire_activity_refund_lock", spy)

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_date": date.today().isoformat(),
            "type": "refund",
            "notes": "退費測試確認 advisory lock 被呼叫",
        },
    )
    assert res.status_code == 201, res.text
    assert calls == [reg_id]


def test_payment_does_not_acquire_refund_lock(lock_client, monkeypatch):
    """payment 路徑不應觸發 refund advisory lock（YAGNI；payment 由 _lock_regs 即足）。"""
    client, sf = lock_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="李繳費", paid_amount=0)
        s.commit()
        reg_id = reg.id

    calls: list[int] = []
    from utils import advisory_lock as advisory_lock_mod

    def spy(session, registration_id: int):
        calls.append(registration_id)

    monkeypatch.setattr(advisory_lock_mod, "acquire_activity_refund_lock", spy)
    from api.activity import pos as pos_mod

    monkeypatch.setattr(pos_mod, "acquire_activity_refund_lock", spy)

    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_date": date.today().isoformat(),
            "type": "payment",
            "notes": "繳費不應取退費鎖",
        },
    )
    assert res.status_code == 201, res.text
    assert calls == []


def test_multi_item_refund_acquires_lock_in_ascending_order(lock_client, monkeypatch):
    """多 item refund 應按 reg_id 升冪逐一取鎖（避免 deadlock）。"""
    client, sf = lock_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg_a = _setup_reg(
            s,
            student_name="A 多筆",
            paid_amount=2000,
            is_paid=True,
            course_name="美術",
            supply_name="畫具",
        )
        reg_b = _setup_reg(
            s,
            student_name="B 多筆",
            paid_amount=2000,
            is_paid=True,
            course_name="勞作",
            supply_name="剪刀",
            course_price=1000,
            supply_price=300,
        )
        s.commit()
        ids = sorted([reg_a.id, reg_b.id])

    calls: list[int] = []
    from utils import advisory_lock as advisory_lock_mod

    def spy(session, registration_id: int):
        calls.append(registration_id)

    monkeypatch.setattr(advisory_lock_mod, "acquire_activity_refund_lock", spy)
    from api.activity import pos as pos_mod

    monkeypatch.setattr(pos_mod, "acquire_activity_refund_lock", spy)

    assert _login(client).status_code == 200
    # 故意以「降冪」順序送 items；handler 內仍應升冪取鎖
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [
                {"registration_id": max(ids), "amount": 100},
                {"registration_id": min(ids), "amount": 100},
            ],
            "payment_date": date.today().isoformat(),
            "type": "refund",
            "notes": "多筆退費驗證升冪取鎖以避免 deadlock",
        },
    )
    assert res.status_code == 201, res.text
    assert calls == ids  # 升冪順序
