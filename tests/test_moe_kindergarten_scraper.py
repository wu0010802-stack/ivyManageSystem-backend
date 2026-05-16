"""教育部幼兒園爬蟲 moe_kindergarten_scraper 回歸測試。

重點覆蓋：
- 主同步流程末端的 kiang 補充同步必須真的被呼叫（line 690 NameError 回歸）
- 每頁 N+1 SELECT 已收斂為批次查詢
- HTTP 失敗時的重試行為
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import moe_kindergarten_scraper as scraper


class _FakeSession:
    """極簡 fake DB session：支援 query().filter_by().first() / add() / flush()。"""

    def __init__(self, existing: list[Any] | None = None):
        self._existing_by_name: dict[str, Any] = {}
        for e in existing or []:
            name = getattr(e, "school_name", None)
            if name:
                self._existing_by_name[name] = e
        self.added: list[Any] = []
        self.flushed = 0
        self.query_count = 0
        self.filter_by_count = 0

    def query(self, _model):
        self.query_count += 1
        return _FakeQuery(self)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed += 1


class _FakeQuery:
    def __init__(self, sess: _FakeSession):
        self._sess = sess
        self._name_filter: list[str] | None = None
        self._single_name: str | None = None

    def filter(self, *args):
        # 模擬 `CompetitorSchool.school_name.in_([...])`
        for arg in args:
            try:
                clauses = getattr(arg, "clauses", None)
                if clauses is not None:
                    # in_() 產出的 BinaryExpression
                    right = getattr(arg, "right", None)
                    if right is not None and hasattr(right, "value"):
                        val = right.value
                        if isinstance(val, (list, tuple, set)):
                            self._name_filter = list(val)
            except Exception:
                pass
        return self

    def filter_by(self, **kwargs):
        self._sess.filter_by_count += 1
        if "school_name" in kwargs:
            self._single_name = kwargs["school_name"]
        return self

    def first(self):
        if self._single_name is not None:
            return self._sess._existing_by_name.get(self._single_name)
        return None

    def all(self):
        if self._name_filter is not None:
            return [
                self._sess._existing_by_name[n]
                for n in self._name_filter
                if n in self._sess._existing_by_name
            ]
        return list(self._sess._existing_by_name.values())


def _install_minimal_mocks(
    monkeypatch,
    fake_db: _FakeSession,
    *,
    pages: list[list[dict]] | None = None,
):
    """把 sync_moe_kindergartens 跑完一輪所需的外部呼叫全部 stub 掉。

    pages：每頁要回的學校 list；None 表示空頁（最快終止）。
    """
    monkeypatch.setattr(scraper, "_SYNC_LOCK", threading.Lock())
    monkeypatch.setattr(scraper, "_make_session", lambda: SimpleNamespace())
    monkeypatch.setattr(scraper, "_fetch_search_page", lambda _s: "<html>home</html>")
    monkeypatch.setattr(
        scraper,
        "_get_hidden_fields",
        lambda _h: {"__VIEWSTATE": "v", "__VIEWSTATEGENERATOR": "g", "__EVENTVALIDATION": "e"},
    )
    monkeypatch.setattr(
        scraper, "_submit_search", lambda *a, **kw: "<html>result</html>"
    )
    monkeypatch.setattr(scraper, "_fetch_punish_data", lambda _s: {})

    if pages is None:
        pages = [[]]
    page_iter = iter(pages)

    def fake_parse(_html):
        try:
            return next(page_iter)
        except StopIteration:
            return []

    monkeypatch.setattr(scraper, "_parse_gridview_schools", fake_parse)
    monkeypatch.setattr(
        scraper,
        "_has_next_page",
        lambda _h: False,  # 預設只跑一頁
    )

    # _update_sync_state 內部會自己 open session_scope，stub 掉避免碰 DB
    monkeypatch.setattr(scraper, "_update_sync_state", lambda *a, **kw: None)

    @contextmanager
    def _scope():
        yield fake_db

    monkeypatch.setattr(scraper, "session_scope", _scope)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)


class TestKiangSupplementarySession:
    """line 690 bug 回歸：kiang 補充同步必須拿到有效 db session。"""

    def test_sync_calls_kiang_with_valid_session(self, monkeypatch):
        fake_db = _FakeSession()
        _install_minimal_mocks(monkeypatch, fake_db)

        kiang_calls: list[tuple[Any, Any]] = []

        def fake_kiang(http_sess, db_session):
            kiang_calls.append((http_sess, db_session))
            assert db_session is not None
            assert hasattr(db_session, "query"), (
                f"kiang 拿到的不是 DB session：type={type(db_session)}"
            )
            return 0

        monkeypatch.setattr(scraper, "_sync_kiang_supplementary", fake_kiang)

        result = scraper.sync_moe_kindergartens()

        assert result["status"] == "success", result
        assert kiang_calls, (
            "kiang 補充同步未被呼叫（line 690 NameError 把整段攔截了）"
        )
        assert len(kiang_calls) == 1


class TestParseGridviewSchools:
    """parser 純函式，無 DB 依賴。"""

    def test_returns_empty_list_when_no_match(self):
        assert scraper._parse_gridview_schools("<html></html>") == []


# ── per-page N+1 整合測試（SQLite + 真實 CompetitorSchool）──────────────


@pytest.fixture
def sqlite_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    return engine, SessionFactory


def _stub_for_per_page_test(monkeypatch, sqlite_engine, schools_one_page):
    """讓 sync_moe_kindergartens 跑「一頁 N 筆 schools」並改用 SQLite 真實 session_scope。"""
    from contextlib import contextmanager
    engine, SessionFactory = sqlite_engine

    @contextmanager
    def real_session_scope():
        s = SessionFactory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(scraper, "_SYNC_LOCK", threading.Lock())
    monkeypatch.setattr(scraper, "_make_session", lambda: SimpleNamespace())
    monkeypatch.setattr(scraper, "_fetch_search_page", lambda _s: "<html>home</html>")
    monkeypatch.setattr(
        scraper,
        "_get_hidden_fields",
        lambda _h: {"__VIEWSTATE": "v"},
    )
    monkeypatch.setattr(scraper, "_submit_search", lambda *a, **kw: "<html>r</html>")
    monkeypatch.setattr(scraper, "_fetch_punish_data", lambda _s: {})
    monkeypatch.setattr(scraper, "_parse_gridview_schools", lambda _h: schools_one_page)
    monkeypatch.setattr(scraper, "_has_next_page", lambda _h: False)
    monkeypatch.setattr(scraper, "_update_sync_state", lambda *a, **kw: None)
    monkeypatch.setattr(scraper, "_sync_kiang_supplementary", lambda *a, **kw: 0)
    monkeypatch.setattr(scraper, "session_scope", real_session_scope)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)


class TestRequestRetry:
    """`_request_with_retry` 應對 Timeout/ConnectionError 做指數退避重試。"""

    def test_retry_succeeds_on_third_attempt(self, monkeypatch):
        import requests

        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

        attempts = {"n": 0}

        def flaky_op():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise requests.Timeout("人為 timeout")
            return "OK"

        result = scraper._request_with_retry(flaky_op, label="test")
        assert result == "OK"
        assert attempts["n"] == 3
        # 兩次重試之間 sleep 兩次（指數退避 0.5、1.0）
        assert len(sleeps) == 2
        assert sleeps[0] < sleeps[1]  # backoff 遞增

    def test_retry_gives_up_after_max(self, monkeypatch):
        import requests

        monkeypatch.setattr(time, "sleep", lambda *_: None)

        def always_timeout():
            raise requests.ConnectionError("conn refused")

        result = scraper._request_with_retry(
            always_timeout, label="test", max_retries=2
        )
        assert result is None

    def test_non_retryable_exception_propagates(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        def raises_value_error():
            raise ValueError("不該重試")

        with pytest.raises(ValueError):
            scraper._request_with_retry(raises_value_error, label="test")


class TestPerPageNoNPlus1:
    """每頁迴圈內對 competitor_school 的 SELECT 必須收斂為一次 in_() 批撈。"""

    def test_per_page_existing_lookup_is_batched(self, monkeypatch, sqlite_engine):
        from sqlalchemy import event

        engine, _ = sqlite_engine
        schools = [
            {"school_name": f"幼兒園{i}", "owner_name": "王" + str(i)}
            for i in range(10)
        ]
        _stub_for_per_page_test(monkeypatch, sqlite_engine, schools)

        select_school_count = 0

        @event.listens_for(engine, "before_cursor_execute")
        def _count(_conn, _cursor, statement, *_):
            nonlocal select_school_count
            stmt_lower = statement.lower()
            if (
                "from competitor_school" in stmt_lower
                and stmt_lower.lstrip().startswith("select")
            ):
                select_school_count += 1

        result = scraper.sync_moe_kindergartens()

        assert result["status"] == "success", result
        assert result["created"] == 10
        # 修前：10 筆 schools ⇒ 10 次 SELECT competitor_school
        # 修後：1 次（kiang 補充同步可能再 1 次 lookup，但 _sync_kiang_supplementary 已 stub）
        assert select_school_count <= 2, (
            f"N+1 未修：對 competitor_school 執行了 {select_school_count} 次 SELECT，"
            "預期 ≤ 2 次"
        )
