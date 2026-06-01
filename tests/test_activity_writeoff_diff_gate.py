"""tests/test_activity_writeoff_diff_gate.py — PUT /registrations/{id}/payment
全額沖帳（is_paid=False）路徑之退費 diff 閘測試。

問題：update_payment 的 is_paid=False 分支漏了 require_approve_for_refund_diff，
導致無簽核權限員工對「calculator 建議退≈0、已繳 1000」的 reg 全額沖帳時繞過 diff 閘。

修正後：
- 無 ACTIVITY_PAYMENT_APPROVE + diff > 100 → 403
- 有 ACTIVITY_PAYMENT_APPROVE + diff > 100 → 200（放行）
- diff <= 100（current_paid ≈ suggested）→ 200（放行）
- current_paid == 0 → 不寫 refund record，diff 閘不影響（204/200）
"""

import os
import sys

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
    ActivityAttendance,
    ActivityCourse,
    ActivitySession,
    Base,
)
from tests.test_activity_pos import _create_admin, _login, _setup_reg
from tests.test_activity_refund_diff_verify import (
    _mark_attendance,
    _set_course_sessions,
)
from datetime import date


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "writeoff_diff.sqlite"
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


REFUND_REASON = "家長要求退費，已確認原因符合園所政策。"


def _writeoff_body(current_paid: int) -> dict:
    """PUT /registrations/{id}/payment body：全額沖帳（標記未繳費）。"""
    return {
        "is_paid": False,
        "confirm_refund_amount": current_paid,
        "refund_reason": REFUND_REASON,
    }


# ── 核心：旁路測試 ─────────────────────────────────────────────────────────


def test_writeoff_diff_over_threshold_blocks_staff(client):
    """is_paid=False 全額沖帳：diff > 100 + 無 ACTIVITY_PAYMENT_APPROVE → 403。

    設定：
    - course_price=1000, sessions=10, paid_amount=1000
    - 出席 8 堂（>=2/3×10）→ suggested=0
    - current_paid=1000, suggested=0, diff=1000 > 100 → 守衛觸發
    修正前此路徑回 200（旁路），修正後應 403。
    """
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(
            s, course_price=1000, supply_price=0, paid_amount=1000, is_paid=True
        )
        _set_course_sessions(s, "美術", 10)
        # 出席 8 堂 → T_served/T_total=0.8 >= 2/3 → suggested=0
        course = s.query(ActivityCourse).filter_by(name="美術").first()
        _mark_attendance(s, reg.id, course.id, 8)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.put(
        f"/api/activity/registrations/{reg_id}/payment",
        json=_writeoff_body(1000),
    )
    # 修正前：200（旁路）；修正後：403
    assert resp.status_code == 403, resp.json()
    detail = resp.json()["detail"]
    assert "偏離" in detail or "差" in detail or "1000" in detail


def test_writeoff_diff_over_threshold_passes_approver(client):
    """is_paid=False 全額沖帳：diff > 100 + 有 ACTIVITY_PAYMENT_APPROVE → 200 放行。

    同樣 diff=1000 的情境，但 user 有簽核權限 → 不觸發 diff 閘。
    """
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
        reg = _setup_reg(
            s, course_price=1000, supply_price=0, paid_amount=1000, is_paid=True
        )
        _set_course_sessions(s, "美術", 10)
        course = s.query(ActivityCourse).filter_by(name="美術").first()
        _mark_attendance(s, reg.id, course.id, 8)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.put(
        f"/api/activity/registrations/{reg_id}/payment",
        json=_writeoff_body(1000),
    )
    assert resp.status_code == 200, resp.json()


def test_writeoff_diff_below_threshold_passes_staff(client):
    """is_paid=False 全額沖帳：diff <= 100 → 一線員工放行。

    設定：
    - course_price=800, sessions=10, paid_amount=800
    - 0 堂出席 → suggested=800（not_started 全退）
    - current_paid=800, suggested=800, diff=0 → 不觸發
    """
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(
            s, course_price=800, supply_price=0, paid_amount=800, is_paid=True
        )
        _set_course_sessions(s, "美術", 10)
        # 0 出席 → T_served=0 → suggested=800（全退）→ diff=0
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.put(
        f"/api/activity/registrations/{reg_id}/payment",
        json=_writeoff_body(800),
    )
    assert resp.status_code == 200, resp.json()


def test_writeoff_zero_paid_skips_refund_record_no_diff_error(client):
    """current_paid=0 時：沖帳金額=0，不寫 refund record，diff 閘不誤擋。

    建議値亦為 0（未繳費），diff=0 → 不觸發；endpoint 正常回 200。
    """
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        # paid_amount=0, is_paid=False 模擬「從未繳費就要標記 is_paid=False」
        reg = _setup_reg(
            s, course_price=800, supply_price=0, paid_amount=0, is_paid=False
        )
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.put(
        f"/api/activity/registrations/{reg_id}/payment",
        json=_writeoff_body(0),
    )
    # current_paid=0 → confirm_refund_amount=0 符合；diff=0 → 不擋
    assert resp.status_code == 200, resp.json()
