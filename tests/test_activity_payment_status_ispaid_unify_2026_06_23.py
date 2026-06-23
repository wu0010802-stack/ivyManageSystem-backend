"""tests/test_activity_payment_status_ispaid_unify_2026_06_23.py

第2波：統一 payment_status 篩選的 is_paid 真相來源。

問題（檢視 finding）：_build_registration_filter_query 的 "paid" 分支用持久化
ActivityRegistration.is_paid，其餘四態（unpaid/partial/overpaid/no_fee）走即時
total 子查詢重算 → 同一個篩選參數兩種真相來源。is_paid 為純衍生快取（所有寫入
點皆 _compute_is_paid，無 decoupled override），但若 total 變動未回寫 is_paid
（如候補轉正抬高 total）即漂移：一筆其實已欠費的列因 is_paid 仍 True 被「已繳清」
篩選撈到。

修正：paid 分支改即時衍生（total>0 且 paid==total），與 _derive_payment_status
及其餘四態完全對齊，不再依賴持久化 is_paid。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.activity._shared import _build_registration_filter_query
from models.database import ActivityRegistration, Base, RegistrationCourse


@pytest.fixture
def sf(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pf.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _mk(session, *, paid: int, is_paid: bool, course_price: int, name: str) -> int:
    """直接指定 is_paid（可與 paid/total 不一致），用以模擬快取漂移。"""
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        paid_amount=paid,
        is_paid=is_paid,
        is_active=True,
        school_year=114,
        semester=1,
    )
    session.add(reg)
    session.flush()
    if course_price > 0:
        session.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=1,
                status="enrolled",
                price_snapshot=course_price,
            )
        )
    session.flush()
    return reg.id


def _ids(session, status):
    return [
        r.id
        for r in _build_registration_filter_query(session, payment_status=status).all()
    ]


def test_paid_filter_ignores_stale_is_paid_true(sf):
    # 漂移：is_paid=True 但 paid(1000) < total(2000) → 即時衍生為 partial，
    # 不應再被「已繳清」撈到（原依賴 is_paid 會誤撈）。
    with sf() as s:
        drifted = _mk(s, paid=1000, is_paid=True, course_price=2000, name="漂移欠費")
        settled = _mk(s, paid=2000, is_paid=True, course_price=2000, name="真結清")
        s.commit()
        paid_ids = _ids(s, "paid")
    assert settled in paid_ids, "真結清（paid==total）仍應落 paid"
    assert (
        drifted not in paid_ids
    ), "is_paid 漂移的欠費列不應落 paid（即時衍生為 partial）"


def test_partial_filter_catches_stale_is_paid_true(sf):
    with sf() as s:
        drifted = _mk(s, paid=1000, is_paid=True, course_price=2000, name="漂移欠費")
        s.commit()
        partial_ids = _ids(s, "partial")
    assert drifted in partial_ids, "0<paid<total 應落 partial（不論 is_paid 快取值）"


def test_paid_filter_includes_settled_when_is_paid_false_stale(sf):
    # 反向漂移：實際結清(paid==total)但 is_paid=False（快取未更新）→ 即時衍生仍應
    # 落 paid（原依賴 is_paid 會漏掉真正結清的列）。
    with sf() as s:
        settled_stale = _mk(
            s, paid=2000, is_paid=False, course_price=2000, name="結清但快取False"
        )
        s.commit()
        paid_ids = _ids(s, "paid")
    assert settled_stale in paid_ids, "實際結清列即使 is_paid 快取為 False 也應落 paid"
