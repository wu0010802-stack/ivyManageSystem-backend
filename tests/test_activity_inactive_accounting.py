"""停用（軟刪）報名後帳務明細與報表仍可查核（code review #5，2026-06-22）。

問題：
- 付款明細 GET /registrations/{id}/payments 硬篩 is_active=True → 軟刪報名回 404。
- 匯出繳費報表未傳 include_inactive，預設只含 active → 軟刪報名的退費沖帳消失於報表。
刪除並自動退款後，財務端無法從標準帳務功能查核該筆歷史。

修法：
- 明細端點以 registration_id 取資料，不再要求 is_active（已知 id 即查得歷史）。
- 匯出報表新增 include_inactive 參數（預設 False 維持現狀），財務需要時可納入軟刪報名。

DB 隔離：SQLite + monkeypatch base_module（不碰 dev PG）。
"""

import io
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
    ActivityPaymentRecord,
    ActivityRegistration,
    Base,
    Classroom,
    User,
)
from utils.auth import hash_password

PASSWORD = "Temp123456"


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "inactive_acct.sqlite"
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


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _login(c):
    r = c.post("/api/auth/login", json={"username": "clerk", "password": PASSWORD})
    assert r.status_code == 200, r.text


def _seed_inactive_reg_with_payment(sf, *, student_name="已刪小明"):
    """建立一筆已軟刪（is_active=False）且有付款/退費紀錄的報名。"""
    sy, sem = _term()
    with sf() as s:
        s.add(
            User(
                username="clerk",
                password_hash=hash_password(PASSWORD),
                role="hr",
                permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
                is_active=True,
            )
        )
        c1 = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
        s.add(c1)
        s.flush()
        reg = ActivityRegistration(
            student_name=student_name,
            birthday="2020-05-10",
            class_name="海豚班",
            classroom_id=c1.id,
            school_year=sy,
            semester=sem,
            is_active=False,  # 已軟刪
            paid_amount=0,
        )
        s.add(reg)
        s.flush()
        s.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="refund",
                amount=1000,
                payment_date=date.today(),
                payment_method="系統補齊",
                notes="（刪除報名自動沖帳）",
                operator="clerk",
            )
        )
        s.commit()
        return reg.id


def _xlsx_contains(content: bytes, needle: str) -> bool:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for cell in row:
                if cell is not None and needle in str(cell):
                    return True
    return False


class TestInactivePaymentDetail:
    def test_payments_detail_available_for_inactive_registration(self, admin_client):
        c, sf = admin_client
        reg_id = _seed_inactive_reg_with_payment(sf)
        _login(c)

        res = c.get(f"/api/activity/registrations/{reg_id}/payments")
        assert res.status_code == 200, res.text
        body = res.json()
        assert any(
            r["type"] == "refund" and r["amount"] == 1000 for r in body["records"]
        )


class TestPaymentReportIncludeInactive:
    def test_report_excludes_inactive_by_default(self, admin_client):
        c, sf = admin_client
        _seed_inactive_reg_with_payment(sf, student_name="預設不含小明")
        _login(c)

        res = c.get("/api/activity/registrations/payment-report")
        assert res.status_code == 200, res.text
        assert not _xlsx_contains(res.content, "預設不含小明")

    def test_report_includes_inactive_when_requested(self, admin_client):
        c, sf = admin_client
        _seed_inactive_reg_with_payment(sf, student_name="納入查核小明")
        _login(c)

        res = c.get(
            "/api/activity/registrations/payment-report",
            params={"include_inactive": "true"},
        )
        assert res.status_code == 200, res.text
        assert _xlsx_contains(res.content, "納入查核小明")
