"""tests/test_activity_fee_strictness.py — 才藝收費嚴格化（2026-04-24）回歸測試。

覆蓋：
- #1 DELETE payment 改軟刪、需 ACTIVITY_PAYMENT_APPROVE、reason 必填
- #2 退費必填 notes（≥5 字）+ 金額 > 1000 需簽核權限
- #3 batch_update_payment 禁 is_paid=False；update_payment 要 confirm_refund_amount
- #4 public_update 手機衝突擴大至全域（跨學期亦擋）
- #5 get_registration_payments 的 operator 去敏化
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
    Base,
    RegistrationCourse,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def strict_client(tmp_path):
    db_path = tmp_path / "strict.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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


def _create_user(
    session,
    *,
    username: str,
    permissions: int,
    role: str = "admin",
    password: str = "TempPass123",
) -> User:
    u = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
    )
    session.add(u)
    session.flush()
    return u


def _login(client: TestClient, username: str, password: str = "TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _setup_reg(
    session,
    *,
    student_name: str = "王小明",
    parent_phone: str = "0912345678",
    school_year: int = None,
    semester: int = None,
    course_price: int = 500,
    paid_amount: int = 0,
    is_paid: bool = False,
) -> ActivityRegistration:
    from utils.academic import resolve_current_academic_term

    if school_year is None or semester is None:
        school_year, semester = resolve_current_academic_term()
    course = (
        session.query(ActivityCourse)
        .filter(
            ActivityCourse.name == "美術",
            ActivityCourse.school_year == school_year,
            ActivityCourse.semester == semester,
        )
        .first()
    )
    if not course:
        course = ActivityCourse(
            name="美術",
            price=course_price,
            capacity=30,
            allow_waitlist=True,
            school_year=school_year,
            semester=semester,
        )
        session.add(course)
        session.flush()
    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        parent_phone=parent_phone,
        class_name="大班",
        paid_amount=paid_amount,
        is_paid=is_paid,
        is_active=True,
        school_year=school_year,
        semester=semester,
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
    session.commit()
    return reg


# ═══════════════════════════════════════════════════════════════════════
# #1 DELETE payment 軟刪
# ═══════════════════════════════════════════════════════════════════════


class TestDeletePaymentSoftDelete:
    def test_delete_requires_payment_approve_permission(self, strict_client):
        """僅有 ACTIVITY_WRITE 者不可軟刪 payment（需 ACTIVITY_PAYMENT_APPROVE）。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="write_only",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            rec = ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=500,
                payment_date=date.today(),
                payment_method="現金",
                operator="someone",
            )
            s.add(rec)
            s.commit()
            reg_id, rec_id = reg.id, rec.id

        assert _login(client, "write_only").status_code == 200
        res = client.request(
            "DELETE",
            f"/api/activity/registrations/{reg_id}/payments/{rec_id}",
            json={"reason": "測試權限守衛"},
        )
        assert res.status_code == 403

    def test_delete_requires_reason_min_length(self, strict_client):
        """軟刪 reason 少於 5 字應 422（Pydantic）。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="boss",
                permissions=Permission.ACTIVITY_READ
                | Permission.ACTIVITY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            rec = ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=500,
                payment_date=date.today(),
                payment_method="現金",
            )
            s.add(rec)
            s.commit()
            reg_id, rec_id = reg.id, rec.id

        assert _login(client, "boss").status_code == 200
        res = client.request(
            "DELETE",
            f"/api/activity/registrations/{reg_id}/payments/{rec_id}",
            json={"reason": "誤"},  # 僅 1 字
        )
        assert res.status_code == 422

    def test_delete_soft_void_keeps_row_and_recalculates(self, strict_client):
        """有簽核權限者軟刪：原 row 仍存在、voided_* 填值、paid_amount 重算。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="boss",
                permissions=Permission.ACTIVITY_READ
                | Permission.ACTIVITY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            rec = ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=500,
                payment_date=date.today(),
                payment_method="現金",
            )
            s.add(rec)
            s.commit()
            reg_id, rec_id = reg.id, rec.id

        assert _login(client, "boss").status_code == 200
        res = client.request(
            "DELETE",
            f"/api/activity/registrations/{reg_id}/payments/{rec_id}",
            json={"reason": "單據誤植，客戶要求撤回"},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            row = s.query(ActivityPaymentRecord).get(rec_id)
            assert row is not None, "軟刪後 row 仍應存在（稽核軌跡）"
            assert row.voided_at is not None
            assert row.voided_by == "boss"
            assert "單據誤植" in (row.void_reason or "")

            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 0, "voided 紀錄不計入 paid_amount"
            assert reg.is_paid is False

    def test_cannot_void_twice(self, strict_client):
        """已軟刪者再次 DELETE 應 409。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="boss",
                permissions=Permission.ACTIVITY_READ
                | Permission.ACTIVITY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            rec = ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=500,
                payment_date=date.today(),
                payment_method="現金",
            )
            s.add(rec)
            s.commit()
            reg_id, rec_id = reg.id, rec.id

        assert _login(client, "boss").status_code == 200
        first = client.request(
            "DELETE",
            f"/api/activity/registrations/{reg_id}/payments/{rec_id}",
            json={"reason": "第一次軟刪理由完整"},
        )
        assert first.status_code == 200
        second = client.request(
            "DELETE",
            f"/api/activity/registrations/{reg_id}/payments/{rec_id}",
            json={"reason": "第二次重複軟刪"},
        )
        assert second.status_code == 409


# ═══════════════════════════════════════════════════════════════════════
# #2 退費原因必填 + 金額閾值審批
# ═══════════════════════════════════════════════════════════════════════


class TestRefundRequirements:
    def test_add_refund_requires_notes(self, strict_client):
        """單筆 refund 的 notes < 5 字：Pydantic 層 422。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="write_only",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client, "write_only").status_code == 200
        res = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "refund",
                "amount": 500,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "notes": "短",
            },
        )
        assert res.status_code == 422

    def test_large_refund_requires_approve_permission(self, strict_client):
        """> 1000 的退費需 ACTIVITY_PAYMENT_APPROVE；無則 403。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="write_only",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, course_price=5000, paid_amount=5000, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client, "write_only").status_code == 200
        res = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "refund",
                "amount": 2000,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "notes": "大額退費需簽核（家長申請退費）",
            },
        )
        assert res.status_code == 403

    def test_small_refund_no_approve_needed(self, strict_client):
        """≤ 1000 的退費一般 WRITE 即可。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="write_only",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client, "write_only").status_code == 200
        res = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "refund",
                "amount": 500,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "notes": "小額退費不需簽核（家長申請退費）",
            },
        )
        assert res.status_code == 201, res.text

    def test_pos_checkout_large_refund_requires_approve(self, strict_client):
        """POS 退費 total > 1000 時無 ACTIVITY_PAYMENT_APPROVE 應 403。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="write_only",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, course_price=5000, paid_amount=5000, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client, "write_only").status_code == 200
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 1500}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "type": "refund",
                "notes": "POS 大額退費需簽核（家長申請）",
            },
        )
        assert res.status_code == 403


# ═══════════════════════════════════════════════════════════════════════
# #3 batch/update 收緊
# ═══════════════════════════════════════════════════════════════════════


class TestBatchPaymentLocked:
    def test_batch_is_paid_false_rejected_422(self, strict_client):
        """批次 is_paid=False 一律 422（schema 層禁用）。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="boss",
                permissions=Permission.ACTIVITY_READ
                | Permission.ACTIVITY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client, "boss").status_code == 200
        res = client.put(
            "/api/activity/registrations/batch-payment",
            json={"ids": [reg_id], "is_paid": False},
        )
        assert res.status_code == 422


class TestUpdatePaymentConfirmAmount:
    def test_large_refund_without_approve_rejected(self, strict_client):
        """update_payment is_paid=False 沖帳金額 > 1000 需簽核權限；無則 403。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="write_only",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, course_price=5000, paid_amount=5000, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client, "write_only").status_code == 200
        res = client.put(
            f"/api/activity/registrations/{reg_id}/payment",
            json={
                "is_paid": False,
                "confirm_refund_amount": 5000,
                "refund_reason": "無簽核權限嘗試大額沖帳（測試）",
            },
        )
        assert res.status_code == 403


# ═══════════════════════════════════════════════════════════════════════
# #4 手機衝突擴大至全域
# ═══════════════════════════════════════════════════════════════════════


class TestPhoneConflictCrossTerm:
    def test_cannot_change_to_phone_used_by_different_term_registration(
        self, strict_client
    ):
        """甲生（2026春）想把手機改成乙生（2026秋）正在用的號碼 → 409。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="admin",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            # 啟用報名
            from models.database import ActivityRegistrationSettings

            s.add(ActivityRegistrationSettings(is_open=True))
            # 甲生：2026 春（school_year=115, semester=2）
            reg_a = _setup_reg(
                s,
                student_name="甲生",
                parent_phone="0911111111",
                school_year=115,
                semester=2,
            )
            # 乙生：2026 秋（school_year=115, semester=1）→ 不同學期，同手機 09xx2
            _ = _setup_reg(
                s,
                student_name="乙生",
                parent_phone="0922222222",
                school_year=115,
                semester=1,
            )
            s.commit()
            reg_a_id = reg_a.id

        # 甲家長嘗試把 09xx1 改成 09xx2（乙家長學期）
        res = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_a_id,
                "name": "甲生",
                "birthday": "2020-01-01",
                "parent_phone": "0911111111",
                "new_parent_phone": "0922222222",
                "class": "大班",
                "courses": [{"name": "美術"}],
                "supplies": [],
            },
        )
        assert res.status_code == 409


# ═══════════════════════════════════════════════════════════════════════
# #5 operator 去敏化
# ═══════════════════════════════════════════════════════════════════════


class TestOperatorDesensitization:
    def test_non_approve_viewer_sees_masked_operator(self, strict_client):
        """僅具 ACTIVITY_READ 者看到的 operator 會被遮蔽首字後 ***。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="read_only",
                permissions=Permission.ACTIVITY_READ,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            s.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="payment",
                    amount=500,
                    payment_date=date.today(),
                    payment_method="現金",
                    operator="fee_admin",
                )
            )
            s.commit()
            reg_id = reg.id

        assert _login(client, "read_only").status_code == 200
        res = client.get(f"/api/activity/registrations/{reg_id}/payments")
        assert res.status_code == 200
        records = res.json()["records"]
        assert len(records) == 1
        # "fee_admin" → "f***"
        assert records[0]["operator"] == "f***"

    def test_approve_viewer_sees_full_operator(self, strict_client):
        """具 ACTIVITY_PAYMENT_APPROVE 者看到完整 operator。"""
        client, sf = strict_client
        with sf() as s:
            _create_user(
                s,
                username="boss",
                permissions=Permission.ACTIVITY_READ
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            reg = _setup_reg(s, paid_amount=500, is_paid=True)
            s.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="payment",
                    amount=500,
                    payment_date=date.today(),
                    payment_method="現金",
                    operator="fee_admin",
                )
            )
            s.commit()
            reg_id = reg.id

        assert _login(client, "boss").status_code == 200
        res = client.get(f"/api/activity/registrations/{reg_id}/payments")
        assert res.status_code == 200
        records = res.json()["records"]
        assert records[0]["operator"] == "fee_admin"
