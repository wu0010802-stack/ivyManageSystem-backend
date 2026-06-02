"""tests/test_bonus_config_year_end_rules.py — BonusConfig 年終規則欄位 (TDD, B1)

覆蓋：
  1. 新欄位可正確寫入並讀回（flush 後 in-memory 比較）
  2. 數值欄位 default 正確（未傳值時落 model default）
  3. after_class_award_unit_price JSON dict 可寫入並讀回
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 讓 tests 可以 import backend 模組
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault(
    "MEDICAL_FIELD_ENCRYPTION_KEY",
    "GEdGVEpP4ao9zTk1iIAWFdnEoJ_8ipzw5Y0ZgCerXh4=",
)

# SQLite 相容性修補（必須在所有模型 import 前）
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON

_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]

from models.base import Base
from models.config import BonusConfig

# ============ Fixtures ============


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


# ============ helpers ============


def make_bonus_config(session, **overrides) -> BonusConfig:
    """建一筆 BonusConfig 並 flush；只需傳入要覆蓋的欄位。"""
    defaults = dict(
        config_year=2025,
        version=1,
        is_active=True,
    )
    defaults.update(overrides)
    bc = BonusConfig(**defaults)
    session.add(bc)
    session.flush()
    return bc


# ============ Tests ============


def test_bonus_config_year_end_rule_fields(session):
    """任務規格驗收測試（先寫、後實作）。"""
    cfg = make_bonus_config(
        session,
        art_teacher_unit_price=30,
        dividend_returning_threshold=Decimal("0.9"),
        dividend_returning_amount=500,
        dividend_activity_threshold=Decimal("0.8"),
        dividend_activity_amount=1000,
        late_deduction_per_time=100,
        personal_leave_deduction_per_day=500,
        sick_leave_deduction_per_day=500,
        after_class_award_unit_price={"天堂鳥": 75, "牡丹": 85},
    )
    assert cfg.dividend_returning_amount == 500


def test_bonus_config_art_teacher_unit_price(session):
    """才藝老師單價欄位可寫入並讀回。"""
    cfg = make_bonus_config(session, art_teacher_unit_price=45.5)
    assert cfg.art_teacher_unit_price == 45.5


def test_bonus_config_dividend_thresholds_defaults(session):
    """紅利門檻欄位未傳值時套用 model default。"""
    cfg = make_bonus_config(session)
    # model default: dividend_returning_threshold=0.9, dividend_activity_threshold=0.8
    assert cfg.dividend_returning_threshold == pytest.approx(0.9)
    assert cfg.dividend_activity_threshold == pytest.approx(0.8)
    assert cfg.dividend_returning_amount == 500
    assert cfg.dividend_activity_amount == 1000


def test_bonus_config_deduction_defaults(session):
    """考勤扣款欄位未傳值時套用 model default。"""
    cfg = make_bonus_config(session)
    assert cfg.late_deduction_per_time == 100
    assert cfg.personal_leave_deduction_per_day == 500
    assert cfg.sick_leave_deduction_per_day == 500


def test_bonus_config_after_class_award_unit_price_json(session):
    """after_class_award_unit_price 可存 JSON dict。"""
    award_map = {"天堂鳥": 75, "牡丹": 85, "小班": 60}
    cfg = make_bonus_config(session, after_class_award_unit_price=award_map)
    assert cfg.after_class_award_unit_price == award_map


def test_bonus_config_after_class_award_unit_price_nullable(session):
    """after_class_award_unit_price 未傳值時為 None（nullable）。"""
    cfg = make_bonus_config(session)
    # nullable，未傳值 default=None
    assert cfg.after_class_award_unit_price is None
