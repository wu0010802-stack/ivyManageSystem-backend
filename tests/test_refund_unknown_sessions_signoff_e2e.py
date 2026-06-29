"""tests/test_refund_unknown_sessions_signoff_e2e.py — sessions IS NULL 強制簽核守衛端到端測試。

業主裁示（P3 修補，2026-06-22）：
  課程 sessions IS NULL 時 build_refund_suggestion 回傳 needs_manual_review=True，
  require_approve_for_refund_diff 守衛讀到後強制要求 ACTIVITY_PAYMENT_APPROVE，
  不論 diff 是否為 0。

  本測試驗證 6 個 call site 都已正確傳入 suggestion=，使守衛生效（production 路徑）。

涵蓋的端點（代表性 2~3 個）：
  (A) POST /pos/checkout           （type=refund）                — site 1 POS
  (B) DELETE /registrations/{id}   （force_refund，diff=0 情境）  — site 4 刪報名
  (C) DELETE /registrations/{id}/courses/{course_id}             — site 6 退課

各端點：
  - sessions=NULL 課程＋已繳費報名，無 APPROVE 權限帳號打退費端點 → 403
  - 同樣情境但有 APPROVE 權限 → 成功（200 / 201）

RED → GREEN 流程（按 TDD 要求）：
  修接線前：這些端點因未傳 suggestion=，guard 的 needs_manual_review 分支不觸發，
  全退（diff=0）時回 200/201。
  修接線後：guard 收到 needs_manual_review=True，即使 diff=0 也 403 阻擋無 APPROVE 帳號。
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
    ActivityRegistration,
    Base,
    RegistrationCourse,
    RegistrationSupply,
)
from tests.test_activity_pos import _create_admin, _login, _setup_reg

# ── Constants ────────────────────────────────────────────────────────────────

REFUND_REASON = "家長要求退費，已確認原因符合園所政策。"
TODAY = date.today().isoformat()


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    """共用 SQLite TestClient fixture（與 test_activity_force_refund_diff_gate 相同結構）。"""
    db_path = tmp_path / "refund_unknown_sessions_e2e.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


# ── Helpers ────────────────────────────────────────────────────────────────────


def _setup_null_sessions_reg(
    session, *, course_price: int = 800, paid_amount: int = 800
):
    """建立 sessions=NULL 課程 + 已全繳報名（diff=0 情境：建議退=amount_due=已繳）。

    _setup_reg 預設 sessions=NULL，建議退採 amount_due fallback（= course_price）。
    paid_amount == course_price 時 diff=0，純靠 needs_manual_review 觸發守衛。
    supply_price=0 排除用品干擾。
    """
    return _setup_reg(
        session,
        course_price=course_price,
        supply_price=0,
        paid_amount=paid_amount,
        is_paid=True,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# (A) POS checkout refund — site 1
# ═══════════════════════════════════════════════════════════════════════════════


class TestPosRefundUnknownSessions:
    """POS checkout type=refund，sessions=NULL 課程，diff=0 情境。"""

    def test_staff_without_approve_blocked_403(self, client):
        """sessions=NULL，實退=建議退=800（diff=0），無 ACTIVITY_PAYMENT_APPROVE → 403。

        RED 描述：修接線前 suggestion=None 未傳入，needs_manual_review 分支被略過，回 201。
        GREEN 描述：修接線後守衛讀到 needs_manual_review=True，403 強制簽核。
        """
        c, sf = client
        with sf() as s:
            _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
            reg = _setup_null_sessions_reg(s, course_price=800, paid_amount=800)
            s.commit()
            reg_id = reg.id

        _login(c)
        resp = c.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 800}],
                "payment_method": "現金",
                "payment_date": TODAY,
                "type": "refund",
                "notes": REFUND_REASON,
                "idempotency_key": "REFUNDSIGNOFF-0001",
            },
        )
        assert (
            resp.status_code == 403
        ), f"預期 403（sessions IS NULL 強制簽核），實際：{resp.status_code} {resp.json()}"
        detail = resp.json().get("detail", "")
        assert (
            "sessions" in detail
            or "堂數" in detail
            or "ACTIVITY_PAYMENT_APPROVE" in detail
        ), f"403 detail 應提到 sessions/堂數/ACTIVITY_PAYMENT_APPROVE，實際：{detail}"

    def test_approver_allowed(self, client):
        """sessions=NULL，有 ACTIVITY_PAYMENT_APPROVE → 放行（201）。"""
        c, sf = client
        with sf() as s:
            _create_admin(
                s,
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "ACTIVITY_PAYMENT_APPROVE",
                ],
            )
            reg = _setup_null_sessions_reg(s, course_price=800, paid_amount=800)
            s.commit()
            reg_id = reg.id

        _login(c)
        resp = c.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 800}],
                "payment_method": "現金",
                "payment_date": TODAY,
                "type": "refund",
                "notes": REFUND_REASON,
                "idempotency_key": "REFUNDSIGNOFF-0002",
            },
        )
        assert (
            resp.status_code == 201
        ), f"預期 201（有 APPROVE 放行），實際：{resp.status_code} {resp.json()}"


# ═══════════════════════════════════════════════════════════════════════════════
# (B) DELETE /registrations/{id} force_refund — site 4
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeleteRegistrationUnknownSessions:
    """刪報名 force_refund，sessions=NULL 課程，diff=0（全退=建議退）情境。"""

    def test_staff_without_approve_blocked_403(self, client):
        """sessions=NULL，paid=建議退（diff=0），無 ACTIVITY_PAYMENT_APPROVE → 403。

        刪報名的 diff 路徑：paid_before=course_price=800，suggested=amount_due=800，
        diff=|800-800|=0；舊路徑（未傳 suggestion=）不觸發守衛，回 200。
        新路徑傳入 suggestion=_sugg → needs_manual_review=True → 403。
        """
        c, sf = client
        with sf() as s:
            _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
            reg = _setup_null_sessions_reg(s, course_price=800, paid_amount=800)
            s.commit()
            reg_id = reg.id

        _login(c)
        resp = c.delete(
            f"/api/activity/registrations/{reg_id}",
            params={"force_refund": "true", "refund_reason": REFUND_REASON},
        )
        assert (
            resp.status_code == 403
        ), f"預期 403（sessions IS NULL + diff=0 仍強制簽核），實際：{resp.status_code} {resp.json()}"
        detail = resp.json().get("detail", "")
        assert (
            "sessions" in detail
            or "堂數" in detail
            or "ACTIVITY_PAYMENT_APPROVE" in detail
        )

    def test_approver_allowed(self, client):
        """sessions=NULL，有 ACTIVITY_PAYMENT_APPROVE → 刪除成功（200）。"""
        c, sf = client
        with sf() as s:
            _create_admin(
                s,
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "ACTIVITY_PAYMENT_APPROVE",
                ],
            )
            reg = _setup_null_sessions_reg(s, course_price=800, paid_amount=800)
            s.commit()
            reg_id = reg.id

        _login(c)
        resp = c.delete(
            f"/api/activity/registrations/{reg_id}",
            params={"force_refund": "true", "refund_reason": REFUND_REASON},
        )
        assert (
            resp.status_code == 200
        ), f"預期 200（有 APPROVE 放行），實際：{resp.status_code} {resp.json()}"


# ═══════════════════════════════════════════════════════════════════════════════
# (C) DELETE /registrations/{id}/courses/{course_id} — site 6 退課
# ═══════════════════════════════════════════════════════════════════════════════


class TestWithdrawCourseUnknownSessions:
    """退課 force_refund，sessions=NULL 課程，diff=0（全退=amount_due fallback）情境。

    退課的 diff 路徑：preview_refund=paid_amount=800，calculator 遇 sessions IS NULL 採
    amount_due fallback=800，suggested_for_course=800，diff=|800-800|=0；
    同時 needs_manual_review=True → 守衛強制 403（無 APPROVE）。
    """

    def test_staff_without_approve_blocked_403(self, client):
        """sessions=NULL，退課退款等於建議退（diff=0），無 APPROVE → 403。"""
        c, sf = client
        with sf() as s:
            _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
            reg = _setup_null_sessions_reg(s, course_price=800, paid_amount=800)
            course = (
                s.query(ActivityCourse).filter(ActivityCourse.name == "美術").first()
            )
            s.commit()
            reg_id = reg.id
            course_id = course.id

        _login(c)
        resp = c.delete(
            f"/api/activity/registrations/{reg_id}/courses/{course_id}",
            params={"force_refund": "true", "refund_reason": REFUND_REASON},
        )
        assert (
            resp.status_code == 403
        ), f"預期 403（sessions IS NULL + diff=0 仍強制簽核），實際：{resp.status_code} {resp.json()}"
        detail = resp.json().get("detail", "")
        assert (
            "sessions" in detail
            or "堂數" in detail
            or "ACTIVITY_PAYMENT_APPROVE" in detail
        ), f"403 detail 應提到 sessions/堂數/ACTIVITY_PAYMENT_APPROVE，實際：{detail}"

    def test_approver_allowed(self, client):
        """sessions=NULL，有 ACTIVITY_PAYMENT_APPROVE → 退課成功（200）。"""
        c, sf = client
        with sf() as s:
            _create_admin(
                s,
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "ACTIVITY_PAYMENT_APPROVE",
                ],
            )
            reg = _setup_null_sessions_reg(s, course_price=800, paid_amount=800)
            course = (
                s.query(ActivityCourse).filter(ActivityCourse.name == "美術").first()
            )
            s.commit()
            reg_id = reg.id
            course_id = course.id

        _login(c)
        resp = c.delete(
            f"/api/activity/registrations/{reg_id}/courses/{course_id}",
            params={"force_refund": "true", "refund_reason": REFUND_REASON},
        )
        assert (
            resp.status_code == 200
        ), f"預期 200（有 APPROVE 放行），實際：{resp.status_code} {resp.json()}"


# ═══════════════════════════════════════════════════════════════════════════════
# (D) 防迴歸：sessions 已知時 diff=0 不觸發（確保接線不破壞原有行為）
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegressionSessionsKnown:
    """sessions 已知時不因 needs_manual_review 觸發守衛（防迴歸）。"""

    def test_pos_sessions_known_diff_zero_staff_passes(self, client):
        """POS 退費：sessions 已知，0 出席（建議全退），diff=0，一線員工放行。"""
        from tests.test_activity_refund_diff_verify import _set_course_sessions

        c, sf = client
        with sf() as s:
            _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
            reg = _setup_reg(
                s, course_price=800, supply_price=0, paid_amount=800, is_paid=True
            )
            _set_course_sessions(
                s, "美術", 10
            )  # sessions=10（已知）；0 出席 → suggested=800
            s.commit()
            reg_id = reg.id

        _login(c)
        resp = c.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 800}],
                "payment_method": "現金",
                "payment_date": TODAY,
                "type": "refund",
                "notes": REFUND_REASON,
                "idempotency_key": "REFUNDSIGNOFF-0003",
            },
        )
        assert resp.status_code in (
            200,
            201,
        ), f"sessions 已知 + diff=0 應放行，實際：{resp.status_code} {resp.json()}"

    def test_withdraw_course_sessions_known_diff_zero_staff_passes(self, client):
        """退課：sessions 已知，0 出席（建議全退），diff=0，一線員工放行。"""
        from tests.test_activity_refund_diff_verify import _set_course_sessions

        c, sf = client
        with sf() as s:
            _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
            reg = _setup_reg(
                s, course_price=800, supply_price=0, paid_amount=800, is_paid=True
            )
            _set_course_sessions(
                s, "美術", 10
            )  # 0 出席 → not_started 全退 → suggested=800
            course = (
                s.query(ActivityCourse).filter(ActivityCourse.name == "美術").first()
            )
            s.commit()
            reg_id = reg.id
            course_id = course.id

        _login(c)
        resp = c.delete(
            f"/api/activity/registrations/{reg_id}/courses/{course_id}",
            params={"force_refund": "true", "refund_reason": REFUND_REASON},
        )
        assert (
            resp.status_code == 200
        ), f"sessions 已知 + diff=0 應放行，實際：{resp.status_code} {resp.json()}"
