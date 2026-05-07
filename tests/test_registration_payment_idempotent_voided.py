"""驗證 add_registration_payment 端點冪等 replay 排除 voided 紀錄。

對應 POS 端 spec C5（test_pos_idempotent_voided.py）的 legacy 路徑：
POST /api/activity/registrations/{registration_id}/payments

威脅：員工 idempotency_key=K 收 NT$5000 → 主管 void → 客戶端用同 K 重送
→ 舊版回 200 「繳費記錄新增成功」但 DB 無新紀錄、paid_amount=0，
員工誤認已收，永久漏收。

修補：與 pos._find_idempotent_hit 對齊，排除 voided 紀錄；
key 命中但全 voided → 409。

Refs: 邏輯漏洞 audit 2026-05-07 P0 (#7)。
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

from tests.test_activity_pos import _create_admin, _login, _setup_reg

APPROVE_PERMS = (
    Permission.ACTIVITY_READ
    | Permission.ACTIVITY_WRITE
    | Permission.ACTIVITY_PAYMENT_APPROVE
)


@pytest.fixture
def reg_payment_client(tmp_path):
    db_path = tmp_path / "reg_payment_voided.sqlite"
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


def _add_payment_with_key(client, reg_id, idk, amount=500, ptype="payment", notes=""):
    return client.post(
        f"/api/activity/registrations/{reg_id}/payments",
        json={
            "type": ptype,
            "amount": amount,
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "notes": notes,
            "idempotency_key": idk,
        },
    )


class TestAddRegistrationPaymentVoidedReplay:
    def test_replay_after_void_returns_409(self, reg_payment_client):
        """收款 → void → 同 key 重送 → 409，不可被當 replay。"""
        client, sf = reg_payment_client
        with sf() as s:
            _create_admin(s, permissions=APPROVE_PERMS)
            reg = _setup_reg(s, student_name="王測試", paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        idk = "regpayvoided_aaaaa12345"
        res = _add_payment_with_key(client, reg_id, idk, amount=500)
        assert res.status_code == 201, res.text

        # 取剛建立的 payment_id 並 void 它
        with sf() as s:
            rec = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.idempotency_key == idk)
                .first()
            )
            assert rec is not None
            payment_id = rec.id

        res = client.request(
            "DELETE",
            f"/api/activity/registrations/{reg_id}/payments/{payment_id}",
            json={"reason": "誤刷退款測試 audit P0 #7"},
        )
        assert res.status_code == 200, res.text

        # 同 key 重送 → 必須 409，否則回 200 但 DB 無新紀錄 → 漏收
        res = _add_payment_with_key(client, reg_id, idk, amount=500)
        assert res.status_code == 409, res.text
        detail = res.json().get("detail", "")
        assert "作廢" in detail or "idempotency_key" in detail

    def test_replay_after_void_does_not_create_new_record(self, reg_payment_client):
        """確認 409 後 DB 不會多出新紀錄、paid_amount 仍歸零。"""
        client, sf = reg_payment_client
        with sf() as s:
            _create_admin(s, permissions=APPROVE_PERMS)
            reg = _setup_reg(s, student_name="陳測試", paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        idk = "regpaynonew_bbbbb67890"
        res = _add_payment_with_key(client, reg_id, idk, amount=800)
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
            json={"reason": "誤刷退款測試 audit P0 #7 案例二"},
        )
        assert res.status_code == 200

        # 同 key 重送
        res = _add_payment_with_key(client, reg_id, idk, amount=800)
        assert res.status_code == 409

        # DB 必須只剩 1 筆且 voided_at 非 None
        with sf() as s:
            rows = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.idempotency_key == idk)
                .all()
            )
            assert len(rows) == 1, f"預期 1 筆 voided 紀錄，實際 {len(rows)} 筆"
            assert rows[0].voided_at is not None
            from models.database import ActivityRegistration

            reg_after = (
                s.query(ActivityRegistration)
                .filter(ActivityRegistration.id == reg_id)
                .one()
            )
            assert (
                reg_after.paid_amount == 0
            ), f"paid_amount 應為 0（void 已沖回），實際 {reg_after.paid_amount}"

    def test_normal_replay_still_works(self, reg_payment_client):
        """收款 → 同 key 重送（未 void） → 200 + 同樣 paid_amount。"""
        client, sf = reg_payment_client
        with sf() as s:
            _create_admin(s, permissions=APPROVE_PERMS)
            reg = _setup_reg(s, student_name="李測試", paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        idk = "regpaynormal_ccccc54321"
        res1 = _add_payment_with_key(client, reg_id, idk, amount=500)
        assert res1.status_code == 201
        paid_first = res1.json()["paid_amount"]

        res2 = _add_payment_with_key(client, reg_id, idk, amount=500)
        assert res2.status_code == 201, res2.text
        # replay 應回相同 paid_amount，未產生新紀錄
        assert res2.json()["paid_amount"] == paid_first

        with sf() as s:
            rows = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.idempotency_key == idk)
                .all()
            )
            assert len(rows) == 1, "正常 replay 不應產生新紀錄"
