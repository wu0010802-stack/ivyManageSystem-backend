"""utils.term_events hooks registry 測試。"""

import pytest
from datetime import date
from unittest.mock import MagicMock

from utils.term_events import (
    on_term_changed,
    register_handler,
    fire_term_changed,
    reset_handlers_for_tests,
    list_handler_names,
)


@pytest.fixture(autouse=True)
def _reset():
    """確保每個 test 從空 registry 開始。"""
    reset_handlers_for_tests()
    yield
    reset_handlers_for_tests()


class _FakeTerm:
    def __init__(self, school_year, semester, id_=1):
        self.id = id_
        self.school_year = school_year
        self.semester = semester


class TestHooksRegistry:
    def test_register_duplicate_raises(self):
        @on_term_changed("dup")
        def h1(*, old, new, session):
            pass

        with pytest.raises(RuntimeError, match="dup"):

            @on_term_changed("dup")
            def h2(*, old, new, session):
                pass

    def test_fire_no_handlers_is_noop(self, caplog):
        new_term = _FakeTerm(115, 1)
        session = MagicMock()
        # 不應 raise
        fire_term_changed(old=None, new=new_term, session=session)

    def test_fire_order_matches_registration(self):
        calls = []

        @on_term_changed("a")
        def h_a(*, old, new, session):
            calls.append("a")

        @on_term_changed("b")
        def h_b(*, old, new, session):
            calls.append("b")

        @on_term_changed("c")
        def h_c(*, old, new, session):
            calls.append("c")

        fire_term_changed(old=None, new=_FakeTerm(115, 1), session=MagicMock())
        assert calls == ["a", "b", "c"]

    def test_fire_handler_raise_propagates_and_stops_chain(self):
        calls = []

        @on_term_changed("first")
        def h1(*, old, new, session):
            calls.append("first")

        @on_term_changed("boom")
        def h2(*, old, new, session):
            calls.append("boom")
            raise ValueError("subscriber failure")

        @on_term_changed("never")
        def h3(*, old, new, session):
            calls.append("never")

        with pytest.raises(ValueError, match="subscriber failure"):
            fire_term_changed(old=None, new=_FakeTerm(115, 1), session=MagicMock())
        assert calls == ["first", "boom"]  # 第三個沒跑

    def test_reset_handlers_clears_registry(self):
        @on_term_changed("x")
        def h(*, old, new, session):
            pass

        assert "x" in list_handler_names()
        reset_handlers_for_tests()
        assert list_handler_names() == []

    def test_register_handler_explicit_api(self):
        called = []

        def manual(*, old, new, session):
            called.append(True)

        register_handler("manual", manual)
        fire_term_changed(old=None, new=_FakeTerm(115, 1), session=MagicMock())
        assert called == [True]
