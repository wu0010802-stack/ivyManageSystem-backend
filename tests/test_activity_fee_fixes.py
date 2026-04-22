"""tests/test_activity_fee_fixes.py — 才藝收費 bug 修正回歸測試。

涵蓋：
- #1: update_payment / batch_update_payment 的 is_paid=False 改為寫 refund 沖帳，
  不再 DELETE 歷史付款記錄
- #2: withdraw_course / delete_registration 若有 paid_amount 需 force_refund
- #3: add_registration_payment 支援 idempotency_key 重送、refund 不會讓 paid_amount 為負
- #4: 已完成 daily-close 的日期拒絕新增/刪除付款紀錄（POS checkout、單筆繳費、delete payment）
"""

import os
import sys
from datetime import date, datetime, timedelta

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
    ActivityPosDailyClose,
    ActivityRegistration,
    ActivitySupply,
    Base,
    RegistrationCourse,
    RegistrationSupply,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def fee_client(tmp_path):
    db_path = tmp_path / "fee.sqlite"
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
    username: str = "fee_admin",
    password: str = "TempPass123",
    permissions: int = (
        Permission.ACTIVITY_READ
        | Permission.ACTIVITY_WRITE
        | Permission.ACTIVITY_PAYMENT_APPROVE
    ),
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=permissions,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(
    client: TestClient,
    username: str = "fee_admin",
    password: str = "TempPass123",
):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _setup_reg(
    session,
    *,
    student_name: str = "王小明",
    course_price: int = 1000,
    supply_price: int = 0,
    paid_amount: int = 0,
    is_paid: bool = False,
    course_name: str = "美術",
    supply_name: str = "畫具包",
    class_name: str = "大班",
) -> ActivityRegistration:
    """建立一筆含一門課（可選用品）的報名。total = course_price + supply_price。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = (
        session.query(ActivityCourse)
        .filter(
            ActivityCourse.name == course_name,
            ActivityCourse.school_year == sy,
            ActivityCourse.semester == sem,
        )
        .first()
    )
    if not course:
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
        class_name=class_name,
        paid_amount=paid_amount,
        is_paid=is_paid,
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
        supply = (
            session.query(ActivitySupply)
            .filter(
                ActivitySupply.name == supply_name,
                ActivitySupply.school_year == sy,
                ActivitySupply.semester == sem,
            )
            .first()
        )
        if not supply:
            supply = ActivitySupply(
                name=supply_name,
                price=supply_price,
                school_year=sy,
                semester=sem,
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


# ══════════════════════════════════════════════════════════════════════
# #1 — update_payment / batch_update_payment 不再 DELETE 記錄
# ══════════════════════════════════════════════════════════════════════


class TestMarkUnpaidWritesRefund:
    def test_single_mark_unpaid_writes_refund_not_delete(self, fee_client):
        """單筆標記未繳費：應寫入一筆 refund 沖帳，保留原 payment 記錄。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=1000, is_paid=True)
            s.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="payment",
                    amount=1000,
                    payment_date=date.today(),
                    payment_method="現金",
                    operator="fee_admin",
                )
            )
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/activity/registrations/{reg_id}/payment",
            json={"is_paid": False},
        )
        assert res.status_code == 200

        with sf() as s:
            records = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .order_by(ActivityPaymentRecord.id.asc())
                .all()
            )
            # 原 payment 仍在（審計軌跡保留）+ 新增 refund 沖帳
            assert len(records) == 2
            assert records[0].type == "payment"
            assert records[0].amount == 1000
            assert records[1].type == "refund"
            assert records[1].amount == 1000
            assert records[1].payment_method == "系統補齊"

            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 0
            assert reg.is_paid is False

    def test_batch_mark_unpaid_writes_refund_not_delete(self, fee_client):
        """批次標記未繳費：每筆都應寫入 refund，保留原有付款記錄。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            r1 = _setup_reg(s, student_name="甲", paid_amount=500, is_paid=False)
            r2 = _setup_reg(s, student_name="乙", paid_amount=1000, is_paid=True)
            for reg, amt in [(r1, 500), (r2, 1000)]:
                s.add(
                    ActivityPaymentRecord(
                        registration_id=reg.id,
                        type="payment",
                        amount=amt,
                        payment_date=date.today(),
                        payment_method="轉帳",
                        operator="fee_admin",
                    )
                )
            s.commit()
            ids = [r1.id, r2.id]

        assert _login(client).status_code == 200

        res = client.put(
            "/api/activity/registrations/batch-payment",
            json={"ids": ids, "is_paid": False},
        )
        assert res.status_code == 200

        with sf() as s:
            for rid, original_amt in zip(ids, [500, 1000]):
                records = (
                    s.query(ActivityPaymentRecord)
                    .filter(ActivityPaymentRecord.registration_id == rid)
                    .order_by(ActivityPaymentRecord.id.asc())
                    .all()
                )
                assert len(records) == 2, f"reg={rid} 應保留原 payment + 新增 refund"
                assert records[0].type == "payment"
                assert records[1].type == "refund"
                assert records[1].amount == original_amt
                assert records[1].payment_method == "系統補齊"

                reg = s.query(ActivityRegistration).get(rid)
                assert reg.paid_amount == 0
                assert reg.is_paid is False

    def test_mark_paid_auto_fill_uses_system_reconcile_method(self, fee_client):
        """自動補齊時 payment_method 應為「系統補齊」而非「現金」，避免污染 POS 日結。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=1500)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/activity/registrations/{reg_id}/payment",
            json={"is_paid": True},
        )
        assert res.status_code == 200

        with sf() as s:
            rec = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .first()
            )
            assert rec is not None
            assert rec.payment_method == "系統補齊"
            assert rec.amount == 1500


# ══════════════════════════════════════════════════════════════════════
# #3 — add_registration_payment：idempotency / refund 不負數
# ══════════════════════════════════════════════════════════════════════


class TestAddPaymentIdempotency:
    def test_idempotent_replay_does_not_double_write(self, fee_client):
        """相同 idempotency_key 10 分鐘內重送，不會重複寫入、回覆相同語義。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=1000)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        body = {
            "type": "payment",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "notes": "",
            "idempotency_key": "REG-PAY-ABCDEFGH12345678",
        }
        res1 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        res2 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert res1.status_code == 201
        assert res2.status_code == 201

        with sf() as s:
            records = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .all()
            )
            assert len(records) == 1, "idempotency 重送不應重複寫入"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500

    def test_refund_over_paid_rejected_with_400(self, fee_client):
        """退費金額超過已繳應回 400，不可讓 paid_amount 為負。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=300)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "refund",
                "amount": 500,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "notes": "",
            },
        )
        assert res.status_code == 400
        with sf() as s:
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 300


# ══════════════════════════════════════════════════════════════════════
# #2 — withdraw_course / delete_registration 守衛
# ══════════════════════════════════════════════════════════════════════


class TestWithdrawAndDeleteGuards:
    def test_delete_registration_with_paid_requires_force(self, fee_client):
        """刪除有繳費的報名若沒帶 force_refund=true，應回 409。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=1000)
            s.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="payment",
                    amount=1000,
                    payment_date=date.today(),
                    payment_method="現金",
                    operator="fee_admin",
                )
            )
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.delete(f"/api/activity/registrations/{reg_id}")
        assert res.status_code == 409

        with sf() as s:
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.is_active is True, "守衛未通過，報名不應被軟刪"

    def test_delete_registration_force_refund_writes_refund(self, fee_client):
        """force_refund=true 時自動寫退費沖帳並軟刪。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, paid_amount=1000)
            s.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="payment",
                    amount=1000,
                    payment_date=date.today(),
                    payment_method="轉帳",
                    operator="fee_admin",
                )
            )
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.delete(f"/api/activity/registrations/{reg_id}?force_refund=true")
        assert res.status_code == 200

        with sf() as s:
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.is_active is False
            assert reg.paid_amount == 0
            refunds = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.registration_id == reg_id,
                    ActivityPaymentRecord.type == "refund",
                )
                .all()
            )
            assert len(refunds) == 1
            assert refunds[0].amount == 1000
            assert refunds[0].payment_method == "系統補齊"

    def test_withdraw_course_overpay_requires_force(self, fee_client):
        """退光所有課程、paid 未動時，應要求 force_refund。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=500, paid_amount=500, is_paid=True)
            s.commit()
            reg_id = reg.id
            course_id = (
                s.query(RegistrationCourse.course_id)
                .filter(RegistrationCourse.registration_id == reg_id)
                .scalar()
            )

        assert _login(client).status_code == 200

        res = client.delete(f"/api/activity/registrations/{reg_id}/courses/{course_id}")
        assert res.status_code == 409, "退課後超繳應擋下要求 force_refund"

    def test_withdraw_course_force_refund_writes_refund(self, fee_client):
        """force_refund=true 時退課並寫退費沖帳差額。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=500, paid_amount=500, is_paid=True)
            s.commit()
            reg_id = reg.id
            course_id = (
                s.query(RegistrationCourse.course_id)
                .filter(RegistrationCourse.registration_id == reg_id)
                .scalar()
            )

        assert _login(client).status_code == 200

        res = client.delete(
            f"/api/activity/registrations/{reg_id}/courses/{course_id}"
            "?force_refund=true"
        )
        assert res.status_code == 200
        data = res.json()
        assert data["refunded_amount"] == 500
        assert data["paid_amount"] == 0

        with sf() as s:
            refunds = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.registration_id == reg_id,
                    ActivityPaymentRecord.type == "refund",
                )
                .all()
            )
            assert len(refunds) == 1
            assert refunds[0].amount == 500
            assert refunds[0].payment_method == "系統補齊"

    def test_withdraw_one_of_many_courses_no_refund_needed(self, fee_client):
        """退一門課但其餘課程仍需繳 paid_amount 以內的金額，不需 force_refund。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=500, paid_amount=500, is_paid=False)
            # 再加一門課，total 變 1000
            from utils.academic import resolve_current_academic_term

            sy, sem = resolve_current_academic_term()
            course2 = ActivityCourse(
                name="勞作",
                price=500,
                capacity=30,
                allow_waitlist=True,
                school_year=sy,
                semester=sem,
            )
            s.add(course2)
            s.flush()
            s.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course2.id,
                    status="enrolled",
                    price_snapshot=500,
                )
            )
            s.commit()
            reg_id = reg.id
            first_course_id = (
                s.query(RegistrationCourse.course_id)
                .filter(
                    RegistrationCourse.registration_id == reg_id,
                    RegistrationCourse.course_id != course2.id,
                )
                .scalar()
            )

        assert _login(client).status_code == 200

        # 退第一門；退完 total 還有 500（另一門），paid=500，剛好結清 → 不需 force
        res = client.delete(
            f"/api/activity/registrations/{reg_id}/courses/{first_course_id}"
        )
        assert res.status_code == 200

        with sf() as s:
            refunds = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.registration_id == reg_id,
                    ActivityPaymentRecord.type == "refund",
                )
                .count()
            )
            assert refunds == 0, "未超繳不應自動寫退費"


# ══════════════════════════════════════════════════════════════════════
# #4 — 已 daily-close 日期守衛
# ══════════════════════════════════════════════════════════════════════


class TestDailyCloseGuard:
    def _close_today(self, session, approver: str = "fee_admin"):
        session.add(
            ActivityPosDailyClose(
                close_date=date.today(),
                approver_username=approver,
                approved_at=datetime.now(),
                payment_total=0,
                refund_total=0,
                net_total=0,
                transaction_count=0,
                by_method_json="{}",
            )
        )
        session.flush()

    def test_pos_checkout_rejects_closed_day(self, fee_client):
        """POS checkout 若 payment_date 已 daily-close 應回 400。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=1000)
            self._close_today(s)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "notes": "",
                "type": "payment",
            },
        )
        assert res.status_code == 400
        assert "日結" in res.json()["detail"]

    def test_add_payment_rejects_closed_day(self, fee_client):
        """單筆繳費若 payment_date 已簽核應 400。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=1000)
            self._close_today(s)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "payment",
                "amount": 500,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "notes": "",
            },
        )
        assert res.status_code == 400

    def test_delete_payment_rejects_closed_day(self, fee_client):
        """刪除付款若該筆 payment_date 已簽核應 400。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=1000, paid_amount=500)
            rec = ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=500,
                payment_date=date.today(),
                payment_method="現金",
                operator="fee_admin",
            )
            s.add(rec)
            s.flush()
            self._close_today(s)
            s.commit()
            reg_id = reg.id
            rec_id = rec.id

        assert _login(client).status_code == 200

        res = client.delete(f"/api/activity/registrations/{reg_id}/payments/{rec_id}")
        assert res.status_code == 400

    def test_mark_unpaid_rejects_closed_day(self, fee_client):
        """今日已簽核時標記未繳費應 400（自動沖帳會寫 today）。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=1000, paid_amount=1000, is_paid=True)
            self._close_today(s)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/activity/registrations/{reg_id}/payment",
            json={"is_paid": False},
        )
        assert res.status_code == 400


# ── Audit state overrides（覆寫 middleware 預設推斷的 entity_id）─────────


class TestActivityAuditStateOverride:
    """敏感端點需把 request.state.audit_entity_id 鎖回 registration_id，
    否則 middleware 的 _parse_entity_id 會抓到 URL 尾段的 payment_id / course_id，
    導致「某筆報名的所有稽核事件」查詢漏掉退課與刪繳費。"""

    def _make_capture_app(self, captured):
        from starlette.middleware.base import BaseHTTPMiddleware

        class StateCaptureMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                response = await call_next(request)
                captured["audit_entity_id"] = getattr(
                    request.state, "audit_entity_id", None
                )
                captured["audit_summary"] = getattr(
                    request.state, "audit_summary", None
                )
                return response

        app = FastAPI()
        app.add_middleware(StateCaptureMiddleware)
        app.include_router(auth_router)
        app.include_router(activity_router)
        return app

    def test_delete_payment_sets_registration_as_entity(self, fee_client):
        _, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=1000, paid_amount=500)
            rec = ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=500,
                payment_date=date.today(),
                payment_method="現金",
                operator="fee_admin",
            )
            s.add(rec)
            s.flush()
            s.commit()
            reg_id = reg.id
            rec_id = rec.id

        captured = {}
        app = self._make_capture_app(captured)
        with TestClient(app) as mw_client:
            assert _login(mw_client).status_code == 200
            res = mw_client.delete(
                f"/api/activity/registrations/{reg_id}/payments/{rec_id}"
            )
            assert res.status_code == 200, res.text

        assert captured["audit_entity_id"] == str(reg_id), (
            f"預期 entity_id 鎖回 registration_id={reg_id}，"
            f"但拿到 {captured['audit_entity_id']}（可能被 URL 尾段 payment_id 搶走）"
        )
        assert captured["audit_summary"] and "刪除繳費記錄" in captured["audit_summary"]

    def test_withdraw_course_sets_registration_as_entity(self, fee_client):
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            # 加第二門課以避免退課後 reg.paid_amount > total 觸發 409
            reg = _setup_reg(s, course_price=1000, paid_amount=0)
            extra_course = ActivityCourse(
                name="額外課程",
                price=500,
                capacity=30,
                allow_waitlist=True,
                school_year=reg.school_year,
                semester=reg.semester,
            )
            s.add(extra_course)
            s.flush()
            s.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=extra_course.id,
                    status="enrolled",
                    price_snapshot=500,
                )
            )
            s.commit()
            reg_id = reg.id
            course_id = extra_course.id

        captured = {}
        app = self._make_capture_app(captured)
        with TestClient(app) as mw_client:
            assert _login(mw_client).status_code == 200
            res = mw_client.delete(
                f"/api/activity/registrations/{reg_id}/courses/{course_id}"
            )
            assert res.status_code == 200, res.text

        assert captured["audit_entity_id"] == str(reg_id), (
            f"預期 entity_id 鎖回 registration_id={reg_id}，"
            f"但拿到 {captured['audit_entity_id']}（可能被 URL 尾段 course_id 搶走）"
        )
        assert captured["audit_summary"] and "退課" in captured["audit_summary"]


# ══════════════════════════════════════════════════════════════════════
# #5 — remove_registration_supply 超繳守衛（與 withdraw_course 對稱）
# ══════════════════════════════════════════════════════════════════════


class TestRemoveSupplyGuards:
    def test_remove_supply_overpay_requires_force(self, fee_client):
        """移除用品後已繳 > 新應繳，應 409 要求 force_refund。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(
                s,
                course_price=500,
                supply_price=500,
                paid_amount=1000,
                is_paid=True,
            )
            s.commit()
            reg_id = reg.id
            supply_record_id = (
                s.query(RegistrationSupply.id)
                .filter(RegistrationSupply.registration_id == reg_id)
                .scalar()
            )

        assert _login(client).status_code == 200

        res = client.delete(
            f"/api/activity/registrations/{reg_id}/supplies/{supply_record_id}"
        )
        assert res.status_code == 409, res.text

        with sf() as s:
            # 守衛未通過，用品不應被刪
            rs = (
                s.query(RegistrationSupply)
                .filter(RegistrationSupply.id == supply_record_id)
                .first()
            )
            assert rs is not None, "守衛未通過，用品紀錄不應被刪除"

    def test_remove_supply_force_refund_writes_refund(self, fee_client):
        """force_refund=true 時移除用品並寫退費沖帳。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(
                s,
                course_price=500,
                supply_price=500,
                paid_amount=1000,
                is_paid=True,
            )
            s.commit()
            reg_id = reg.id
            supply_record_id = (
                s.query(RegistrationSupply.id)
                .filter(RegistrationSupply.registration_id == reg_id)
                .scalar()
            )

        assert _login(client).status_code == 200

        res = client.delete(
            f"/api/activity/registrations/{reg_id}/supplies/{supply_record_id}"
            "?force_refund=true"
        )
        assert res.status_code == 200
        data = res.json()
        assert data["refunded_amount"] == 500
        assert data["paid_amount"] == 500
        assert data["total_amount"] == 500

        with sf() as s:
            # 用品已刪、reg.paid_amount 扣回
            rs = (
                s.query(RegistrationSupply)
                .filter(RegistrationSupply.id == supply_record_id)
                .first()
            )
            assert rs is None
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500

            refunds = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.registration_id == reg_id,
                    ActivityPaymentRecord.type == "refund",
                )
                .all()
            )
            assert len(refunds) == 1
            assert refunds[0].amount == 500
            assert refunds[0].payment_method == "系統補齊"

    def test_remove_supply_no_overpay_does_not_require_force(self, fee_client):
        """移除用品後 paid 仍在 new_total 以內，不需 force。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            # course=500、supply=500、只繳 400 → 移除用品後 new_total=500，paid=400 → 不超繳
            reg = _setup_reg(
                s,
                course_price=500,
                supply_price=500,
                paid_amount=400,
                is_paid=False,
            )
            s.commit()
            reg_id = reg.id
            supply_record_id = (
                s.query(RegistrationSupply.id)
                .filter(RegistrationSupply.registration_id == reg_id)
                .scalar()
            )

        assert _login(client).status_code == 200

        res = client.delete(
            f"/api/activity/registrations/{reg_id}/supplies/{supply_record_id}"
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["refunded_amount"] == 0
        assert data["total_amount"] == 500
        assert data["paid_amount"] == 400

        with sf() as s:
            # 不應留下任何 refund 紀錄
            refunds = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.registration_id == reg_id,
                    ActivityPaymentRecord.type == "refund",
                )
                .count()
            )
            assert refunds == 0


# ══════════════════════════════════════════════════════════════════════
# #6 — add_registration_course / supply 產生欠款時回 outstanding_amount
# ══════════════════════════════════════════════════════════════════════


class TestAddOutstandingAmount:
    def test_add_course_to_paid_reg_returns_outstanding(self, fee_client):
        """對已繳清的報名加新課程，response 應回 outstanding_amount。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=500, paid_amount=500, is_paid=True)
            from utils.academic import resolve_current_academic_term

            sy, sem = resolve_current_academic_term()
            extra = ActivityCourse(
                name="陶藝",
                price=800,
                capacity=30,
                allow_waitlist=True,
                school_year=sy,
                semester=sem,
            )
            s.add(extra)
            s.commit()
            reg_id = reg.id
            extra_course_id = extra.id

        assert _login(client).status_code == 200

        res = client.post(
            f"/api/activity/registrations/{reg_id}/courses",
            json={"course_id": extra_course_id},
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["total_amount"] == 1300  # 500 + 800
        assert data["paid_amount"] == 500
        assert data["outstanding_amount"] == 800
        assert data["payment_status"] == "partial"

    def test_add_supply_to_paid_reg_returns_outstanding(self, fee_client):
        """對已繳清的報名加用品，response 應回 outstanding_amount + 扣款差額。"""
        client, sf = fee_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, course_price=500, paid_amount=500, is_paid=True)
            from utils.academic import resolve_current_academic_term

            sy, sem = resolve_current_academic_term()
            supply = ActivitySupply(
                name="額外材料",
                price=300,
                school_year=sy,
                semester=sem,
            )
            s.add(supply)
            s.commit()
            reg_id = reg.id
            supply_id = supply.id

        assert _login(client).status_code == 200

        res = client.post(
            f"/api/activity/registrations/{reg_id}/supplies",
            json={"supply_id": supply_id},
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["total_amount"] == 800  # 500 + 300
        assert data["paid_amount"] == 500
        assert data["outstanding_amount"] == 300
        assert data["payment_status"] == "partial"
