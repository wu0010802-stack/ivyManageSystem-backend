"""tests/test_bonus_config_year_end_rules.py — BonusConfig 年終規則欄位 (TDD, B1)

覆蓋：
  1. 新欄位可正確寫入並讀回（flush 後 in-memory 比較）
  2. 數值欄位 default 正確（未傳值時落 model default）
  3. after_class_award_unit_price JSON dict 可寫入並讀回
  4. GET /config/bonus round-trip：新欄位出現在 endpoint 回傳 JSON（防 F1 前端讀不到）
     - 真正呼叫 FastAPI TestClient，命中 api/config/bonus.py 第 144-180 行的 literal dict
     - 不用 importlib.reload；不用 patch；cache 每次重置
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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

import models.base as base_module
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


@pytest.fixture
def client_with_db(tmp_path):
    """TestClient fixture：換掉全域 engine/SessionFactory，建 TestClient + 登入用帳號。

    與 test_bonus_config_finance_guard.py 的 client_with_db 同模式，
    額外在 teardown 重置 cache singleton 確保不汙染其他測試。
    """
    from api.auth import _account_failures, _ip_attempts
    from api.auth import router as auth_router
    from api.config import router as config_router
    from models.auth import User
    from models.database import Base as DBBase
    from utils.auth import hash_password
    from utils.cache_layer import reset_cache_for_testing

    db_path = tmp_path / "year-end-round-trip.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    DBBase.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    reset_cache_for_testing()  # 清掉 config_bonus 快取，確保 GET 重新讀 DB

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(config_router)

    with session_factory() as setup_session:
        setup_session.add(
            User(
                username="settings_user",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["SETTINGS_READ"],
                is_active=True,
            )
        )
        setup_session.commit()

    with TestClient(app) as client:
        # 登入取得 session cookie
        res = client.post(
            "/api/auth/login",
            json={"username": "settings_user", "password": "TempPass123"},
        )
        assert res.status_code == 200, f"login failed: {res.text}"
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    reset_cache_for_testing()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
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


def test_get_bonus_config_includes_phase2_fields(client_with_db):
    """GET /config/bonus endpoint 的 JSON response 必須包含 B1 phase2 新欄位。

    防回歸：api/config/bonus.py 第 144-180 行 literal dict 若漏寫任何新欄位，
    前端（F1）就讀不到，此測試必須 FAIL。

    做法：真正呼叫 FastAPI TestClient → GET /api/config/bonus → 斷言 JSON body。
    cache 在 fixture 進出都重置，不會因快取命中跳過 literal dict 構建。
    """
    client, sf = client_with_db

    award_map = {"天堂鳥": 75, "牡丹": 85}

    # 透過 app 的 session factory 寫入 BonusConfig
    with sf() as s:
        bc = BonusConfig(
            config_year=2025,
            version=1,
            is_active=True,
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
        s.add(bc)
        s.commit()

    # 呼叫真實 endpoint（命中 api/config/bonus.py 的 literal dict 構建邏輯）
    res = client.get("/api/config/bonus")
    assert res.status_code == 200, f"GET /api/config/bonus 失敗: {res.text}"
    body = res.json()

    # --- B1 phase2 新欄位 round-trip 斷言（漏掉任一欄位即 FAIL）---
    assert body["art_teacher_unit_price"] == pytest.approx(30.0)
    assert body["dividend_returning_threshold"] == pytest.approx(0.9)
    assert body["dividend_returning_amount"] == pytest.approx(500)
    assert body["dividend_activity_threshold"] == pytest.approx(0.8)
    assert body["dividend_activity_amount"] == pytest.approx(1000)
    assert body["late_deduction_per_time"] == pytest.approx(100)
    assert body["personal_leave_deduction_per_day"] == pytest.approx(500)
    assert body["sick_leave_deduction_per_day"] == pytest.approx(500)
    assert body["after_class_award_unit_price"] == award_map

    # --- 既有欄位確認未回歸 ---
    assert body["meeting_default_hours"] == pytest.approx(2.0)
    assert body["meeting_absence_penalty"] == 300
    assert body["art_teacher_festival"] == pytest.approx(3000.0)


def test_bonus_config_fields_in_bonus_fields_list():
    """_BONUS_FIELDS 必須包含所有 9 個新欄位（防 PUT 複製時遺漏）。

    與 round-trip 測試互補：round-trip 測 GET literal dict，
    此測試防 PUT copy 路徑（_BONUS_FIELDS 迴圈）漏欄。
    """
    from api.config.bonus import _BONUS_FIELDS

    required = [
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
    for field in required:
        assert field in _BONUS_FIELDS, f"_BONUS_FIELDS 缺少 {field}（PUT 複製會遺漏）"
