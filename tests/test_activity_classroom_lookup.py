"""tests/test_activity_classroom_lookup.py — activity_classroom_lookup 測試。"""

import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Classroom  # noqa: F401 metadata
from services.activity_classroom_lookup import (
    _get_active_classroom,
    _require_active_classroom,
)


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


def _make_classroom(s, name="小班 A", active=True):
    c = Classroom(name=name, is_active=active)
    s.add(c)
    s.flush()
    return c


class TestGetActiveClassroom:
    def test_returns_active_classroom(self, session):
        c = _make_classroom(session, name="小班 A", active=True)
        result = _get_active_classroom(session, "小班 A")
        assert result is not None
        assert result.id == c.id

    def test_returns_none_when_missing(self, session):
        result = _get_active_classroom(session, "不存在班級")
        assert result is None

    def test_returns_none_when_inactive(self, session):
        _make_classroom(session, name="停用班", active=False)
        result = _get_active_classroom(session, "停用班")
        assert result is None

    def test_strips_whitespace_in_lookup(self, session):
        c = _make_classroom(session, name="小班 A", active=True)
        # 名稱搜尋會被 .strip()，前後空白應被忽略
        result = _get_active_classroom(session, "  小班 A  ")
        assert result is not None
        assert result.id == c.id


class TestRequireActiveClassroom:
    def test_returns_active_classroom(self, session):
        c = _make_classroom(session, name="小班 A", active=True)
        result = _require_active_classroom(session, "小班 A")
        assert result.id == c.id

    def test_raises_400_when_missing(self, session):
        with pytest.raises(HTTPException) as exc:
            _require_active_classroom(session, "不存在班級")
        assert exc.value.status_code == 400
        assert "班級不存在" in exc.value.detail or "停用" in exc.value.detail

    def test_raises_400_when_inactive(self, session):
        _make_classroom(session, name="停用班", active=False)
        with pytest.raises(HTTPException) as exc:
            _require_active_classroom(session, "停用班")
        assert exc.value.status_code == 400
