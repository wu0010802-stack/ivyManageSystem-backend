"""class_performance_rate 月底班級歸屬須批次解析，避免逐生 N+1（稽核 2026-06-03 P1#5）。

原實作每個 month_end 對全校候選學生逐生呼叫 classroom_at_month_end（每生 1-2 條 DB
query）→ refresh ≈ 班級×2×6×(1+學生×1.5) 條同步 round-trip。批次化後每月固定 2-3 條，
且結果須與逐生 classroom_at_month_end 完全一致（語意不變）。
"""

import os
import sys
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Classroom, Student
from models.student_transfer import StudentClassroomTransfer
from services.year_end.enrollment_rates import class_performance_rate

MONTH_END = date(2026, 5, 31)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s._engine_for_test = engine
    yield s
    s.close()
    engine.dispose()


def _student(session, classroom_id):
    n = len(session.query(Student).all()) + 1
    st = Student(
        student_id=f"E{n:03d}",
        name=f"生{n}",
        classroom_id=classroom_id,
        lifecycle_status="active",
        enrollment_date=date(2026, 2, 1),
    )
    session.add(st)
    session.flush()
    return st


def _transfer(session, student_id, to_classroom_id, when):
    t = StudentClassroomTransfer(
        student_id=student_id,
        to_classroom_id=to_classroom_id,
        transferred_at=when,
    )
    session.add(t)
    session.flush()
    return t


def _seed(session):
    a = Classroom(name="A班", school_year=114, semester=2)
    b = Classroom(name="B班", school_year=114, semester=2)
    session.add_all([a, b])
    session.flush()
    s1 = _student(session, a.id)  # 無轉班 → 月底 a
    s2 = _student(session, a.id)  # 無轉班 → 月底 a
    _student(session, b.id)  # s3 無轉班 → 月底 b
    s4 = _student(session, b.id)  # 現態 b，轉入 a（月底前）→ 月底 a
    s5 = _student(session, a.id)  # 現態 a，轉去 b（月底前）→ 月底 b
    _transfer(session, s4.id, a.id, datetime(2026, 3, 1, 9, 0))
    _transfer(session, s5.id, b.id, datetime(2026, 4, 1, 9, 0))
    session.commit()
    return a.id, b.id


def test_class_performance_rate_is_batched_and_correct(db):
    classroom_a, _classroom_b = _seed(db)

    counter = {"n": 0}

    @event.listens_for(db._engine_for_test, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    # 月底 A 班在班：s1, s2, s4(轉入) = 3；target=5 → 3/5*100 = 60.00
    rate = class_performance_rate(db, classroom_a, [MONTH_END], head_count_target=5)

    assert rate == Decimal("60.00"), f"結果不正確（語意應與逐生 resolver 一致）：{rate}"
    # 批次化後 query 數與學生數無關（候選 1 + 批次 transfer 1 + fallback 1 ≈ 3）；
    # 逐生 N+1 版對 5 生需 ~9 條 → 此上界在修補前必被突破。
    assert counter["n"] <= 5, f"query 數 {counter['n']} 過多，疑似逐生 N+1 未批次化"
