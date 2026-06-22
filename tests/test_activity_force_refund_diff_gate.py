"""tests/test_activity_force_refund_diff_gate.py

三條「force_refund 自動沖帳」路徑之退費 diff 閘測試：
- DELETE /registrations/{id}                       （刪報名 force_refund）
- DELETE /registrations/{id}/courses/{course_id}   （退課 force_refund）
- DELETE /registrations/{id}/supplies/{rs_id}       （退用品 force_refund）

問題：上述三路徑只套 require_refund_reason + require_approve_for_cumulative_refund
（總額閘），漏了 require_approve_for_refund_diff（偏離建議值閘）。後果：一線員工
可對「calculator 建議退≈0、已繳 1000」的 reg 整筆沖帳（diff=1000）而無簽核制衡，
只要單筆 ≤ 累積閾值（1000）即可繞過——與 POS / POST payments / writeoff 路徑不一致。

修正後（與 PUT /payment writeoff 閘對齊）：
- 無 ACTIVITY_PAYMENT_APPROVE + diff > 100 → 403
- 有 ACTIVITY_PAYMENT_APPROVE + diff > 100 → 200（放行）
- diff <= 100（實退 ≈ 建議）→ 一線員工放行（200）

註：refund=1000 時 cumulative 閘為 `> 1000`（不觸發），故 403 必來自 diff 閘，
能乾淨隔離本次新增的偏離閘。
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
    ActivityCourse,
    Base,
    RegistrationSupply,
)
from tests.test_activity_pos import _create_admin, _login, _setup_reg
from tests.test_activity_refund_diff_verify import (
    _mark_attendance,
    _set_course_sessions,
)

REFUND_REASON = "家長要求退費，已確認原因符合園所政策。"


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "force_refund_diff.sqlite"
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


def _q(force: bool = True, reason: str = REFUND_REASON) -> dict:
    return {"force_refund": "true" if force else "false", "refund_reason": reason}


# ── 刪報名 force_refund ─────────────────────────────────────────────────────


def test_delete_registration_diff_over_threshold_blocks_staff(client):
    """刪報名 force_refund：建議退=0、實退=1000、diff=1000 → 一線員工 403。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(
            s, course_price=1000, supply_price=0, paid_amount=1000, is_paid=True
        )
        _set_course_sessions(s, "美術", 10)
        course = s.query(ActivityCourse).filter_by(name="美術").first()
        _mark_attendance(s, reg.id, course.id, 8)  # 8/10 >= 2/3 → suggested=0
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.delete(f"/api/activity/registrations/{reg_id}", params=_q())
    assert resp.status_code == 403, resp.json()
    assert "偏離" in resp.json()["detail"]


def test_delete_registration_diff_over_threshold_passes_approver(client):
    """刪報名 force_refund：同 diff=1000，但有 ACTIVITY_PAYMENT_APPROVE → 放行。"""
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
    resp = c.delete(f"/api/activity/registrations/{reg_id}", params=_q())
    assert resp.status_code == 200, resp.json()


def test_delete_registration_diff_below_threshold_passes_staff(client):
    """刪報名 force_refund：0 出席 → 建議退=800=實退、diff=0 → 一線員工放行。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(
            s, course_price=800, supply_price=0, paid_amount=800, is_paid=True
        )
        _set_course_sessions(s, "美術", 10)  # 0 出席 → not_started 全退建議=800
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.delete(f"/api/activity/registrations/{reg_id}", params=_q())
    assert resp.status_code == 200, resp.json()


# ── 退課 force_refund ───────────────────────────────────────────────────────


def test_withdraw_course_diff_over_threshold_blocks_staff(client):
    """退課 force_refund：該課建議退=0、實退=1000、diff=1000 → 一線員工 403。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(
            s, course_price=1000, supply_price=0, paid_amount=1000, is_paid=True
        )
        _set_course_sessions(s, "美術", 10)
        course = s.query(ActivityCourse).filter_by(name="美術").first()
        _mark_attendance(s, reg.id, course.id, 8)
        s.commit()
        reg_id, course_id = reg.id, course.id

    _login(c)
    resp = c.delete(
        f"/api/activity/registrations/{reg_id}/courses/{course_id}", params=_q()
    )
    assert resp.status_code == 403, resp.json()
    assert "偏離" in resp.json()["detail"]


def test_withdraw_course_diff_over_threshold_passes_approver(client):
    """退課 force_refund：同 diff=1000，但有簽核權限 → 放行。"""
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
        reg_id, course_id = reg.id, course.id

    _login(c)
    resp = c.delete(
        f"/api/activity/registrations/{reg_id}/courses/{course_id}", params=_q()
    )
    assert resp.status_code == 200, resp.json()


def test_withdraw_course_diff_below_threshold_passes_staff(client):
    """退課 force_refund：0 出席 → 該課建議退=800=實退、diff=0 → 一線員工放行。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(
            s, course_price=800, supply_price=0, paid_amount=800, is_paid=True
        )
        _set_course_sessions(s, "美術", 10)
        course = s.query(ActivityCourse).filter_by(name="美術").first()
        s.commit()
        reg_id, course_id = reg.id, course.id

    _login(c)
    resp = c.delete(
        f"/api/activity/registrations/{reg_id}/courses/{course_id}", params=_q()
    )
    assert resp.status_code == 200, resp.json()


# ── 退用品 force_refund ─────────────────────────────────────────────────────


def test_remove_supply_diff_over_threshold_blocks_staff(client):
    """退用品 force_refund：用品建議退一律=0、實退=1000、diff=1000 → 一線員工 403。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(
            s, course_price=0, supply_price=1000, paid_amount=1000, is_paid=True
        )
        s.commit()
        reg_id = reg.id
        rs_id = s.query(RegistrationSupply).filter_by(registration_id=reg_id).first().id

    _login(c)
    resp = c.delete(
        f"/api/activity/registrations/{reg_id}/supplies/{rs_id}", params=_q()
    )
    assert resp.status_code == 403, resp.json()
    assert "偏離" in resp.json()["detail"]


def test_remove_supply_diff_over_threshold_passes_approver(client):
    """退用品 force_refund：同 diff=1000，但有簽核權限 → 放行。"""
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
            s, course_price=0, supply_price=1000, paid_amount=1000, is_paid=True
        )
        s.commit()
        reg_id = reg.id
        rs_id = s.query(RegistrationSupply).filter_by(registration_id=reg_id).first().id

    _login(c)
    resp = c.delete(
        f"/api/activity/registrations/{reg_id}/supplies/{rs_id}", params=_q()
    )
    assert resp.status_code == 200, resp.json()


def test_remove_supply_diff_below_threshold_passes_staff(client):
    """退用品 force_refund：小額用品（100）實退=100、diff=100 不超門檻 → 放行。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(
            s, course_price=0, supply_price=100, paid_amount=100, is_paid=True
        )
        s.commit()
        reg_id = reg.id
        rs_id = s.query(RegistrationSupply).filter_by(registration_id=reg_id).first().id

    _login(c)
    resp = c.delete(
        f"/api/activity/registrations/{reg_id}/supplies/{rs_id}", params=_q()
    )
    assert resp.status_code == 200, resp.json()
