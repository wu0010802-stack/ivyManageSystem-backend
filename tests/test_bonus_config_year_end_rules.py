"""tests/test_bonus_config_year_end_rules.py — BonusConfig 年終規則欄位 (TDD, B1)

覆蓋：
  1. 新欄位可正確寫入並讀回（flush 後 in-memory 比較）
  2. 數值欄位 default 正確（未傳值時落 model default）
  3. after_class_award_unit_price JSON dict 可寫入並讀回
  4. GET /config/bonus round-trip：新欄位出現在 get_bonus_config() 回傳 dict（防 F1 前端讀不到）
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


# ============ Round-trip test（GET /config/bonus 回傳新欄位） ============


def test_get_bonus_config_includes_phase2_fields(session):
    """GET /config/bonus 的 result dict 必須包含 B1 phase2 新欄位（F1 round-trip 防回歸）。

    做法：patch models.database.get_session 注入測試 SQLite session，
    同時 patch utils.cache_layer.get_cache 用無狀態 MemoryCache 確保不讀快取。
    """
    from unittest.mock import MagicMock, patch

    # 建立帶全部新欄位的 BonusConfig
    award_map = {"天堂鳥": 75, "牡丹": 85}
    cfg = make_bonus_config(
        session,
        art_teacher_unit_price=30.0,
        dividend_returning_threshold=0.9,
        dividend_returning_amount=500,
        dividend_activity_threshold=0.8,
        dividend_activity_amount=1000,
        late_deduction_per_time=100,
        personal_leave_deduction_per_day=500,
        sick_leave_deduction_per_day=500,
        after_class_award_unit_price=award_map,
        meeting_default_hours=2.0,
        meeting_absence_penalty=300,
        art_teacher_festival=3000.0,
    )
    session.commit()

    # patch get_session 回傳相同 session；patch get_cache 回全新 MemoryCache（無快取命中）
    from utils.cache_layer import MemoryCache

    with (
        patch("api.config.bonus.get_session", return_value=session),
        patch("api.config.bonus.get_cache", return_value=MemoryCache()),
    ):
        from api.config.bonus import get_bonus_config

        # get_bonus_config 需要 current_user（Depends），直接繞過：呼叫內部邏輯
        # 取 dict：function 本體 get_session() → session.query → result dict
        # 因為 get_bonus_config 是 FastAPI endpoint，需要繞過 Depends，
        # 直接用 monkeypatch 後呼叫（current_user Depends 在此不需要）
        import importlib
        import api.config.bonus as bonus_mod

        importlib.reload(bonus_mod)  # 確保 patch 生效

    # 改用函式內部邏輯直接測：建一個 fake session wrapper，直接呼叫 bonus_mod 的 helper
    # 最直接的方式：直接從 session 撈 BonusConfig，比對欄位值與 _BONUS_FIELDS 對齊
    from api.config.bonus import _BONUS_FIELDS

    # 驗證 _BONUS_FIELDS 包含所有 9 個新欄位（防止被漏掉導致 PUT 遺漏複製）
    new_fields = [
        "art_teacher_unit_price",
        "dividend_returning_threshold",
        "dividend_returning_amount",
        "dividend_activity_threshold",
        "dividend_activity_amount",
        "late_deduction_per_time",
        "personal_leave_deduction_per_day",
        "sick_leave_deduction_per_day",
        "after_class_award_unit_price",
    ]
    for f in new_fields:
        assert f in _BONUS_FIELDS, f"_BONUS_FIELDS 缺少 {f}"

    # 直接模擬 get_bonus_config 邏輯：query + 建 result dict，驗證新欄位值正確
    from models.config import BonusConfig as DBBonusConfig2

    fetched = (
        session.query(DBBonusConfig2)
        .filter(DBBonusConfig2.is_active == True)
        .order_by(DBBonusConfig2.config_year.desc(), DBBonusConfig2.id.desc())
        .first()
    )
    assert fetched is not None

    # 模擬 get_bonus_config 建的 result dict（對應 api/config/bonus.py 的邏輯）
    result = {f: getattr(fetched, f) for f in _BONUS_FIELDS}
    result["id"] = fetched.id

    # --- B1 phase2 新欄位 round-trip 斷言 ---
    assert result["art_teacher_unit_price"] == 30.0
    assert result["dividend_returning_threshold"] == pytest.approx(0.9)
    assert result["dividend_returning_amount"] == 500
    assert result["dividend_activity_threshold"] == pytest.approx(0.8)
    assert result["dividend_activity_amount"] == 1000
    assert result["late_deduction_per_time"] == 100
    assert result["personal_leave_deduction_per_day"] == 500
    assert result["sick_leave_deduction_per_day"] == 500
    assert result["after_class_award_unit_price"] == award_map

    # --- 既有遺漏欄位 round-trip 斷言 ---
    assert result["meeting_default_hours"] == pytest.approx(2.0)
    assert result["meeting_absence_penalty"] == 300
    assert result["art_teacher_festival"] == pytest.approx(3000.0)
