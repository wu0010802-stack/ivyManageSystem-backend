"""
test_finance_reconciliation.py — 驗證 paid_amount 對帳偵測（spec H4）。

聚焦純函式 detect_paid_amount_mismatches 與 format_mismatches_for_line；
scheduler 本身不測（asyncio time loop 不易測且無核心邏輯）。
"""

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    Base,
    RegistrationCourse,
)
from services.finance_reconciliation_service import (
    PaidAmountMismatch,
    detect_paid_amount_mismatches,
    format_mismatches_for_line,
)


@pytest.fixture
def session_factory(tmp_path):
    db_path = tmp_path / "reconciliation.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf

    Base.metadata.create_all(engine)

    yield sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _make_reg(s, *, name, paid, is_active=True):
    course = s.query(ActivityCourse).filter(ActivityCourse.name == "美術").first()
    if not course:
        course = ActivityCourse(
            name="美術",
            price=1000,
            capacity=30,
            allow_waitlist=True,
            school_year=114,
            semester=1,
        )
        s.add(course)
        s.flush()
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=paid,
        is_paid=False,
        is_active=is_active,
        school_year=114,
        semester=1,
    )
    s.add(reg)
    s.flush()
    s.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=1000,
        )
    )
    s.flush()
    return reg


def _add_record(s, *, reg_id, type_, amount, voided=False):
    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type=type_,
        amount=amount,
        payment_date=date.today(),
        payment_method="現金",
        operator="pos_admin",
        notes="",
        created_at=datetime.now(),
    )
    if voided:
        rec.voided_at = datetime.now()
        rec.voided_by = "tester"
        rec.void_reason = "test"
    s.add(rec)
    s.flush()
    return rec


# ── detect_paid_amount_mismatches ─────────────────────────────────────


def test_no_mismatches_when_clean(session_factory):
    """paid_amount = SUM(records 淨額) → 無 drift。"""
    sf = session_factory
    with sf() as s:
        reg = _make_reg(s, name="王", paid=500)
        _add_record(s, reg_id=reg.id, type_="payment", amount=500)
        s.commit()
        mismatches = detect_paid_amount_mismatches(s)
    assert mismatches == []


def test_no_mismatches_when_no_records_and_zero_paid(session_factory):
    """全新報名沒任何 record 且 paid=0 → 無 drift。"""
    sf = session_factory
    with sf() as s:
        _make_reg(s, name="新生", paid=0)
        s.commit()
        mismatches = detect_paid_amount_mismatches(s)
    assert mismatches == []


def test_db_paid_higher_than_records_drift_positive(session_factory):
    """歷史匯入直接寫入 paid_amount 但無對應 records → drift = +db_paid。"""
    sf = session_factory
    with sf() as s:
        reg = _make_reg(s, name="李歷史", paid=1500)
        reg_id = reg.id
        # 沒有任何 payment_record
        s.commit()
        mismatches = detect_paid_amount_mismatches(s)
    assert len(mismatches) == 1
    assert mismatches[0].registration_id == reg_id
    assert mismatches[0].db_paid_amount == 1500
    assert mismatches[0].records_net == 0
    assert mismatches[0].drift == 1500


def test_db_paid_lower_than_records_drift_negative(session_factory):
    """records 累計 1000 但 paid_amount 漏更新 = 0 → drift = -1000。"""
    sf = session_factory
    with sf() as s:
        reg = _make_reg(s, name="陳", paid=0)
        _add_record(s, reg_id=reg.id, type_="payment", amount=1000)
        s.commit()
        mismatches = detect_paid_amount_mismatches(s)
    assert len(mismatches) == 1
    assert mismatches[0].drift == -1000
    assert mismatches[0].records_net == 1000


def test_voided_records_excluded(session_factory):
    """voided records 不計入。500 paid + 500 voided refund → records_net=500 與 paid 一致。"""
    sf = session_factory
    with sf() as s:
        reg = _make_reg(s, name="張", paid=500)
        _add_record(s, reg_id=reg.id, type_="payment", amount=500)
        # 一筆 voided refund 應該被排除（不會把 records_net 拉低）
        _add_record(s, reg_id=reg.id, type_="refund", amount=100, voided=True)
        s.commit()
        mismatches = detect_paid_amount_mismatches(s)
    assert mismatches == []


def test_inactive_registrations_excluded(session_factory):
    """is_active=False 的 reg 不被檢查（已封存資料不需告警）。"""
    sf = session_factory
    with sf() as s:
        # active 帳一致
        reg_a = _make_reg(s, name="A", paid=100)
        _add_record(s, reg_id=reg_a.id, type_="payment", amount=100)
        # inactive 帳不一致 — 不應出現在結果
        reg_b = _make_reg(s, name="B 已停用", paid=999, is_active=False)
        s.commit()
        mismatches = detect_paid_amount_mismatches(s)
    assert mismatches == []


def test_refund_subtracted_correctly(session_factory):
    """payment 1000 + refund 300 → records_net=700；paid=700 → 一致。"""
    sf = session_factory
    with sf() as s:
        reg = _make_reg(s, name="退費生", paid=700)
        _add_record(s, reg_id=reg.id, type_="payment", amount=1000)
        _add_record(s, reg_id=reg.id, type_="refund", amount=300)
        s.commit()
        mismatches = detect_paid_amount_mismatches(s)
    assert mismatches == []


def test_multiple_mismatches_detected(session_factory):
    """多筆同時不一致皆被偵測。"""
    sf = session_factory
    with sf() as s:
        reg_a = _make_reg(s, name="甲", paid=500)  # 有對應 records
        _add_record(s, reg_id=reg_a.id, type_="payment", amount=500)
        reg_b = _make_reg(s, name="乙", paid=999)  # 漂移
        reg_c = _make_reg(s, name="丙", paid=0)
        _add_record(s, reg_id=reg_c.id, type_="payment", amount=200)  # 漂移
        reg_b_id = reg_b.id
        reg_c_id = reg_c.id
        s.commit()
        mismatches = detect_paid_amount_mismatches(s)
    assert len(mismatches) == 2
    by_id = {m.registration_id: m for m in mismatches}
    assert by_id[reg_b_id].drift == 999
    assert by_id[reg_c_id].drift == -200


# ── format_mismatches_for_line ────────────────────────────────────────


def test_format_empty_returns_empty_string():
    assert format_mismatches_for_line([], "2026-05-07") == ""


def test_format_includes_date_count_and_total():
    mismatches = [
        PaidAmountMismatch(
            registration_id=1,
            student_name="王",
            class_name="大班",
            db_paid_amount=500,
            records_net=300,
            drift=200,
        ),
    ]
    msg = format_mismatches_for_line(mismatches, "2026-05-07")
    assert "2026-05-07" in msg
    assert "1 筆" in msg
    assert "差額合計：+200" in msg
    assert "王" in msg
    assert "DB paid=500" in msg


def test_format_truncates_at_10_with_overflow_hint():
    mismatches = [
        PaidAmountMismatch(
            registration_id=i,
            student_name=f"N{i}",
            class_name="X",
            db_paid_amount=100,
            records_net=0,
            drift=100,
        )
        for i in range(1, 16)
    ]
    msg = format_mismatches_for_line(mismatches, "2026-05-07")
    assert "其餘 5 筆" in msg
    assert "差額合計：+1500" in msg


# ── run_finance_reconciliation: 推 LINE 行為 ────────────────────────


def test_run_pushes_line_when_mismatches(session_factory):
    """有不一致時呼叫 line_push 一次。"""
    sf = session_factory
    with sf() as s:
        _make_reg(s, name="漂移生", paid=1000)
        s.commit()

    from services import finance_reconciliation_scheduler as fr_sched

    mock_push = MagicMock(return_value=True)
    result = fr_sched.run_finance_reconciliation(
        target_date=date.today(), line_push=mock_push
    )

    assert result["mismatch_count"] == 1
    assert result["total_drift"] == 1000
    assert result["notification_pushed"] is True
    mock_push.assert_called_once()
    call_arg = mock_push.call_args[0][0]
    assert "漂移生" in call_arg


def test_run_skips_line_when_no_mismatches(session_factory):
    """無不一致時不推 LINE（避免每天送雜訊）。"""
    sf = session_factory
    with sf() as s:
        reg = _make_reg(s, name="正常", paid=500)
        _add_record(s, reg_id=reg.id, type_="payment", amount=500)
        s.commit()

    from services import finance_reconciliation_scheduler as fr_sched

    mock_push = MagicMock(return_value=True)
    result = fr_sched.run_finance_reconciliation(
        target_date=date.today(), line_push=mock_push
    )

    assert result["mismatch_count"] == 0
    assert result["notification_pushed"] is False
    mock_push.assert_not_called()
