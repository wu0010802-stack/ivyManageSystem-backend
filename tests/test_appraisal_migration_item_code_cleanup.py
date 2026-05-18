"""migration aprcal001 P1-2 defensive item_code cleanup 驗收。

執行 migration 中段的 UPDATE statement 序列，驗證即便有舊小寫 item_code
殘值（理論上 1bcb251f 之後不會發生，但本 cleanup 是 fail-safe），也能被
正確 rename 到新 14-code enum 值。

對應 bug_sweep_2026_05_18.md P1-2。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models.base as base_module
from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoreItem,
    CycleStatus,
    RoleGroup,
    Semester,
)
from models.database import Base
from models.employee import Employee


@pytest.fixture
def migrated_db(tmp_path):
    """獨立 sqlite session（避免污染其他 test）。"""
    db_path = tmp_path / "p12-migration-cleanup.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    session = session_factory()
    yield session, engine
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_minimal(session):
    """建一個 cycle + participant，供 score_items 掛 FK。"""
    emp = Employee(employee_id="E001", name="老師A", is_active=True)
    session.add(emp)
    session.flush()
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    session.add(cycle)
    session.flush()
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    session.add(p)
    session.flush()
    return cycle, p


def _run_p12_item_code_cleanup(engine):
    """執行 migration P1-2 區段的 UPDATE statements（純 SQL，跨 dialect）。"""
    item_code_rename = [
        ("attendance", "LATE_EARLY"),
        ("returning_rate", "RETURNING_RATE_0315"),
        ("after_class", "AFTER_CLASS_RATE"),
        ("disciplinary", "REWARD_PUNISH"),
    ]
    with engine.begin() as conn:
        for old, new in item_code_rename:
            conn.execute(
                text(
                    "UPDATE appraisal_score_items SET item_code = :new "
                    "WHERE item_code = :old"
                ),
                {"old": old, "new": new},
            )


def test_p12_item_code_cleanup_renames_legacy_lowercase_values(migrated_db):
    """埋 4 條舊小寫 item_code → 跑 cleanup → 全部變新 enum 大寫值。"""
    session, engine = migrated_db
    _cycle, p = _seed_minimal(session)
    session.commit()  # 釋放 sqlite write lock 讓後續 engine.begin() 寫入
    pid, cid = p.id, p.cycle_id
    # raw SQL 寫入舊小寫值，繞過 ORM enum 檢查（模擬歷史殘值）
    legacy_codes = ["attendance", "returning_rate", "after_class", "disciplinary"]
    with engine.begin() as conn:
        for i, code in enumerate(legacy_codes):
            conn.execute(
                text(
                    "INSERT INTO appraisal_score_items "
                    "(participant_id, cycle_id, item_code, sequence_no, "
                    "score_delta) VALUES "
                    "(:pid, :cid, :code, :seq, :sd)"
                ),
                {
                    "pid": pid,
                    "cid": cid,
                    "code": code,
                    "seq": i + 1,
                    "sd": "0",
                },
            )

    # 前置驗證：DB 確實含舊小寫 row
    with engine.connect() as conn:
        before = (
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM appraisal_score_items "
                    "WHERE item_code IN ('attendance','returning_rate',"
                    "'after_class','disciplinary')"
                )
            ).scalar()
            or 0
        )
        assert before == 4, f"前置 seed 失敗，舊值 count={before}"

    # 跑 migration P1-2 cleanup
    _run_p12_item_code_cleanup(engine)

    # 後置驗證：舊值 0、新值 4
    with engine.connect() as conn:
        leftover = (
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM appraisal_score_items "
                    "WHERE item_code IN ('attendance','returning_rate',"
                    "'after_class','disciplinary')"
                )
            ).scalar()
            or 0
        )
        renamed = (
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM appraisal_score_items "
                    "WHERE item_code IN ('LATE_EARLY','RETURNING_RATE_0315',"
                    "'AFTER_CLASS_RATE','REWARD_PUNISH')"
                )
            ).scalar()
            or 0
        )
    assert leftover == 0, f"cleanup 後仍殘留舊小寫值 {leftover} 筆"
    assert renamed == 4, f"應 rename 為 4 條新 enum 值，實際 {renamed} 條"


def test_p12_item_code_cleanup_is_idempotent_when_no_legacy_rows(migrated_db):
    """prod 預期無舊值 → cleanup 不應 affect 任何 row。"""
    session, engine = migrated_db
    _cycle, p = _seed_minimal(session)
    session.commit()  # 釋放 sqlite write lock
    pid, cid = p.id, p.cycle_id

    # 只塞新 enum 值
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO appraisal_score_items "
                "(participant_id, cycle_id, item_code, sequence_no, "
                "score_delta) VALUES "
                "(:pid, :cid, 'LATE_EARLY', 1, '-0.25')"
            ),
            {"pid": pid, "cid": cid},
        )

    # 跑 cleanup
    _run_p12_item_code_cleanup(engine)

    # 新值仍在、沒被誤動
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT item_code FROM appraisal_score_items "
                    "WHERE participant_id = :pid"
                ),
                {"pid": pid},
            )
            .scalars()
            .all()
        )
    assert rows == ["LATE_EARLY"]
