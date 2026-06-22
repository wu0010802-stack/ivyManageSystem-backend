"""tests/test_remove_supply_audit_entity.py — 移除用品退費沖帳的 audit_entity_id 覆寫

Bug（P2）：remove_registration_supply 缺少 request: Request 注入與
request.state.audit_entity_id 設定，AuditMiddleware fallback
_parse_entity_id 會抓 URL 尾段的 supply_record_id 而非 registration_id，
導致依報名 ID 彙整退費稽核事件時對不上。

修法比照 withdraw_course：注入 Request + commit 後設 audit_entity_id / summary / changes。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
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
def supply_audit_client(tmp_path):
    db_path = tmp_path / "supply_audit.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
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

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _create_admin(session):
    user = User(
        username="supply_admin",
        password_hash=hash_password("TempPass123"),
        role="admin",
        permission_names=[
            "ACTIVITY_READ",
            "ACTIVITY_WRITE",
            "ACTIVITY_PAYMENT_APPROVE",
        ],
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient):
    return client.post(
        "/api/auth/login",
        json={"username": "supply_admin", "password": "TempPass123"},
    )


def _setup_reg_with_supply(session, *, supply_price=500, paid_amount=500):
    """建立一筆報名：含一門課 + 一件用品，paid_amount 由呼叫端指定。

    為確保 reg_id != rs_id（讓 entity_id 比對有鑑別力），先建一筆空白 ActivityRegistration
    佔掉 id=1，使真正的測試 reg.id >= 2，而 RegistrationSupply id 從 1 起，必然不同。
    """
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()

    # 先佔一筆 reg id，讓後續 reg_id > 1，與 rs_id=1 必然不同
    dummy_reg = ActivityRegistration(
        student_name="佔位",
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=0,
        is_paid=False,
        is_active=False,
        school_year=sy,
        semester=sem,
    )
    session.add(dummy_reg)
    session.flush()

    course = ActivityCourse(
        name="測試課程",
        price=300,
        capacity=30,
        allow_waitlist=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()

    supply = ActivitySupply(
        name="測試用品",
        price=supply_price,
        school_year=sy,
        semester=sem,
    )
    session.add(supply)
    session.flush()

    reg = ActivityRegistration(
        student_name="測試學生",
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=paid_amount,
        is_paid=(paid_amount >= (300 + supply_price)),
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
            price_snapshot=300,
        )
    )

    rs = RegistrationSupply(
        registration_id=reg.id,
        supply_id=supply.id,
        price_snapshot=supply_price,
    )
    session.add(rs)
    session.flush()
    session.commit()

    return reg, rs


# ── 主測試 ────────────────────────────────────────────────────────────────


class TestRemoveSupplyAuditEntityId:
    """remove_registration_supply 的 audit_entity_id 必須覆寫為 registration_id。

    URL 尾段是 supply_record_id；若未設 request.state.audit_entity_id，
    middleware fallback _parse_entity_id 會取 supply_record_id，導致
    依報名 ID 彙整退費稽核事件時對不上。
    """

    def _make_capture_app(self, captured: dict):
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

    def test_remove_supply_no_refund_sets_registration_as_entity(
        self, supply_audit_client
    ):
        """force_refund=false（無退費）時，audit_entity_id 仍須為 registration_id。"""
        _, sf = supply_audit_client
        with sf() as s:
            _create_admin(s)
            # paid_amount=0：移除後不會超繳，不需 force_refund
            reg, rs = _setup_reg_with_supply(s, supply_price=500, paid_amount=0)
            reg_id = reg.id
            rs_id = rs.id

        captured: dict = {}
        app = self._make_capture_app(captured)
        with TestClient(app) as mw_client:
            assert _login(mw_client).status_code == 200
            res = mw_client.delete(
                f"/api/activity/registrations/{reg_id}/supplies/{rs_id}"
            )
            assert res.status_code == 200, res.text

        assert captured["audit_entity_id"] == str(reg_id), (
            f"預期 entity_id 覆寫為 registration_id={reg_id}，"
            f"但拿到 {captured['audit_entity_id']}（可能被 URL 尾段 supply_record_id={rs_id} 搶走）"
        )
        assert captured["audit_summary"] is not None
        assert (
            "移除用品" in captured["audit_summary"]
            or "用品" in captured["audit_summary"]
        )

    def test_remove_supply_force_refund_sets_registration_as_entity(
        self, supply_audit_client
    ):
        """force_refund=true 觸發自動沖帳時，audit_entity_id 必須為 registration_id。

        此為 bug 的主要症狀：金流退費事件的 entity_id 掛錯導致稽核彙整對不上。
        """
        _, sf = supply_audit_client
        with sf() as s:
            _create_admin(s)
            # paid_amount=800 (課程300 + 用品500)：移除用品後超繳 500，需 force_refund
            reg, rs = _setup_reg_with_supply(s, supply_price=500, paid_amount=800)
            reg_id = reg.id
            rs_id = rs.id

        captured: dict = {}
        app = self._make_capture_app(captured)
        with TestClient(app) as mw_client:
            assert _login(mw_client).status_code == 200
            res = mw_client.delete(
                f"/api/activity/registrations/{reg_id}/supplies/{rs_id}",
                params={
                    "force_refund": "true",
                    "refund_reason": "用品退回測試退費稽核覆寫，驗證 entity_id 對齊",
                },
            )
            assert res.status_code == 200, res.text

        assert captured["audit_entity_id"] == str(reg_id), (
            f"預期 entity_id 覆寫為 registration_id={reg_id}，"
            f"但拿到 {captured['audit_entity_id']}（可能被 URL 尾段 supply_record_id={rs_id} 搶走）"
        )
        assert captured["audit_summary"] is not None
        assert (
            "移除用品" in captured["audit_summary"]
            or "用品" in captured["audit_summary"]
        )

    def test_supply_record_id_differs_from_registration_id(self, supply_audit_client):
        """前提確認：rs_id != reg_id，確保 entity_id 測試有意義（非巧合相等）。"""
        _, sf = supply_audit_client
        with sf() as s:
            _create_admin(s)
            reg, rs = _setup_reg_with_supply(s, supply_price=200, paid_amount=0)
            reg_id = reg.id
            rs_id = rs.id

        # SQLite auto-increment：先建 reg 再建 rs，rs_id > reg_id
        assert (
            rs_id != reg_id
        ), f"rs_id={rs_id} 與 reg_id={reg_id} 相等，後續 entity_id 比對無鑑別力"
