"""學期自動切換排程器核心 reconcile_academic_term 的三分支 + 冪等 + rollback。"""

from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Employee  # noqa: F401 — FK 依賴
from models.academic_term import AcademicTerm
from services.academic_term_turnover_scheduler import reconcile_academic_term


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _current(session):
    return session.query(AcademicTerm).filter(AcademicTerm.is_current.is_(True)).first()


def test_seed_when_absent_no_events(session):
    """全新 DB（無 is_current）→ 靜默建立當前學期 row，不觸發事件。"""
    with patch("services.academic_term_turnover_scheduler.fire_term_changed") as fired:
        out = reconcile_academic_term(session, today=date(2026, 3, 15))
        session.flush()
    assert out["action"] == "seed"
    fired.assert_not_called()
    cur = _current(session)
    assert (cur.school_year, cur.semester) == (114, 2)
    assert cur.start_date == date(2026, 2, 1)
    assert cur.end_date == date(2026, 7, 31)


def test_noop_when_aligned(session):
    """is_current 已等於日期推導學期 → 不動、不觸發。"""
    t = AcademicTerm(
        school_year=114,
        semester=2,
        is_current=True,
        start_date=date(2026, 2, 1),
        end_date=date(2026, 7, 31),
    )
    session.add(t)
    session.flush()
    with patch("services.academic_term_turnover_scheduler.fire_term_changed") as fired:
        out = reconcile_academic_term(session, today=date(2026, 3, 15))
        session.flush()
    assert out["action"] == "noop"
    fired.assert_not_called()


def test_turnover_fires_events_and_flips(session):
    """is_current=114-1，今天落在 114-2 → 翻牌 + 觸發事件一次 + 寫 audit。"""
    old = AcademicTerm(
        school_year=114,
        semester=1,
        is_current=True,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
    )
    session.add(old)
    session.flush()
    with patch("services.academic_term_turnover_scheduler.fire_term_changed") as fired:
        out = reconcile_academic_term(session, today=date(2026, 3, 1))
        session.flush()
    assert out["action"] == "turnover"
    assert fired.call_count == 1
    _, kwargs = fired.call_args
    assert (kwargs["old"].school_year, kwargs["old"].semester) == (114, 1)
    assert (kwargs["new"].school_year, kwargs["new"].semester) == (114, 2)
    cur = _current(session)
    assert (cur.school_year, cur.semester) == (114, 2)
    assert old.is_current is False
    from models.audit import AuditLog

    logs = session.query(AuditLog).filter(AuditLog.entity_type == "academic_term").all()
    assert len(logs) == 1
    assert logs[0].username == "academic_term_turnover"


def test_turnover_idempotent_second_run_noop(session):
    """翻牌後同日再跑 → 已對齊 → noop、不再觸發。"""
    old = AcademicTerm(
        school_year=114,
        semester=1,
        is_current=True,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
    )
    session.add(old)
    session.flush()
    reconcile_academic_term(session, today=date(2026, 3, 1))
    session.flush()
    with patch("services.academic_term_turnover_scheduler.fire_term_changed") as fired:
        out = reconcile_academic_term(session, today=date(2026, 3, 1))
        session.flush()
    assert out["action"] == "noop"
    fired.assert_not_called()


def test_reuses_existing_target_row(session):
    """目標學期 row 已存在（非 current）→ 不重建，翻它的 is_current。"""
    old = AcademicTerm(
        school_year=114,
        semester=1,
        is_current=True,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
    )
    existing_new = AcademicTerm(
        school_year=114,
        semester=2,
        is_current=False,
        start_date=date(2026, 2, 1),
        end_date=date(2026, 7, 31),
    )
    session.add_all([old, existing_new])
    session.flush()
    with patch("services.academic_term_turnover_scheduler.fire_term_changed"):
        reconcile_academic_term(session, today=date(2026, 3, 1))
        session.flush()
    assert (
        session.query(AcademicTerm)
        .filter(AcademicTerm.school_year == 114, AcademicTerm.semester == 2)
        .count()
        == 1
    )
    assert _current(session).id == existing_new.id
