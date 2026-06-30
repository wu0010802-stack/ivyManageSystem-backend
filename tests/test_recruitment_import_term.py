"""測試 Excel 匯入時由訪視月份推導 target_school_year / target_semester。"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.recruitment import ImportRecord, import_recruitment_records
from models.base import Base
from models.recruitment import RecruitmentVisit


@pytest.fixture
def recruitment_session_factory(tmp_path):
    db_path = tmp_path / "recruitment-import-term.sqlite"
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


def test_import_derives_target_term_from_month(recruitment_session_factory):
    """115.03（民國 115 年 3 月）應推導為 114 下學期（目標入學學年 114、學期 2）。"""
    result = import_recruitment_records(
        [ImportRecord(**{"月份": "115.03", "幼生姓名": "匯入童"})],
        _=None,
    )
    assert result == {"inserted": 1, "skipped": 0}

    with recruitment_session_factory() as session:
        record = session.query(RecruitmentVisit).filter_by(child_name="匯入童").one()
        # 115.03 → date(2026,3,1) → 114 下學期
        assert (
            record.target_school_year == 114
        ), f"expected 114, got {record.target_school_year}"
        assert record.target_semester == 2, f"expected 2, got {record.target_semester}"


def test_import_derives_target_term_from_month_upper_semester(
    recruitment_session_factory,
):
    """115.09（民國 115 年 9 月）應推導為 115 上學期（學年 115、學期 1）。"""
    result = import_recruitment_records(
        [ImportRecord(**{"月份": "115.09", "幼生姓名": "秋季入學童"})],
        _=None,
    )
    assert result == {"inserted": 1, "skipped": 0}

    with recruitment_session_factory() as session:
        record = (
            session.query(RecruitmentVisit).filter_by(child_name="秋季入學童").one()
        )
        # 115.09 → date(2026,9,1) → 115 上學期
        assert (
            record.target_school_year == 115
        ), f"expected 115, got {record.target_school_year}"
        assert record.target_semester == 1, f"expected 1, got {record.target_semester}"


def test_import_invalid_month_is_skipped(recruitment_session_factory):
    """月份格式錯誤的列在 _normalize_roc_month 階段即被略過（skipped），不會插入、也不會讓整批匯入崩潰。"""
    result = import_recruitment_records(
        [ImportRecord(**{"月份": "notamonth", "幼生姓名": "壞月份童"})],
        _=None,
    )
    # 壞月份 → _normalize_roc_month 拋 ValueError → 直接 skip
    # (handler 在 normalize 時就 skip，不會到 target 推導)
    assert result["skipped"] == 1

    # 驗證未插入任何記錄
    with recruitment_session_factory() as session:
        count = session.query(RecruitmentVisit).filter_by(child_name="壞月份童").count()
        assert count == 0, "malformed month record should not be inserted"
