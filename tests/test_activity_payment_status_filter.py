"""tests/test_activity_payment_status_filter.py

_build_registration_filter_query 的 payment_status 篩選契約（E1）。

問題：payment_status=="paid" 分支用 is_paid（= total>0 且 paid>=total），
把「超繳」(paid>total) 也撈進來；前端把 paid / overpaid 列為兩個獨立選項，
_derive_payment_status 的 paid 也只在 paid==total 時成立。結果「已繳費」篩選
混入 badge 顯示「超額繳費」的列，且 paid 與 overpaid 結果重疊。

修正後：paid 分支 = is_paid 且 paid<=total（即 paid==total、total>0），與
_derive_payment_status 對齊；超繳列只落 overpaid，不再同時落 paid。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.activity._shared import _build_registration_filter_query, _compute_is_paid
from models.database import ActivityRegistration, Base, RegistrationCourse


@pytest.fixture
def sf(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pf.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _mk(session, *, paid: int, course_price: int, name: str) -> int:
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=paid,
        is_paid=_compute_is_paid(paid, course_price),
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


def test_paid_filter_excludes_overpaid(sf):
    with sf() as s:
        settled = _mk(s, paid=1000, course_price=1000, name="結清")
        over = _mk(s, paid=1200, course_price=1000, name="超繳")
        s.commit()
        ids = _ids(s, "paid")
    assert settled in ids, "結清列應落 paid"
    assert over not in ids, "超繳列不應落 paid（應只落 overpaid）"


def test_overpaid_filter_excludes_settled(sf):
    with sf() as s:
        settled = _mk(s, paid=1000, course_price=1000, name="結清")
        over = _mk(s, paid=1200, course_price=1000, name="超繳")
        s.commit()
        ids = _ids(s, "overpaid")
    assert over in ids, "超繳列應落 overpaid"
    assert settled not in ids, "結清列不應落 overpaid"
