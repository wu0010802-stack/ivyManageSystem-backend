"""tests/test_pos_daily_close_lock.py — POS 日結簽核 vs 交易寫入的 advisory lock（M2）。

race：寫入端守衛 `_require_daily_close_unlocked` 只讀一次 close 表（check-then-act），
簽核端 `approve_daily_close` 的 snapshot 看不到未 commit 的 checkout → 兩邊同時
進行會雙雙成功，凍結 snapshot 永久漏單。修法：兩側對同一 close_date 共用
`acquire_activity_daily_close_lock`（PG advisory xact lock）序列化。

SQLite 環境下 lock 為 no-op（單 process 無此 race），因此本檔聚焦：
1. key 推導（穩定 / per-date 隔離 / namespace 隔離 / 63-bit 正整數）
2. dialect 分支（SQLite no-op 不發 SQL；PG 發 pg_advisory_xact_lock）
3. wiring：簽核端與寫入端守衛都實際呼叫 lock helper
4. 行為回歸：簽核後 checkout 該日被 400 擋下
真實並發互斥由生產 PostgreSQL 保障。
"""

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock

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
from utils.advisory_lock import (
    _key_for_activity_daily_close,
    _key_for_activity_refund,
    acquire_activity_daily_close_lock,
)

from tests.test_activity_pos import _create_admin, _login, _setup_reg

APPROVE_PERMS = ["ACTIVITY_READ", "ACTIVITY_WRITE", "ACTIVITY_PAYMENT_APPROVE"]


def _seed_payment(s, reg_id, day, amount=500):
    """直接 seed 一筆當日有效交易，讓日結簽核非 0 筆（避免空簽守衛擋下）。
    與 checkout 自身守衛解耦——本檔測的是鎖協議，不是 checkout 流程。"""
    s.add(
        ActivityPaymentRecord(
            registration_id=reg_id,
            type="payment",
            amount=amount,
            payment_date=day,
            payment_method="現金",
            notes="[TEST]",
            operator="tester",
        )
    )
    s.flush()


@pytest.fixture
def lock_client(tmp_path):
    db_path = tmp_path / "daily_close_lock.sqlite"
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


# ── 1. key 推導 ──────────────────────────────────────────────────────────


class TestKeyDerivation:
    def test_same_date_same_key(self):
        d = date(2026, 6, 13)
        assert _key_for_activity_daily_close(d) == _key_for_activity_daily_close(d)

    def test_different_date_different_key(self):
        assert _key_for_activity_daily_close(
            date(2026, 6, 13)
        ) != _key_for_activity_daily_close(date(2026, 6, 14))

    def test_namespace_isolated_from_refund_lock(self):
        """與 activity refund lock 的 seed namespace 不碰撞。"""
        # refund key 以 registration_id 推導；用任意整數比對 namespace 隔離
        assert _key_for_activity_daily_close(
            date(2026, 6, 13)
        ) != _key_for_activity_refund(20260613)

    def test_key_fits_positive_int63(self):
        k = _key_for_activity_daily_close(date(2099, 12, 31))
        assert 0 <= k <= 0x7FFF_FFFF_FFFF_FFFF


# ── 2. dialect 分支 ──────────────────────────────────────────────────────


class TestDialectBranch:
    def test_sqlite_no_op_no_sql(self):
        """SQLite 降級 no-op：不發任何 SQL、不拋錯。"""
        engine = create_engine("sqlite:///:memory:")
        Session = sessionmaker(bind=engine)
        s = Session()
        try:
            acquire_activity_daily_close_lock(s, date(2026, 6, 13))
        finally:
            s.close()
            engine.dispose()

    def test_postgres_executes_advisory_lock_with_derived_key(self):
        """PG 分支：對 session 發 pg_advisory_xact_lock，bind param 為推導 key。"""
        target = date(2026, 6, 13)
        session = MagicMock()
        session.bind.dialect.name = "postgresql"

        acquire_activity_daily_close_lock(session, target)

        assert session.execute.call_count == 1
        sql_arg, params = session.execute.call_args.args
        assert "pg_advisory_xact_lock" in str(sql_arg)
        assert params == {"k": _key_for_activity_daily_close(target)}

    def test_none_bind_treated_as_non_postgres(self):
        """session.bind=None 不應炸（_is_postgres fail-safe → no-op）。"""
        session = MagicMock()
        session.bind = None
        acquire_activity_daily_close_lock(session, date(2026, 6, 13))
        session.execute.assert_not_called()


# ── 3. wiring ───────────────────────────────────────────────────────────


def _spy_lock(monkeypatch):
    """同時 patch 模組本體與兩個 import 站點，收集 (close_date) 呼叫。"""
    calls: list[date] = []
    from utils import advisory_lock as advisory_lock_mod

    real_fn = advisory_lock_mod.acquire_activity_daily_close_lock

    def spy(session, close_date):
        calls.append(close_date)
        return real_fn(session, close_date)

    monkeypatch.setattr(advisory_lock_mod, "acquire_activity_daily_close_lock", spy)
    from api.activity import pos_approval as pos_approval_mod

    monkeypatch.setattr(pos_approval_mod, "acquire_activity_daily_close_lock", spy)
    import services.activity_daily_snapshot as snapshot_mod

    monkeypatch.setattr(snapshot_mod, "acquire_activity_daily_close_lock", spy)
    return calls


def test_approve_daily_close_acquires_lock(lock_client, monkeypatch):
    """簽核端：approve_daily_close 必須對目標日取 advisory lock。"""
    client, sf = lock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="鎖測試簽核", paid_amount=0)
        _seed_payment(s, reg.id, target)
        s.commit()

    calls = _spy_lock(monkeypatch)
    assert _login(client).status_code == 200
    res = client.post(f"/api/activity/pos/daily-close/{target.isoformat()}", json={})
    assert res.status_code == 201, res.text
    assert target in calls


def test_checkout_guard_acquires_lock_for_payment_date(lock_client, monkeypatch):
    """寫入端：pos checkout 的日結守衛必須對 payment_date 取同一把 advisory lock。"""
    client, sf = lock_client
    target = date.today()
    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="鎖測試", paid_amount=0)
        s.commit()
        reg_id = reg.id

    calls = _spy_lock(monkeypatch)
    assert _login(client).status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_date": target.isoformat(),
            "type": "payment",
            "idempotency_key": "DAILYCLOSE-CHECKOUT-0001",
        },
    )
    assert res.status_code == 201, res.text
    assert target in calls


def test_unlock_daily_close_acquires_lock(lock_client, monkeypatch):
    """解鎖端：unlock_daily_close 必須對目標日取同一把 advisory lock（P2-1）。

    否則並發兩次 DELETE 同一 close_date 各自讀到同一 row → 各 insert 一筆
    history / cancelled ApprovalLog / 通知，第二筆 DELETE 匹配 0 列仍 commit
    （SQLAlchemy 預設只 warning），稽核軌跡與解鎖事件儀表板筆數被灌水。
    """
    client, sf = lock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="鎖測試解鎖", paid_amount=0)
        _seed_payment(s, reg.id, target)
        s.commit()

    assert _login(client).status_code == 200
    res_close = client.post(
        f"/api/activity/pos/daily-close/{target.isoformat()}", json={}
    )
    assert res_close.status_code == 201, res_close.text

    # 簽核完成後才開始 spy，隔離出 unlock 自己的取鎖
    calls = _spy_lock(monkeypatch)
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={
            "reason": "管理員緊急override解鎖測試，原因說明需要足夠長度以通過驗證守衛",
            "is_admin_override": True,
        },
    )
    assert res.status_code == 200, res.text
    assert target in calls


# ── 4. 行為回歸：簽核後該日交易被擋 ──────────────────────────────────────


def test_checkout_rejected_after_daily_close(lock_client):
    """簽核某日後，checkout 該日 payment_date 必 400（既有守衛不回歸）。"""
    client, sf = lock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _setup_reg(s, student_name="簽核後擋寫", paid_amount=0)
        # 先 seed 一筆當日交易，讓日結非 0 筆（空簽已被守衛拒絕）
        _seed_payment(s, reg.id, target)
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    res_close = client.post(
        f"/api/activity/pos/daily-close/{target.isoformat()}", json={}
    )
    assert res_close.status_code == 201, res_close.text

    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_date": target.isoformat(),
            "type": "payment",
            "idempotency_key": "DAILYCLOSE-REJECT-0001",
        },
    )
    assert res.status_code == 400, res.text
    assert "日結簽核" in res.json()["detail"]
