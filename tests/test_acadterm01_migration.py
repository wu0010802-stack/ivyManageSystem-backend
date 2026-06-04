"""acadterm01 migration：正規化起訖日 + 靜默對齊 is_current（不觸發事件）。

用 alembic MigrationContext 在記憶體 SQLite 上直接跑 upgrade()，不碰共用 dev DB。
"""

import importlib.util
from datetime import date
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from models.academic_term import AcademicTerm
from utils.academic import resolve_current_academic_term, term_bounds


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "acadterm01_normalize_academic_terms.py"
    )
    spec = importlib.util.spec_from_file_location("acadterm01_mig", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_upgrade(engine):
    mig = _load_migration()
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        mig.op = Operations(ctx)  # 注入 op proxy，使 op.get_bind() 回傳此連線
        trans = conn.begin()
        mig.upgrade()
        trans.commit()


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    AcademicTerm.__table__.create(eng)
    return eng


def _other_semester(sem):
    return 1 if sem == 2 else 2


def test_normalizes_dates_and_aligns_is_current():
    """既有 row 起訖日被正規化；is_current 靜默對齊到今天日期推導的學期。"""
    tsy, tsem = resolve_current_academic_term()  # 今天日期推導
    eng = _engine()
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=eng)
    s = Session()
    # 今天的學期（故意給錯誤起訖日、非 current）
    target = AcademicTerm(
        school_year=tsy,
        semester=tsem,
        start_date=date(2000, 1, 1),
        end_date=date(2000, 12, 31),
        is_current=False,
    )
    # 一個 stale 的 is_current（同年另一學期）
    stale = AcademicTerm(
        school_year=tsy,
        semester=_other_semester(tsem),
        start_date=date(2000, 1, 1),
        end_date=date(2000, 12, 31),
        is_current=True,
    )
    s.add_all([target, stale])
    s.commit()
    s.close()

    _run_upgrade(eng)

    s = Session()
    cur = s.query(AcademicTerm).filter(AcademicTerm.is_current.is_(True)).all()
    assert len(cur) == 1
    assert (cur[0].school_year, cur[0].semester) == (tsy, tsem)
    # 起訖日已正規化
    exp_start, exp_end = term_bounds(tsy, tsem)
    assert cur[0].start_date == exp_start
    assert cur[0].end_date == exp_end
    # stale row 的日期也被正規化、is_current 已清
    other = (
        s.query(AcademicTerm)
        .filter(AcademicTerm.semester == _other_semester(tsem))
        .first()
    )
    o_start, o_end = term_bounds(tsy, _other_semester(tsem))
    assert other.is_current is False
    assert other.start_date == o_start and other.end_date == o_end
    s.close()


def test_empty_table_creates_current_term():
    """空表 → 建立今天的學期 row 並設 is_current。"""
    tsy, tsem = resolve_current_academic_term()
    eng = _engine()
    _run_upgrade(eng)

    from sqlalchemy.orm import sessionmaker

    s = sessionmaker(bind=eng)()
    rows = s.query(AcademicTerm).all()
    assert len(rows) == 1
    assert (rows[0].school_year, rows[0].semester) == (tsy, tsem)
    assert rows[0].is_current is True
    exp_start, exp_end = term_bounds(tsy, tsem)
    assert rows[0].start_date == exp_start
    assert rows[0].end_date == exp_end
    s.close()
