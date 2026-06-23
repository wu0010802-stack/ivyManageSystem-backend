"""驗證 /pos/receipts/{receipt_no}/print.pdf 不可用 LIKE 萬用字元越權重印他人收據。

威脅（2026-06-23 audit P2）：print 端點先精確比對 receipt_no，落空後把 path
參數「未跳脫」直接塞進 notes.like(f"%[{receipt_no}]%")。傳 `%` / `POS-%` /
`POS-________-...`（含 SQL LIKE 萬用字元 % 與 _）時精確比對落空 → fallback 變成
`%[POS-%]%` 命中任一 [POS-...] 紀錄 → 重建出真實收據，持 ACTIVITY_READ 即可不知
精確收據號補印到他人收據（洩漏學生姓名 / 班級 / 金額 / 明細）。

修法：端點開頭先以嚴格 regex 驗證 receipt_no 格式（POS-YYYYMMDD-<hex>），不合即
404；保留的 notes fallback 同時 escape LIKE 萬用字元做防禦縱深。
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
    Classroom,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def pos_client(tmp_path):
    db_path = tmp_path / "pos_print_wildcard.sqlite"
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


def _create_user(session, *, username, perms, role="staff"):
    if isinstance(perms, str):
        perms = [perms]
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=perms,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Passw0rd!"}
    )


_RECEIPT_NO = "POS-20260507-ABCDEF123456"


def _seed_receipt(session):
    classroom = Classroom(name="海豚班", is_active=True)
    session.add(classroom)
    session.flush()
    course = ActivityCourse(name="圍棋", price=1000, is_active=True)
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name="王小明",
        birthday=date(2020, 5, 1),
        class_name="海豚班",
        parent_phone="0900-111-222",
        is_active=True,
        match_status="matched",
        paid_amount=1000,
    )
    session.add(reg)
    session.flush()
    rec = ActivityPaymentRecord(
        registration_id=reg.id,
        amount=1000,
        type="payment",
        payment_method="現金",
        payment_date=date.today(),
        operator="張三",
        receipt_no=_RECEIPT_NO,
        notes=f"[{_RECEIPT_NO}]",
    )
    session.add(rec)
    session.flush()


def _spy_pdf(monkeypatch, captured):
    import api.activity.pos as pos_mod

    def _spy(*, receipt):
        captured["receipt"] = receipt
        return b"%PDF-1.4 stub"

    monkeypatch.setattr(pos_mod, "generate_pos_receipt_pdf", _spy)


class TestPrintReceiptWildcardGuard:
    @pytest.mark.parametrize(
        "malicious",
        [
            "%",  # 整碼萬用字元
            "POS-%",  # 前綴 + 萬用字元
            "POS-________-____________",  # 以 _ 單字元萬用字元逐位匹配
            "POS-%-%",
        ],
    )
    def test_wildcard_receipt_no_does_not_leak_other_receipt(
        self, pos_client, monkeypatch, malicious
    ):
        client, sf = pos_client
        with sf() as s:
            _create_user(s, username="reader", perms=["ACTIVITY_READ"])
            _seed_receipt(s)
            s.commit()
        assert _login(client, "reader").status_code == 200

        captured: dict = {}
        _spy_pdf(monkeypatch, captured)
        res = client.get(f"/api/activity/pos/receipts/{malicious}/print.pdf")
        # 不可命中任何收據 → 404，且不得呼叫到 PDF 產生器洩漏內容
        assert res.status_code == 404, res.text
        assert "receipt" not in captured
        assert "王小明" not in res.text

    def test_exact_receipt_no_still_prints(self, pos_client, monkeypatch):
        """正控：合法精確收據號仍可正常重印。"""
        client, sf = pos_client
        with sf() as s:
            _create_user(s, username="reader2", perms=["ACTIVITY_READ"])
            _seed_receipt(s)
            s.commit()
        assert _login(client, "reader2").status_code == 200

        captured: dict = {}
        _spy_pdf(monkeypatch, captured)
        res = client.get(f"/api/activity/pos/receipts/{_RECEIPT_NO}/print.pdf")
        assert res.status_code == 200, res.text
        assert captured["receipt"]["receipt_no"] == _RECEIPT_NO
