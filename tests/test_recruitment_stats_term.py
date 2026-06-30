"""招生統計 /stats — 入學學期 scope 測試（Task 9）。

用直接呼叫 endpoint handler（`_=None` 繞過 auth）驗證
school_year / semester 參數正確 scope 整份統計輸出。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.recruitment import get_recruitment_stats
from models.base import Base
from models.recruitment import RecruitmentVisit


@pytest.fixture
def recruitment_session_factory(tmp_path):
    db_path = tmp_path / "stats-term.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    try:
        yield session_factory
    finally:
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


def test_stats_scoped_by_enrollment_term(recruitment_session_factory):
    """school_year=115, semester=1 只計算 target_school_year=115, target_semester=1 的訪視。"""
    with recruitment_session_factory() as session:
        session.add_all(
            [
                RecruitmentVisit(
                    month="114.09",
                    child_name="統甲",
                    has_deposit=True,
                    target_school_year=115,
                    target_semester=1,
                ),
                RecruitmentVisit(
                    month="114.09",
                    child_name="統乙",
                    has_deposit=False,
                    target_school_year=114,
                    target_semester=2,
                ),
            ]
        )
        session.commit()

    stats = get_recruitment_stats(school_year=115, semester=1, _=None)

    assert stats["total_visit"] == 1  # 只算入學 115 上學期者


def test_stats_no_term_filter_returns_all(recruitment_session_factory):
    """不傳 school_year/semester 時回傳全部訪視（不 scope）。"""
    with recruitment_session_factory() as session:
        session.add_all(
            [
                RecruitmentVisit(
                    month="114.09",
                    child_name="全甲",
                    target_school_year=115,
                    target_semester=1,
                ),
                RecruitmentVisit(
                    month="114.09",
                    child_name="全乙",
                    target_school_year=114,
                    target_semester=2,
                ),
            ]
        )
        session.commit()

    stats = get_recruitment_stats(school_year=None, semester=None, _=None)

    assert stats["total_visit"] == 2


def test_stats_school_year_only_scope(recruitment_session_factory):
    """只傳 school_year 時同時含上下學期。"""
    with recruitment_session_factory() as session:
        session.add_all(
            [
                RecruitmentVisit(
                    month="114.09",
                    child_name="年甲",
                    target_school_year=115,
                    target_semester=1,
                ),
                RecruitmentVisit(
                    month="114.09",
                    child_name="年乙",
                    target_school_year=115,
                    target_semester=2,
                ),
                RecruitmentVisit(
                    month="114.09",
                    child_name="年丙",
                    target_school_year=114,
                    target_semester=1,
                ),
            ]
        )
        session.commit()

    stats = get_recruitment_stats(school_year=115, semester=None, _=None)

    assert stats["total_visit"] == 2  # 115上 + 115下，排除 114上
