"""tests/test_activity_idempotency_fallback.py — 無 idempotency_key 時的短窗去重。

風險背景（Task A6）：
`idempotency_key` 為 Optional。官方前端 UI 一律帶 key，重送靠 DB 全域 UNIQUE
擋住。但若呼叫端（外部腳本 / curl / 前端 bug）**不帶 key** 連送兩筆相同退費，
advisory lock 只把兩筆「序列化」執行，兩筆都會各自 INSERT → 重複出帳
（退費路徑會把 paid_amount 退兩次，金流溢退）。

修正：伺服器端短窗（預設 60 秒）自動去重 fallback。無 key 時，先在 lock
保護區內查最近 window 內同 (reg, type, amount, operator) 的有效紀錄；命中則
回放（不再 INSERT）。帶 key 的契約完全不變。

本檔兩支金流關鍵測試：
- add_registration_payment（單筆 /payments）退費無 key 連送兩次
- pos_checkout（多 item POS）退費無 key 連送兩次

兩者都斷言：只建立 1 筆有效退費、paid_amount 只降一次。
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
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    ActivitySupply,
    Base,
    RegistrationCourse,
    RegistrationSupply,
    User,
)
from utils.auth import hash_password

REFUND_REASON = "家長要求退費，已確認原因符合園所政策。"


@pytest.fixture
def idk_client(tmp_path):
    db_path = tmp_path / "idk.sqlite"
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


def _create_admin(
    session,
    username: str = "idk_admin",
    password: str = "TempPass123",
    # 帶 ACTIVITY_PAYMENT_APPROVE：解掉「大額退費」與「實退 vs 建議偏離」兩道
    # 簽核守衛，讓測試聚焦於「無 key 短窗去重」本身，不被退費金額守衛干擾。
    permission_names: list[str] = [
        "ACTIVITY_READ",
        "ACTIVITY_WRITE",
        "ACTIVITY_PAYMENT_APPROVE",
    ],
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=permission_names,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username="idk_admin", password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _setup_reg(
    session,
    *,
    student_name="王小明",
    course_price=2000,
    supply_price=0,
    paid_amount=1000,
    course_name="美術",
    supply_name="畫具包",
) -> ActivityRegistration:
    """建立含一門課程（+ 可選用品）的報名，預設已繳 1000、應繳 2000。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = ActivityCourse(
        name=course_name,
        price=course_price,
        capacity=30,
        allow_waitlist=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=paid_amount,
        is_paid=False,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(reg)
    session.flush()
    session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=course_price,
        )
    )
    if supply_price > 0:
        supply = ActivitySupply(
            name=supply_name, price=supply_price, school_year=sy, semester=sem
        )
        session.add(supply)
        session.flush()
        session.add(
            RegistrationSupply(
                registration_id=reg.id,
                supply_id=supply.id,
                price_snapshot=supply_price,
            )
        )
    session.flush()
    return reg


def _active_refund_records(session, reg_id):
    return (
        session.query(ActivityPaymentRecord)
        .filter(
            ActivityPaymentRecord.registration_id == reg_id,
            ActivityPaymentRecord.type == "refund",
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .all()
    )


class TestAddRegistrationPaymentNoKeyDedup:
    def test_refund_without_key_double_submit_deduped(self, idk_client):
        """已繳 1000，無 idempotency_key 連送兩筆 refund 500：
        修前 → 2 筆退費 / paid 退兩次到 0（重複出帳）。
        修後 → 1 筆退費 / paid 只降一次到 500（第二筆判 replay）。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=1000)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        body = {
            "type": "refund",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "notes": REFUND_REASON,
            # 故意不帶 idempotency_key（外部呼叫端遺漏 key 的情境）
        }
        res1 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        res2 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res1.status_code == 201, res1.text
        # 第二筆視為 replay：沿用既有 add_payment 命中 key 的回應（HTTP 201）
        assert res2.status_code == 201, res2.text

        with sf() as s:
            refunds = _active_refund_records(s, reg_id)
            assert (
                len(refunds) == 1
            ), f"無 key 短窗去重失敗：建立了 {len(refunds)} 筆退費（應為 1 筆）"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert (
                reg.paid_amount == 500
            ), f"paid_amount 應只退一次到 500，實際 {reg.paid_amount}"

    def test_payment_without_key_double_submit_deduped(self, idk_client):
        """繳費路徑同理：應繳 2000、已繳 0，無 key 連送兩筆 payment 500
        → 1 筆 / paid=500（而非 2 筆 / paid=1000）。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        body = {
            "type": "payment",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "notes": "",
        }
        res1 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        res2 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res1.status_code == 201, res1.text
        assert res2.status_code == 201, res2.text

        with sf() as s:
            payments = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.registration_id == reg_id,
                    ActivityPaymentRecord.type == "payment",
                    ActivityPaymentRecord.voided_at.is_(None),
                )
                .all()
            )
            assert (
                len(payments) == 1
            ), f"無 key 繳費短窗去重失敗：建立了 {len(payments)} 筆（應為 1）"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500

    def test_payment_with_key_path_unchanged(self, idk_client):
        """帶 key 的既有路徑不受影響：相同 key 重送仍只 1 筆。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        body = {
            "type": "payment",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "notes": "",
            "idempotency_key": "REG-PAY-WITHKEY12345678",
        }
        res1 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        res2 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res1.status_code == 201
        assert res2.status_code == 201
        with sf() as s:
            payments = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .all()
            )
            assert len(payments) == 1
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500

    def test_different_amount_without_key_not_deduped(self, idk_client):
        """無 key 但金額不同的兩筆繳費是合法的兩筆，不可被誤殺。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        base = {
            "type": "payment",
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "notes": "",
        }
        res1 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "amount": 500},
        )
        res2 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "amount": 300},
        )
        assert res1.status_code == 201
        assert res2.status_code == 201
        with sf() as s:
            payments = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .all()
            )
            assert len(payments) == 2, "金額不同不應被短窗去重誤殺"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 800


class TestPosCheckoutNoKeyDedup:
    def test_refund_without_key_double_submit_deduped(self, idk_client):
        """POS checkout 退費無 key 連送兩次：
        修前 → 2 筆退費 / paid 退兩次；修後 → 1 筆 / paid 只降一次。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=1000)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        body = {
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "type": "refund",
            "notes": REFUND_REASON,
            # 不帶 idempotency_key
        }
        res1 = client.post("/api/activity/pos/checkout", json=body)
        res2 = client.post("/api/activity/pos/checkout", json=body)
        assert res1.status_code == 201, res1.text
        assert res2.status_code == 201, res2.text

        with sf() as s:
            refunds = _active_refund_records(s, reg_id)
            assert (
                len(refunds) == 1
            ), f"POS 無 key 短窗去重失敗：建立了 {len(refunds)} 筆退費（應為 1）"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert (
                reg.paid_amount == 500
            ), f"paid_amount 應只退一次到 500，實際 {reg.paid_amount}"

    def test_multi_item_without_key_not_silently_dropped(self, idk_client):
        """多 item 無 key 結帳不可因 items[0] 撞舊收據而 replay 整張、靜默吞掉其餘 item。
        修前 → regB 漏帳（0 筆）；修後 → 多 item 跳過去重，regB 正常出帳（1 筆）。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg_a = _setup_reg(
                s, student_name="多項A", course_name="課A", paid_amount=1000
            )
            reg_b = _setup_reg(
                s, student_name="多項B", course_name="課B", paid_amount=1000
            )
            s.commit()
            a_id, b_id = reg_a.id, reg_b.id

        assert _login(client).status_code == 200

        r1 = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": a_id, "amount": 400}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "type": "refund",
                "notes": REFUND_REASON,
            },
        )
        assert r1.status_code == 201, r1.text

        r2 = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [
                    {"registration_id": a_id, "amount": 400},
                    {"registration_id": b_id, "amount": 300},
                ],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "type": "refund",
                "notes": REFUND_REASON,
            },
        )
        assert r2.status_code == 201, r2.text

        with sf() as s:
            b_refunds = _active_refund_records(s, b_id)
            assert (
                len(b_refunds) == 1
            ), f"多 item 無 key 時 regB 被靜默吞掉：紀錄 {len(b_refunds)}（應為 1）"

    def test_payment_without_key_double_submit_deduped(self, idk_client):
        """POS checkout 繳費無 key 連送兩次 → 1 筆 / paid 只加一次。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        body = {
            "items": [{"registration_id": reg_id, "amount": 500}],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "type": "payment",
            "notes": "",
        }
        res1 = client.post("/api/activity/pos/checkout", json=body)
        res2 = client.post("/api/activity/pos/checkout", json=body)
        assert res1.status_code == 201, res1.text
        assert res2.status_code == 201, res2.text

        with sf() as s:
            payments = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.registration_id == reg_id,
                    ActivityPaymentRecord.type == "payment",
                    ActivityPaymentRecord.voided_at.is_(None),
                )
                .all()
            )
            assert (
                len(payments) == 1
            ), f"POS 無 key 繳費短窗去重失敗：{len(payments)} 筆（應為 1）"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500
