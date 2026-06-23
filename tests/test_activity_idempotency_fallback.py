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

    def test_with_key_different_payment_date_rejected(self, idk_client):
        """Finding (P1)：帶 idempotency_key 的單筆繳費，同 key/reg/type/amount 但
        payment_date 不同時，不可被當 replay 沿用舊紀錄（會把不同日交易記到舊日期，
        日結/報表錯帳）。應對齊 POS checkout 的內容簽章（含 payment_date）→ 同 key
        不同內容回 409，要求換 key 重送。

        修前 → context 守衛只比 reg/type/amount，第二筆（不同日）回 201 replay，
        DB 只 1 筆且日期停在第一筆。
        修後 → 回 409，DB 仍只 1 筆（第一筆，日期正確、未被覆寫）。"""
        from datetime import timedelta

        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        today = date.today()
        yesterday = today - timedelta(days=1)
        base = {
            "type": "payment",
            "amount": 500,
            "payment_method": "現金",
            "notes": "",
            "idempotency_key": "REG-PAY-DATEGUARD-0001",
        }
        res1 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "payment_date": yesterday.isoformat()},
        )
        assert res1.status_code == 201, res1.text
        # 同 key、同額、不同日 → 視為 key 誤用（內容不符），回 409
        res2 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "payment_date": today.isoformat()},
        )
        assert res2.status_code == 409, res2.text

        with sf() as s:
            payments = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .all()
            )
            assert len(payments) == 1, f"應只建立第一筆，實際 {len(payments)} 筆"
            assert (
                payments[0].payment_date == yesterday
            ), "第一筆日期應保持，不可被第二筆覆寫"

    def test_with_key_race_fallback_different_payment_date_rejected(
        self, idk_client, monkeypatch
    ):
        """Finding (P1)：帶 idempotency_key 的單筆繳費 race fallback 路徑漏比
        payment_date。

        當兩個併發請求都先通過 `_find_idempotent_hit` 前置檢查（race window
        內彼此都還沒看到對方已寫入的紀錄），第二個在 commit 撞 DB UNIQUE 約束
        → 走 IntegrityError fallback。此 fallback 原本只比 reg/type/amount，
        漏比 payment_date → 同 key 但不同帳務日的第二筆會被誤判為 replay 回 201、
        沿用第一筆的舊日期，日結/報表記到錯誤日期錯帳。

        用 monkeypatch 把 `_find_idempotent_hit`→None、`_has_any_record_for_key`
        →False，精確模擬 race window（兩請求都還沒看到對方的已寫入紀錄、都通過
        前置檢查），讓第二筆必走 commit 撞 UNIQUE 的 fallback。

        修前 → fallback context 守衛漏 payment_date，第二筆回 201 replay。
        修後 → fallback 對齊正常路徑（_assert_idempotency_context_match 含
        payment_date），第二筆回 409；DB 仍只 1 筆（第一筆日期未被覆寫）。"""
        from datetime import timedelta

        import api.activity.pos as pos_mod

        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        today = date.today()
        yesterday = today - timedelta(days=1)
        base = {
            "type": "payment",
            "amount": 500,
            "payment_method": "現金",
            "notes": "",
            "idempotency_key": "REG-PAY-RACE-DATE-0001",
        }

        # 模擬 race window：兩請求都先過前置檢查（看不到對方已寫入的紀錄），
        # 第二筆在 commit 撞 UNIQUE → 走 IntegrityError fallback。
        monkeypatch.setattr(pos_mod, "_find_idempotent_hit", lambda *a, **k: None)
        monkeypatch.setattr(pos_mod, "_has_any_record_for_key", lambda *a, **k: False)

        res1 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "payment_date": yesterday.isoformat()},
        )
        assert res1.status_code == 201, res1.text
        # 同 key、同額、不同日 → fallback 路徑也須視為 key 誤用（內容不符），回 409
        res2 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "payment_date": today.isoformat()},
        )
        assert res2.status_code == 409, res2.text

        with sf() as s:
            payments = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .all()
            )
            assert len(payments) == 1, f"應只建立第一筆，實際 {len(payments)} 筆"
            assert (
                payments[0].payment_date == yesterday
            ), "第一筆日期應保持，不可被第二筆 race fallback 覆寫"

    def test_with_key_race_fallback_genuine_duplicate_replays(
        self, idk_client, monkeypatch
    ):
        """race fallback 的正向路徑：同 key/reg/type/amount/payment_date 的真重送，
        在 race window 撞 DB UNIQUE 時應 idempotent replay 回 201（不建立第二筆、
        不誤判 409）。

        同時驗證 reachability 修補：修前 fallback 因 IntegrityError 在
        `_calc_total_amount` 的 autoflush（try 之外）逸出而形同虛設，連真重送都會
        500；修後 try 涵蓋 autoflush，fallback 真正接住碰撞 → 正確 replay。"""
        import api.activity.pos as pos_mod

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
            "idempotency_key": "REG-PAY-RACE-DUP-0001",
        }
        monkeypatch.setattr(pos_mod, "_find_idempotent_hit", lambda *a, **k: None)
        monkeypatch.setattr(pos_mod, "_has_any_record_for_key", lambda *a, **k: False)

        res1 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        res2 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res1.status_code == 201, res1.text
        # 內容完全相同 → fallback idempotent replay，非 500、非 409
        assert res2.status_code == 201, res2.text

        with sf() as s:
            payments = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .all()
            )
            assert len(payments) == 1, f"真重送只應 1 筆，實際 {len(payments)} 筆"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500, "paid_amount 只應 +500 一次（未雙扣）"

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

    def test_different_payment_date_without_key_not_deduped(self, idk_client):
        """Finding 5：無 key、同操作員、同額、同 type，但 payment_date 不同的兩筆
        是合法的兩筆補登（例如同操作員 60 秒內補登昨天與今天各一筆同額繳費），
        不可被短窗去重誤判為 replay 吞掉第二筆。

        修前 → _recent_duplicate_payment 不比 payment_date，第二筆被當 replay
        → 只 1 筆 / paid 只加一次。
        修後 → 去重納入 payment_date，兩筆都入帳 → 2 筆 / paid 加兩次。"""
        from datetime import timedelta

        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        today = date.today()
        yesterday = today - timedelta(days=1)
        base = {
            "type": "payment",
            "amount": 500,
            "payment_method": "現金",
            "notes": "",
        }
        res1 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "payment_date": yesterday.isoformat()},
        )
        res2 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "payment_date": today.isoformat()},
        )
        assert res1.status_code == 201, res1.text
        assert res2.status_code == 201, res2.text
        with sf() as s:
            payments = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .all()
            )
            assert (
                len(payments) == 2
            ), f"不同 payment_date 不應被短窗去重誤殺，實際 {len(payments)} 筆"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 1000


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
        """多 item 結帳不可因 items[0] 撞舊收據而 replay 整張、靜默吞掉其餘 item。

        R7-3（2026-06-06）後契約更新：多 item 無 key 直接 400（強制帶
        idempotency_key，防非官方 caller 重送整車重複出帳）；帶 key 的多 item
        為全請求去重，items[0] 撞舊收據不會吞掉 regB——本測試保留原保護目標，
        改以新契約斷言（無 key → 400；帶 key → regB 正常出帳 1 筆）。"""
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

        multi_items = {
            "items": [
                {"registration_id": a_id, "amount": 400},
                {"registration_id": b_id, "amount": 300},
            ],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "type": "refund",
            "notes": REFUND_REASON,
        }
        # R7-3：多 item 無 key → 400（不再走 items[0] fallback 去重）
        r_nokey = client.post("/api/activity/pos/checkout", json=multi_items)
        assert r_nokey.status_code == 400, r_nokey.text

        # 帶 key 的多 item：items[0] 撞 r1 舊收據不可吞掉 regB
        r2 = client.post(
            "/api/activity/pos/checkout",
            json={**multi_items, "idempotency_key": "multi-item-key-001"},
        )
        assert r2.status_code == 201, r2.text

        with sf() as s:
            b_refunds = _active_refund_records(s, b_id)
            assert (
                len(b_refunds) == 1
            ), f"多 item 時 regB 被靜默吞掉：紀錄 {len(b_refunds)}（應為 1）"

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
