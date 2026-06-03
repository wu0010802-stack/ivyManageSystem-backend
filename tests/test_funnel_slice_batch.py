"""funnel slice_by_source/grade 一次載入 visit，消除逐 source/grade 重複整表載入 N+1
（稽核 2026-06-03 P3-6）。結果等價由既有 test_analytics_funnel.py 覆蓋，此處鎖 query 數。
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
from models.recruitment import RecruitmentVisit
from services.analytics.funnel_service import slice_by_source


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


def test_slice_by_source_loads_visits_once(db):
    for i, src in enumerate(["官網", "轉介", "路過", "FB", "Google"]):
        db.add(
            RecruitmentVisit(
                month="115.03",
                child_name=f"幼生{i}",
                source=src,
                has_deposit=False,
                enrolled=False,
            )
        )
    db.commit()

    counter = {"n": 0}

    @event.listens_for(db._engine_for_test, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    rows = slice_by_source(db, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31))

    assert {r["source"] for r in rows} == {"官網", "轉介", "路過", "FB", "Google"}
    # 一次載入所有 visit → query 數與 source 數無關；逐 source 版對 5 source 需 ~6
    assert counter["n"] <= 2, f"query 數 {counter['n']} 過多，疑逐 source 重複載入"
