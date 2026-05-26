"""Tests for services/pdf_recovery.py — orphan 'generating' row sweep."""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    Classroom,
    Student,
    StudentGrowthReport,
    session_scope,
)
from models.portfolio import (
    REPORT_STATUS_FAILED,
    REPORT_STATUS_GENERATING,
    REPORT_STATUS_PENDING,
    REPORT_STATUS_READY,
)


@pytest.fixture
def in_memory_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    TestSession = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(base_module, "_engine", engine)
    monkeypatch.setattr(base_module, "_SessionFactory", TestSession)
    Base.metadata.create_all(engine)
    return engine


_PERIOD_SEQ = [0]


def _seed_report(student_id: int, status: str) -> int:
    _PERIOD_SEQ[0] += 1
    with session_scope() as s:
        r = StudentGrowthReport(
            student_id=student_id,
            period_label=f"P{_PERIOD_SEQ[0]:03d}",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 6, 30),
            status=status,
        )
        s.add(r)
        s.flush()
        return r.id


_SID_SEQ = [0]


def _seed_student() -> int:
    with session_scope() as s:
        c = Classroom(name="貓貓班")
        s.add(c)
        s.flush()
        _SID_SEQ[0] += 1
        st = Student(student_id=f"S{_SID_SEQ[0]:06d}", name="小明", classroom_id=c.id)
        s.add(st)
        s.flush()
        return st.id


def test_recovery_marks_generating_as_failed(in_memory_db):
    from services.pdf_recovery import _INTERRUPTED_MESSAGE, recover_orphan_pdf_jobs

    sid = _seed_student()
    gen_id = _seed_report(sid, REPORT_STATUS_GENERATING)

    count = recover_orphan_pdf_jobs()
    assert count == 1

    with session_scope() as s:
        r = s.query(StudentGrowthReport).filter_by(id=gen_id).first()
        assert r.status == REPORT_STATUS_FAILED
        assert r.error_message == _INTERRUPTED_MESSAGE


def test_recovery_skips_non_generating_rows(in_memory_db):
    from services.pdf_recovery import recover_orphan_pdf_jobs

    sid = _seed_student()
    pending_id = _seed_report(sid, REPORT_STATUS_PENDING)
    ready_id = _seed_report(sid, REPORT_STATUS_READY)
    failed_id = _seed_report(sid, REPORT_STATUS_FAILED)

    count = recover_orphan_pdf_jobs()
    assert count == 0

    with session_scope() as s:
        assert (
            s.query(StudentGrowthReport).filter_by(id=pending_id).first().status
            == REPORT_STATUS_PENDING
        )
        assert (
            s.query(StudentGrowthReport).filter_by(id=ready_id).first().status
            == REPORT_STATUS_READY
        )
        assert (
            s.query(StudentGrowthReport).filter_by(id=failed_id).first().status
            == REPORT_STATUS_FAILED
        )


def test_recovery_handles_multiple_generating(in_memory_db):
    from services.pdf_recovery import recover_orphan_pdf_jobs

    sid = _seed_student()
    ids = [_seed_report(sid, REPORT_STATUS_GENERATING) for _ in range(3)]
    # 一張 ready 不該被掃
    ready_id = _seed_report(sid, REPORT_STATUS_READY)

    count = recover_orphan_pdf_jobs()
    assert count == 3

    with session_scope() as s:
        for rid in ids:
            assert (
                s.query(StudentGrowthReport).filter_by(id=rid).first().status
                == REPORT_STATUS_FAILED
            )
        assert (
            s.query(StudentGrowthReport).filter_by(id=ready_id).first().status
            == REPORT_STATUS_READY
        )


def test_recovery_returns_zero_when_no_orphans(in_memory_db):
    from services.pdf_recovery import recover_orphan_pdf_jobs

    count = recover_orphan_pdf_jobs()
    assert count == 0
