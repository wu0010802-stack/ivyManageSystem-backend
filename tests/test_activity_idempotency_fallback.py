"""tests/test_activity_idempotency_fallback.py — idempotency_key 為金流寫入必填。

契約變更（2026-06-29 audit Finding #1）：
舊版 `idempotency_key` 為 Optional，無 key 時走「短窗內容式去重」fallback
（同 reg/type/amount/operator/payment_date 60 秒內視為重送）。此 fallback 會把
**合法的同額分次收款**（例如同操作員同日對同報名連收兩筆相同金額）誤判為重送
靜默吞掉，帳本與實收不符。

決策：移除模糊的內容式去重，改為**所有金流寫入強制帶 idempotency_key**。官方前端
（繳費 dialog + POS）本就一律帶唯一 key，不受影響；外部/手動 caller 須改帶 key。
無 key（或空字串）一律 schema 層 422 拒絕，與多-item POS 既有契約（R7-3）一致。
合法的同額分次收款只要各帶不同 key 即可兩筆都入帳。

本檔斷言：
- 單筆 /payments 無 key / 空 key → 422
- POS checkout（單 item / 多 item）無 key → 422
- 帶 key 的既有冪等行為（replay / payment_date 內容守衛 / race fallback）完全不變
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
    # 簽核守衛，讓測試聚焦於「idempotency_key 必填」本身，不被退費金額守衛干擾。
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


def _count_records(session, reg_id):
    return (
        session.query(ActivityPaymentRecord)
        .filter(ActivityPaymentRecord.registration_id == reg_id)
        .count()
    )


class TestAddRegistrationPaymentRequiresKey:
    """單筆 /payments：idempotency_key 必填，無/空 key 一律 422 且不出帳。"""

    def test_payment_without_key_rejected_422(self, idk_client):
        """無 idempotency_key 的繳費 → 422，DB 不建立任何紀錄。

        舊行為（移除前）：無 key 連送兩筆同額視為重送去重，回 201/1 筆，
        合法的同額分次收款被靜默吞掉。新契約：直接 422 拒絕。"""
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
            # 故意不帶 idempotency_key
        }
        res = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res.status_code == 422, res.text

        with sf() as s:
            assert _count_records(s, reg_id) == 0, "422 後不應建立任何付款紀錄"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 0

    def test_refund_without_key_rejected_422(self, idk_client):
        """無 idempotency_key 的退費 → 422，paid_amount 不變。"""
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
        }
        res = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res.status_code == 422, res.text

        with sf() as s:
            assert len(_active_refund_records(s, reg_id)) == 0
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 1000

    def test_empty_string_key_rejected_422(self, idk_client):
        """idempotency_key 空字串等同未帶 → 422。"""
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
            "idempotency_key": "",
        }
        res = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res.status_code == 422, res.text
        with sf() as s:
            assert _count_records(s, reg_id) == 0

    def test_same_amount_different_key_both_recorded(self, idk_client):
        """核心：合法的同額分次收款只要各帶不同 key → 兩筆都入帳（不再被誤吞）。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        base = {
            "type": "payment",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "notes": "",
        }
        res1 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "idempotency_key": "REG-PAY-INSTALMENT-0001"},
        )
        res2 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "idempotency_key": "REG-PAY-INSTALMENT-0002"},
        )
        assert res1.status_code == 201, res1.text
        assert res2.status_code == 201, res2.text
        with sf() as s:
            assert _count_records(s, reg_id) == 2, "不同 key 的同額兩筆都應入帳"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 1000


class TestAddRegistrationPaymentWithKey:
    """帶 key 的既有冪等行為不受契約變更影響。"""

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
            assert _count_records(s, reg_id) == 1
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500

    def test_with_key_different_payment_date_rejected(self, idk_client):
        """帶 idempotency_key 的單筆繳費，同 key/reg/type/amount 但 payment_date
        不同時，不可被當 replay 沿用舊紀錄（會把不同日交易記到舊日期，日結/報表
        錯帳）。應對齊 POS checkout 的內容簽章（含 payment_date）→ 同 key 不同內容
        回 409，要求換 key 重送。"""
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
        """帶 idempotency_key 的單筆繳費 race fallback 路徑漏比 payment_date。

        當兩個併發請求都先通過 `_find_idempotent_hit` 前置檢查（race window
        內彼此都還沒看到對方已寫入的紀錄），第二個在 commit 撞 DB UNIQUE 約束
        → 走 IntegrityError fallback。此 fallback 須對齊正常路徑（含 payment_date）
        → 同 key 但不同帳務日的第二筆回 409，DB 仍只 1 筆（日期未被覆寫）。"""
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

        monkeypatch.setattr(pos_mod, "_find_idempotent_hit", lambda *a, **k: None)
        monkeypatch.setattr(pos_mod, "_has_any_record_for_key", lambda *a, **k: False)

        res1 = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={**base, "payment_date": yesterday.isoformat()},
        )
        assert res1.status_code == 201, res1.text
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
        不誤判 409）。"""
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

    def test_with_key_race_fallback_voided_key_rejected(self, idk_client, monkeypatch):
        """race fallback 撞 UNIQUE 但 key 對應紀錄已全 voided → 必須 409。

        對齊 POS spec C5（pos.py:1069）：voided 紀錄不可被當 replay 回傳。
        情境：一筆帶 key 的繳費建立後被 void（key 仍佔用 UNIQUE、paid_amount 歸零）。
        另一併發請求在 race window 內前置 `_has_any_record_for_key` 尚未看到（以
        monkeypatch 讓其首呼回 False 模擬），第二筆同 key INSERT 撞 UNIQUE →
        IntegrityError fallback。單筆端點的 fallback 若沿用「不濾 voided 的裸查詢」
        會命中該作廢紀錄、context 相符 → 回 201「新增成功」+ paid_amount（已反映
        void=0）→ 員工誤認已收、DB 無有效紀錄 → 靜默漏收（P0-class）。正確行為：
        與 POS 對齊回 409、不加回款項。"""
        import api.activity.pos as pos_mod

        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        key = "REG-PAY-RACE-VOIDED-0001"
        body = {
            "type": "payment",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "notes": "",
            "idempotency_key": key,
        }
        # 1) 正常建立一筆
        res_create = client.post(
            f"/api/activity/registrations/{reg_id}/payments", json=body
        )
        assert res_create.status_code == 201, res_create.text
        with sf() as s:
            rec = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.idempotency_key == key)
                .first()
            )
            payment_id = rec.id
        # 2) void 該筆 → key 對應紀錄全 voided、paid_amount 歸零
        res_void = client.request(
            "DELETE",
            f"/api/activity/registrations/{reg_id}/payments/{payment_id}",
            json={"reason": "誤刷退款，測試 voided race fallback。"},
        )
        assert res_void.status_code == 200, res_void.text
        with sf() as s:
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 0

        # 3) 模擬 race window：前置檢查的兩個 helper 都還沒看到（與同類 race 測試一致）。
        #    fallback 刻意以真實 DB 狀態（裸查詢）判定 → 命中全 voided → 409。
        monkeypatch.setattr(pos_mod, "_find_idempotent_hit", lambda *a, **k: None)
        monkeypatch.setattr(pos_mod, "_has_any_record_for_key", lambda *a, **k: False)

        # 4) 同 key 再送 → INSERT 撞 UNIQUE → fallback。全 voided → 必須 409。
        res2 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res2.status_code == 409, res2.text

        # 5) 未回假成功、未加回款項；DB 仍只 1 筆（原 voided）
        with sf() as s:
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 0, "voided race fallback 不應回假成功、加回款項"
            assert _count_records(s, reg_id) == 1


class TestPosCheckoutRequiresKey:
    """POS checkout：idempotency_key 必填，無/空 key 一律 422 且不出帳。"""

    def test_single_item_without_key_rejected_422(self, idk_client):
        """單 item POS 無 key → 422（舊版走 items[0] 短窗去重，現移除）。"""
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
        res = client.post("/api/activity/pos/checkout", json=body)
        assert res.status_code == 422, res.text
        with sf() as s:
            assert _count_records(s, reg_id) == 0

    def test_multi_item_without_key_rejected_422(self, idk_client):
        """多 item POS 無 key → 422（R7-3 原以 router 400 守衛，現統一 schema 422）。"""
        client, sf = idk_client
        with sf() as s:
            _create_admin(s)
            reg_a = _setup_reg(
                s, student_name="多項A", course_name="課A", paid_amount=0
            )
            reg_b = _setup_reg(
                s, student_name="多項B", course_name="課B", paid_amount=0
            )
            s.commit()
            a_id, b_id = reg_a.id, reg_b.id

        assert _login(client).status_code == 200

        body = {
            "items": [
                {"registration_id": a_id, "amount": 400},
                {"registration_id": b_id, "amount": 300},
            ],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "type": "payment",
            "notes": "",
        }
        res = client.post("/api/activity/pos/checkout", json=body)
        assert res.status_code == 422, res.text
        with sf() as s:
            assert _count_records(s, a_id) == 0
            assert _count_records(s, b_id) == 0

    def test_with_key_replay_unchanged(self, idk_client):
        """POS 帶 key 的冪等 replay 不受影響：相同 key 重送仍只 1 筆。"""
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
            "idempotency_key": "POS-WITHKEY-0001",
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
            assert len(payments) == 1, f"帶 key 重送應只 1 筆，實際 {len(payments)}"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500
