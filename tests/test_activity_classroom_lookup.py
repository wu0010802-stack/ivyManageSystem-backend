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


def _make_classroom(s, name="小班 A", active=True, school_year=None, semester=None):
    kwargs = {"name": name, "is_active": active}
    if school_year is not None:
        kwargs["school_year"] = school_year
    if semester is not None:
        kwargs["semester"] = semester
    c = Classroom(**kwargs)
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


class TestTermAwareLookup:
    """2026-06-29 才藝點名稽核 F3：班級反查須可依學期收斂。

    Classroom 有 uq(school_year, semester, name)，同名班級可跨學期各一筆且同時啟用
    （學期交接期）。原 _get_active_classroom 只按 name+is_active `.first()`，會任意
    取到舊學期那筆 → 校外/未匹配生報名綁到錯學期班級 FK。
    """

    def test_term_filter_picks_matching_term(self, session):
        old = _make_classroom(session, name="大象班", school_year=113, semester=2)
        new = _make_classroom(session, name="大象班", school_year=114, semester=1)
        # 帶學期 → 精確命中該學期，不受 .first() 任意順序影響
        result = _get_active_classroom(session, "大象班", school_year=114, semester=1)
        assert result is not None
        assert result.id == new.id
        result_old = _get_active_classroom(
            session, "大象班", school_year=113, semester=2
        )
        assert result_old.id == old.id

    def test_require_raises_when_term_mismatch(self, session):
        _make_classroom(session, name="大象班", school_year=113, semester=2)
        # 只有 113-2 大象班，要 114-1 → 該學期無此班 → 400（不會誤綁到 113-2）
        with pytest.raises(HTTPException) as exc:
            _require_active_classroom(session, "大象班", school_year=114, semester=1)
        assert exc.value.status_code == 400

    def test_no_term_keeps_backward_compatible(self, session):
        c = _make_classroom(session, name="大象班", school_year=114, semester=1)
        # 不帶學期 → 行為不變（沿用 name+is_active），向後相容既有 caller
        result = _get_active_classroom(session, "大象班")
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
