"""P2-6 回歸（2026-06-23 深度 audit）：多 item POS 收據被部分作廢後重印，
total 靜默縮減（同 receipt_no 兩次列印金額不同）造成客訴困惑。

修：_parse_receipt_response_from_record 額外回 has_voided_items / original_total
（含已作廢的原始開立金額），收據 PDF 據此標註「部分作廢，原始金額 NT$X」。
total 仍為有效（未作廢）金額以對齊 daily/finance 流水口徑。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.activity import ActivityPaymentRecord, ActivityRegistration
from api.activity.pos import _parse_receipt_response_from_record


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _reg(session, name="王小明"):
    r = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=0,
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


def _rec(session, reg_id, receipt_no, amount, *, voided=False):
    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type="payment",
        amount=amount,
        payment_date=date(2026, 3, 1),
        payment_method="現金",
        receipt_no=receipt_no,
        voided_at=date(2026, 3, 2) if voided else None,
        voided_by="admin" if voided else None,
        void_reason="客戶取消其中一項" if voided else None,
    )
    session.add(rec)
    session.flush()
    return rec


def test_partial_void_receipt_flags_and_original_total(session):
    reg_a = _reg(session, "王小明")
    reg_b = _reg(session, "陳小美")
    receipt_no = "POS-20260301-ABCDEF123456"
    rec_a = _rec(session, reg_a.id, receipt_no, 1000)
    _rec(session, reg_b.id, receipt_no, 500, voided=True)  # 其中一項作廢
    session.flush()

    out = _parse_receipt_response_from_record(session, rec_a)
    assert out is not None
    # total 僅含有效金額（對齊流水口徑）
    assert out["total"] == 1000
    # 部分作廢旗標 + 原始開立金額（含作廢）
    assert out["has_voided_items"] is True
    assert out["original_total"] == 1500


def test_no_void_receipt_not_flagged(session):
    reg_a = _reg(session, "王小明")
    receipt_no = "POS-20260301-FEDCBA654321"
    rec_a = _rec(session, reg_a.id, receipt_no, 1000)
    session.flush()

    out = _parse_receipt_response_from_record(session, rec_a)
    assert out is not None
    assert out["total"] == 1000
    assert out["has_voided_items"] is False
    assert out["original_total"] == 1000
