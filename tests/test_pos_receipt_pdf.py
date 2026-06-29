"""GET /api/activity/pos/receipts/{receipt_no}/print.pdf 端點測試。"""

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
from models.database import ActivityPaymentRecord, Base, User
from utils.auth import hash_password
from utils.permissions import Permission

from tests.test_activity_pos import _create_admin, _login, _setup_reg


@pytest.fixture
def pos_pdf_client(tmp_path):
    db_path = tmp_path / "pos-receipt-pdf.sqlite"
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


def _checkout_and_get_receipt_no(client, reg_id, amount=500, *, idempotency_key):
    """走真實 POS checkout，回傳 receipt_no。"""
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": amount}],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "type": "payment",
            "idempotency_key": idempotency_key,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["receipt_no"]


class TestPosReceiptPdfEndpoint:
    def test_returns_pdf_200(self, pos_pdf_client):
        client, sf = pos_pdf_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        receipt_no = _checkout_and_get_receipt_no(
            client, reg_id, idempotency_key="RECEIPTPDF-0001"
        )

        res = client.get(f"/api/activity/pos/receipts/{receipt_no}/print.pdf")
        assert res.status_code == 200, res.text
        assert res.headers["content-type"] == "application/pdf"
        assert res.content.startswith(b"%PDF-")
        assert len(res.content) > 1500
        # P1-22 防回歸：確保 TTF embed（不是 CID stub）。
        assert b"/FontFile2" in res.content
        assert b"NotoSansTC" in res.content

    def test_not_found_for_unknown_receipt(self, pos_pdf_client):
        client, sf = pos_pdf_client
        with sf() as s:
            _create_admin(s)
            s.commit()

        assert _login(client).status_code == 200
        res = client.get("/api/activity/pos/receipts/POS-99999999-DEADBEEF/print.pdf")
        assert res.status_code == 404

    def test_forbidden_without_activity_read(self, pos_pdf_client):
        """無 ACTIVITY_READ 權限 → 403."""
        client, sf = pos_pdf_client
        with sf() as s:
            # 先用 admin checkout
            _create_admin(s)
            reg = _setup_reg(s, student_name="李小華")
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        receipt_no = _checkout_and_get_receipt_no(
            client, reg_id, idempotency_key="RECEIPTPDF-0002"
        )

        # 重新登入無權限帳號
        with sf() as s:
            u = User(
                username="no_perm",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["SALARY_READ"],
                is_active=True,
            )
            s.add(u)
            s.commit()
        res = client.post(
            "/api/auth/login",
            json={"username": "no_perm", "password": "TempPass123"},
        )
        assert res.status_code == 200

        res = client.get(f"/api/activity/pos/receipts/{receipt_no}/print.pdf")
        assert res.status_code == 403

    def test_voided_receipt_reprint_returns_404(self, pos_pdf_client):
        """整張收據作廢後重印 → 404（不可印出含已作廢金額的有效收據）。"""
        client, sf = pos_pdf_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        receipt_no = _checkout_and_get_receipt_no(
            client, reg_id, idempotency_key="RECEIPTPDF-0003"
        )
        # reprint 成功（基準）
        assert (
            client.get(f"/api/activity/pos/receipts/{receipt_no}/print.pdf").status_code
            == 200
        )

        # 作廢這張收據的全部付款紀錄
        from datetime import datetime

        with sf() as s:
            recs = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.receipt_no == receipt_no)
                .all()
            )
            assert recs
            for r in recs:
                r.voided_at = datetime.now()
            s.commit()

        res = client.get(f"/api/activity/pos/receipts/{receipt_no}/print.pdf")
        assert res.status_code == 404, res.text

    def test_reprint_total_excludes_voided_records(self, pos_pdf_client):
        """部分作廢：重建收據的合計與項目只計未作廢紀錄（不重新加總已作廢金額）。"""
        client, sf = pos_pdf_client
        with sf() as s:
            _create_admin(s)
            r1 = _setup_reg(s, student_name="甲")
            r2 = _setup_reg(s, student_name="乙")
            s.commit()
            r1_id, r2_id = r1.id, r2.id

        assert _login(client).status_code == 200
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [
                    {"registration_id": r1_id, "amount": 500},
                    {"registration_id": r2_id, "amount": 300},
                ],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "idempotency_key": "voidsum-key-001",
            },
        )
        assert res.status_code == 201, res.text
        receipt_no = res.json()["receipt_no"]

        # 作廢 r2 那筆（300）
        from datetime import datetime

        from api.activity.pos import _parse_receipt_response_from_record

        with sf() as s:
            rec2 = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.receipt_no == receipt_no,
                    ActivityPaymentRecord.registration_id == r2_id,
                )
                .one()
            )
            rec2.voided_at = datetime.now()
            s.commit()

        with sf() as s:
            anchor = (
                s.query(ActivityPaymentRecord)
                .filter(
                    ActivityPaymentRecord.receipt_no == receipt_no,
                    ActivityPaymentRecord.voided_at.is_(None),
                )
                .first()
            )
            receipt = _parse_receipt_response_from_record(s, anchor)

        assert receipt is not None
        assert receipt["total"] == 500, "合計不應含已作廢的 300"
        assert len(receipt["items"]) == 1, "項目不應含已作廢紀錄"
