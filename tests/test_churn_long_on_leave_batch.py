"""detect_signal_long_on_leave 批次取休學 log，消除逐生 N+1（稽核 2026-06-03 P3-5）。

原實作先取所有 on_leave 學生，再對每位學生各下一條 StudentChangeLog 查最近休學 → N+1。
批次化後：一次 IN 查詢 + Python 端取每生最新一筆。結果須與逐生版一致。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Student
from models.student_log import StudentChangeLog
from services.analytics.churn_service import detect_signal_long_on_leave


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s._engine_for_test = engine
    yield s
    s.close()
    engine.dispose()


def _on_leave_student(session, n):
    stu = Student(
        student_id=f"OL{n:03d}",
        name=f"休學生{n}",
        lifecycle_status="on_leave",
        is_active=True,
    )
    session.add(stu)
    session.flush()
    session.add(
        StudentChangeLog(
            student_id=stu.id,
            school_year=113,
            semester=1,
            event_type="休學",
            event_date=date(2020, 1, 1),  # 久遠 → days 遠超門檻 → 觸發
        )
    )
    session.flush()
    return stu.id


def test_detect_long_on_leave_is_batched_and_correct(db):
    ids = [_on_leave_student(db, i) for i in range(4)]
    db.commit()

    counter = {"n": 0}

    @event.listens_for(db._engine_for_test, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    result = detect_signal_long_on_leave(db, today=date(2026, 6, 3))

    # 4 位 on_leave 學生皆觸發（休學 2020 → 6 年）
    assert {r["student_id"] for r in result} == set(ids)
    assert all(r["type"] == "long_on_leave" for r in result)
    # 批次化後 query 數與學生數無關（候選 1 + 批次 log 1 ≈ 2）；逐生 N+1 對 4 生需 ~5
    assert counter["n"] <= 3, f"query 數 {counter['n']} 過多，疑逐生 N+1"
