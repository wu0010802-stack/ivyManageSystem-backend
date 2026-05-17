"""tests/test_workday_rules.py — workday_rules 測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.event import Holiday, WorkdayOverride  # noqa: F401 metadata
from services.workday_rules import classify_day, load_day_rule_maps


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _add_holiday(s, d, name, active=True):
    s.add(Holiday(date=d, name=name, is_active=active))
    s.flush()


def _add_makeup(s, d, name, active=True):
    s.add(WorkdayOverride(date=d, name=name, is_active=active))
    s.flush()


class TestLoadDayRuleMaps:
    def test_loads_active_holidays_and_makeups(self, session):
        _add_holiday(session, date(2026, 1, 1), "元旦")
        _add_makeup(session, date(2026, 2, 8), "補上班")
        holidays, makeups = load_day_rule_maps(
            session, date(2026, 1, 1), date(2026, 12, 31)
        )
        assert holidays == {date(2026, 1, 1): "元旦"}
        assert makeups == {date(2026, 2, 8): "補上班"}

    def test_excludes_inactive(self, session):
        _add_holiday(session, date(2026, 1, 1), "停用假日", active=False)
        _add_makeup(session, date(2026, 2, 8), "停用補班", active=False)
        holidays, makeups = load_day_rule_maps(
            session, date(2026, 1, 1), date(2026, 12, 31)
        )
        assert holidays == {}
        assert makeups == {}

    def test_excludes_out_of_range(self, session):
        _add_holiday(session, date(2025, 12, 25), "範圍前")
        _add_holiday(session, date(2027, 1, 1), "範圍後")
        _add_holiday(session, date(2026, 6, 1), "範圍內")
        holidays, _ = load_day_rule_maps(session, date(2026, 1, 1), date(2026, 12, 31))
        assert holidays == {date(2026, 6, 1): "範圍內"}

    def test_empty_db_returns_empty_dicts(self, session):
        holidays, makeups = load_day_rule_maps(
            session, date(2026, 1, 1), date(2026, 12, 31)
        )
        assert holidays == {}
        assert makeups == {}


class TestClassifyDay:
    def test_makeup_takes_precedence_over_weekend(self):
        # 2026-02-08 是週日，但若被覆寫為補上班則 kind=workday
        d = date(2026, 2, 8)  # Sunday
        assert d.weekday() == 6
        result = classify_day(d, holiday_map={}, makeup_map={d: "補上班"})
        assert result["kind"] == "workday"
        assert result["is_makeup_workday"] is True
        assert result["is_weekend"] is False
        assert result["workday_override_name"] == "補上班"

    def test_holiday_returns_holiday_kind(self):
        d = date(2026, 1, 1)
        result = classify_day(d, holiday_map={d: "元旦"}, makeup_map={})
        assert result["kind"] == "holiday"
        assert result["is_holiday"] is True
        assert result["holiday_name"] == "元旦"

    def test_weekend_no_overrides(self):
        d = date(2026, 1, 3)  # Saturday
        assert d.weekday() == 5
        result = classify_day(d, holiday_map={}, makeup_map={})
        assert result["kind"] == "weekend"
        assert result["is_weekend"] is True
        assert result["is_holiday"] is False

    def test_normal_workday(self):
        d = date(2026, 1, 5)  # Monday
        result = classify_day(d, holiday_map={}, makeup_map={})
        assert result["kind"] == "workday"
        assert result["is_weekend"] is False
        assert result["is_holiday"] is False
        assert result["is_makeup_workday"] is False
        assert result["holiday_name"] is None

    def test_makeup_beats_holiday_when_both(self):
        # 政策上不應同時存在，但邏輯走 makeup 先 → workday
        d = date(2026, 1, 1)
        result = classify_day(d, holiday_map={d: "元旦"}, makeup_map={d: "補上班"})
        assert result["kind"] == "workday"
        assert result["is_makeup_workday"] is True
