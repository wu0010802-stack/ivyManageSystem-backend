"""驗證 legacy 自動沖帳路徑（標記未繳/退課/移除用品/刪除報名）改用累積退費判斷。

威脅：reg 已退 NT$600（add_registration_payment / pos refund 主路徑）→ 再透過
退課 force_refund=true 自動沖帳 NT$900（單筆 < NT$1000）→ 兩筆累積 NT$1500
但都不需簽核，繞過 ACTIVITY_PAYMENT_APPROVE。

修補：四條路徑改用 require_approve_for_cumulative_refund，把 prior_refunded
（voided=NULL）+ this_refund 一起跨門檻判斷。

Refs: 邏輯漏洞 audit 2026-05-07 P0 (#8)。
"""

import os
import sys
from datetime import date, datetime

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
)
from utils.permissions import Permission

from tests.test_activity_pos import _create_admin, _login, _setup_reg

# 不含 ACTIVITY_PAYMENT_APPROVE → 應被累積簽核擋下
NO_APPROVE_PERMS = Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE


@pytest.fixture
def cumulative_client(tmp_path):
    db_path = tmp_path / "cumulative_refund.sqlite"
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


def _seed_prior_refund(session, reg_id: int, amount: int):
    """直接寫一筆既有的 refund 紀錄（voided=None），模擬先前已退過部分款。"""
    session.add(
        ActivityPaymentRecord(
            registration_id=reg_id,
            type="refund",
            amount=amount,
            payment_date=date.today(),
            payment_method="現金",
            notes=f"既有部分退費 NT${amount}",
            operator="prior_admin",
        )
    )
    session.flush()


class TestMarkUnpaidCumulativeRefund:
    """PUT /api/activity/registrations/{id}/payment 的標記未繳全額沖帳。"""

    def test_mark_unpaid_blocked_when_cumulative_exceeds_threshold(
        self, cumulative_client
    ):
        client, sf = cumulative_client
        with sf() as s:
            _create_admin(s, permissions=NO_APPROVE_PERMS)
            reg = _setup_reg(s, student_name="王測試", paid_amount=900)
            s.commit()
            reg_id = reg.id
            # 先塞一筆 NT$600 prior refund（單筆未跨閾值 NT$1000，當時不需簽核）
            _seed_prior_refund(s, reg_id, 600)
            s.commit()

        assert _login(client).status_code == 200

        # 標記未繳 → 應沖 NT$900（單筆 < NT$1000）
        # 但 600 + 900 = 1500 > 1000 → 累積簽核應擋下
        res = client.put(
            f"/api/activity/registrations/{reg_id}/payment",
            json={
                "is_paid": False,
                "confirm_refund_amount": 900,
                "refund_reason": "家長要求改用銀行轉帳退款，必須作廢已繳金額",
            },
        )
        assert res.status_code == 403, (
            f"標記未繳累積退費 NT$1500 必須簽核才能執行；status={res.status_code}, "
            f"body={res.text}"
        )
        detail = res.json().get("detail", "")
        assert "累積" in detail or "簽核" in detail


class TestWithdrawCourseCumulativeRefund:
    """POST /api/activity/registrations/{id}/courses/{course_id}/withdraw force_refund。"""

    def test_withdraw_force_refund_blocked_when_cumulative_exceeds(
        self, cumulative_client
    ):
        """_setup_reg 預設 1500 課 + 500 用品 (total=2000)，paid_amount=1100：
        - 美術課 NT$1500 退掉 → after_total = 500
        - preview_refund = 1100 - 500 = 600（單筆 < 閾值 NT$1000）
        - prior_refund = 500（單筆當初也 < 閾值）
        - 累積 = 1100 > 1000 → 應簽核
        """
        client, sf = cumulative_client
        with sf() as s:
            _create_admin(s, permissions=NO_APPROVE_PERMS)
            reg = _setup_reg(s, student_name="陳測試", paid_amount=1100)
            s.commit()
            reg_id = reg.id
            rc = (
                s.query(RegistrationCourse)
                .filter(RegistrationCourse.registration_id == reg_id)
                .first()
            )
            assert rc is not None
            course_id = rc.course_id
            _seed_prior_refund(s, reg_id, 500)
            s.commit()

        assert _login(client).status_code == 200

        res = client.delete(
            f"/api/activity/registrations/{reg_id}/courses/{course_id}",
            params={
                "force_refund": "true",
                "refund_reason": "家長要求改報其他課程，需要先退費",
            },
        )
        assert (
            res.status_code == 403
        ), f"退課累積退費必須簽核才能執行；status={res.status_code}, body={res.text}"
        detail = res.json().get("detail", "")
        assert "累積" in detail or "簽核" in detail


class TestPriorVoidedRefundDoesNotCount:
    """voided=NOT NULL 的舊退費不應計入累積（避免錯擋合法操作）。"""

    def test_voided_prior_refund_excluded_from_cumulative(self, cumulative_client):
        client, sf = cumulative_client
        with sf() as s:
            _create_admin(s, permissions=NO_APPROVE_PERMS)
            reg = _setup_reg(s, student_name="李測試", paid_amount=900)
            s.commit()
            reg_id = reg.id
            # 一筆「已被作廢」的舊退費 NT$600 — 不計入累積
            session_now = datetime.utcnow()
            session_now_aware = session_now.replace()
            session_obj = ActivityPaymentRecord(
                registration_id=reg_id,
                type="refund",
                amount=600,
                payment_date=date.today(),
                payment_method="現金",
                notes="舊退費已作廢",
                operator="x",
                voided_at=session_now_aware,
                voided_by="admin_b",
            )
            s.add(session_obj)
            s.commit()

        assert _login(client).status_code == 200

        # 本次標記未繳沖 NT$900：voided 那筆不計入 → 累積仍是 NT$900 < NT$1000 → 200
        res = client.put(
            f"/api/activity/registrations/{reg_id}/payment",
            json={
                "is_paid": False,
                "confirm_refund_amount": 900,
                "refund_reason": "改採其他付款方式，需要作廢前次紀錄",
            },
        )
        assert (
            res.status_code == 200
        ), f"voided 紀錄不該計入累積；status={res.status_code}, body={res.text}"
