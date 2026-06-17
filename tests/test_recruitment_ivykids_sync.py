"""義華校官網同步 parser 與 _run_sync 整合測試。"""

import os
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import recruitment_ivykids_sync as sync_service


def test_parse_backend_list_row_supports_leading_sort_column():
    row_html = """
    <tr>
        <td></td>
        <td style="color:green">預約正常</td>
        <td>2026-04-15 上午場 10:00</td>
        <td>范瑀玹</td>
        <td>2024-02-20</td>
        <td>0919766932</td>
        <td>親友介紹</td>
        <td>2026-04-12 19:59:45</td>
        <td>
            <a href="form.php?id=177"><button type="button">編輯</button></a>
            <a href="?delid=177"><button type="button">刪除</button></a>
        </td>
    </tr>
    """

    record = sync_service._parse_backend_list_row(
        row_html,
        "https://www.ivykids.tw/manage/make_an_appointment/index.php?page=1",
    )

    assert record is not None
    assert record.external_id == "177"
    assert record.status == "預約正常"
    assert record.visit_date == "2026-04-15 上午場 10:00"
    assert record.child_name == "范瑀玹"
    assert record.phone == "0919766932"
    assert record.source == "親友介紹"
    assert record.created_at == "2026-04-12 19:59:45"
    assert record.month == "115.04"


# ── SSRF 防護：detail_url / next-page 必須限制在官網網域 ─────────────────────


_PAGE_URL = "https://www.ivykids.tw/manage/make_an_appointment/index.php?page=1"


def test_parse_backend_list_row_rejects_offdomain_detail_link():
    """惡意官網 HTML 含指向內網/外網絕對 URL 的 form.php 連結時，
    不應產生對該 URL 的 detail_url（避免 server 端 SSRF GET）。"""
    row_html = """
    <tr>
        <td></td>
        <td>預約正常</td>
        <td>2026-04-15</td>
        <td>范瑀玹</td>
        <td>2024-02-20</td>
        <td>0919766932</td>
        <td>親友介紹</td>
        <td>2026-04-12 19:59:45</td>
        <td>
            <a href="http://169.254.169.254/manage/form.php?id=1">編輯</a>
        </td>
    </tr>
    """

    record = sync_service._parse_backend_list_row(row_html, _PAGE_URL)

    # 被過濾：要嘛整列丟棄(None)，要嘛 detail_url 不指向內網
    if record is not None:
        assert record.detail_url is None or "169.254.169.254" not in (
            record.detail_url or ""
        )
    # 並確認 helper 直接否決該 URL
    assert (
        sync_service._is_allowed_sync_url("http://169.254.169.254/manage/form.php?id=1")
        is False
    )


def test_parse_backend_list_row_keeps_same_domain_detail_link():
    """同網域的相對連結仍應正常組出 detail_url。"""
    row_html = """
    <tr>
        <td></td>
        <td>預約正常</td>
        <td>2026-04-15</td>
        <td>范瑀玹</td>
        <td>2024-02-20</td>
        <td>0919766932</td>
        <td>親友介紹</td>
        <td>2026-04-12 19:59:45</td>
        <td><a href="form.php?id=177">編輯</a></td>
    </tr>
    """

    record = sync_service._parse_backend_list_row(row_html, _PAGE_URL)

    assert record is not None
    assert record.detail_url == (
        "https://www.ivykids.tw/manage/make_an_appointment/form.php?id=177"
    )


def test_discover_next_pages_filters_offdomain_links():
    """next-page 連結指向外網/內網時必須被丟棄，不入抓取 queue。"""
    page_html = """
    <a href="index.php?page=2">下一頁</a>
    <a href="http://169.254.169.254/index.php?page=3">內網</a>
    <a href="https://evil.example.com/index.php?page=4">外網</a>
    """

    discovered = sync_service._discover_next_pages(page_html, _PAGE_URL)

    assert any("page=2" in url for url in discovered)
    assert all("169.254.169.254" not in url for url in discovered)
    assert all("evil.example.com" not in url for url in discovered)


def test_is_allowed_sync_url_blocks_private_and_offdomain():
    assert sync_service._is_allowed_sync_url(
        "https://www.ivykids.tw/manage/form.php?id=1"
    )
    assert not sync_service._is_allowed_sync_url("http://127.0.0.1/x")
    assert not sync_service._is_allowed_sync_url("http://10.0.0.5/x")
    assert not sync_service._is_allowed_sync_url("http://169.254.169.254/x")
    assert not sync_service._is_allowed_sync_url("https://attacker.example.com/x")
    assert not sync_service._is_allowed_sync_url("ftp://www.ivykids.tw/x")


# ── _run_sync 整合測試（SQLite + 真實 ORM）─────────────────────────────────


@pytest.fixture
def sqlite_session():
    """提供 in-memory SQLite session（已建好 recruitment_ivykids_records / recruitment_sync_state 表）。

    回傳 (engine, session, session_factory)：session_factory 用來讓
    `_try_acquire_sync_lock` 在獨立 session 內也指向同一個 SQLite。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from models.database import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    try:
        yield engine, session, SessionFactory
    finally:
        session.close()


def _make_backend_record(external_id: str, *, child_name: str = "測試小孩"):
    """生產 _run_sync 期待的 IvykidsBackendRecord。"""
    return sync_service.IvykidsBackendRecord(
        external_id=external_id,
        status="預約正常",
        visit_date="2026-04-15",
        child_name=child_name,
        phone="0900000000",
        source="網路搜尋",
        created_at="2099-01-01 00:00:00",  # 確保通過 cutoff
        detail_url=None,
        month="115.04",
    )


def _install_run_sync_mocks(monkeypatch, records, session_factory=None):
    """把 _run_sync 內呼叫的外部資源全部 stub 掉。

    session_factory：若有提供，會把 sync_service.session_scope 指向同一個 SQLite
    SessionFactory，讓 _try_acquire_sync_lock 在獨立 session 內也用測試 DB。
    """
    monkeypatch.setattr(sync_service, "sync_configured", lambda: True)
    monkeypatch.setattr(sync_service, "scheduler_configured", lambda: False)
    monkeypatch.setattr(sync_service, "scheduler_requested", lambda: False)
    monkeypatch.setattr(sync_service, "get_sync_interval_minutes", lambda: 10)
    monkeypatch.setattr(sync_service, "get_sync_created_at_cutoff", lambda: None)

    if session_factory is not None:

        @contextmanager
        def fake_session_scope():
            s = session_factory()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        monkeypatch.setattr(sync_service, "session_scope", fake_session_scope)

    @contextmanager
    def fake_build_requests_session():
        yield None

    monkeypatch.setattr(
        sync_service, "_build_requests_session", fake_build_requests_session
    )
    monkeypatch.setattr(sync_service, "_login_session", lambda _s: None)
    monkeypatch.setattr(
        sync_service,
        "fetch_backend_records",
        lambda **kw: (list(records), 1),
    )
    monkeypatch.setattr(
        sync_service,
        "enrich_backend_records",
        lambda records, **kw: records,
    )


class TestCrossWorkerLock:
    """跨 worker lock：用 recruitment_sync_state 表原子 UPDATE 取代 threading.Lock，
    多 worker 部署時仍能正確序列化同步。"""

    def _make_session_factory(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from models.database import Base

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,  # 多 session 共用同一個 in-memory connection
        )
        Base.metadata.create_all(engine)
        return engine, sessionmaker(bind=engine)

    def test_first_acquire_succeeds_second_blocks(self, monkeypatch):
        """同一筆 sync_state，第一次 acquire True、第二次 False。"""
        from contextlib import contextmanager
        from services import recruitment_ivykids_sync as sync_service

        _engine, SessionFactory = self._make_session_factory()

        @contextmanager
        def fake_scope():
            s = SessionFactory()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        monkeypatch.setattr(sync_service, "session_scope", fake_scope)

        # 第一次：應取得
        assert sync_service._try_acquire_sync_lock() is True
        # 第二次：應被擋（前一次未釋放）
        assert sync_service._try_acquire_sync_lock() is False

    def test_release_allows_reacquire(self, monkeypatch):
        from contextlib import contextmanager
        from services import recruitment_ivykids_sync as sync_service

        _engine, SessionFactory = self._make_session_factory()

        @contextmanager
        def fake_scope():
            s = SessionFactory()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        monkeypatch.setattr(sync_service, "session_scope", fake_scope)

        assert sync_service._try_acquire_sync_lock() is True
        sync_service._release_sync_lock()
        assert sync_service._try_acquire_sync_lock() is True

    def test_stale_lock_can_be_acquired(self, monkeypatch):
        """卡死的 worker 留 stale True：超過 stale timeout 後另一個 worker 可搶過。"""
        from contextlib import contextmanager
        from datetime import datetime, timedelta
        from services import recruitment_ivykids_sync as sync_service
        from models.recruitment import RecruitmentSyncState

        _engine, SessionFactory = self._make_session_factory()

        @contextmanager
        def fake_scope():
            s = SessionFactory()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        monkeypatch.setattr(sync_service, "session_scope", fake_scope)

        assert sync_service._try_acquire_sync_lock() is True

        # 手動把 last_started_at 設成 stale_minutes 之前
        stale_ago = datetime.now() - timedelta(
            minutes=sync_service.SYNC_LOCK_STALE_MINUTES + 1
        )
        with fake_scope() as s:
            row = (
                s.query(RecruitmentSyncState)
                .filter(
                    RecruitmentSyncState.provider_name
                    == sync_service.IVYKIDS_BACKEND_SOURCE
                )
                .one()
            )
            row.last_started_at = stale_ago

        # stale 後可搶
        assert sync_service._try_acquire_sync_lock() is True


class TestRunSyncNoNPlus1:
    """_run_sync 對 RecruitmentIvykidsRecord 的 existing 查詢必須批次化（in_()），
    而非每筆一次 SELECT（N+1）。"""

    def test_existing_lookup_is_batched(self, monkeypatch, sqlite_session):
        from sqlalchemy import event

        engine, session, session_factory = sqlite_session
        records = [
            _make_backend_record(str(i), child_name=f"小孩{i}") for i in range(20)
        ]
        _install_run_sync_mocks(monkeypatch, records, session_factory=session_factory)

        select_record_count = 0

        @event.listens_for(engine, "before_cursor_execute")
        def _count(_conn, _cursor, statement, *_):
            nonlocal select_record_count
            stmt_lower = statement.lower()
            if (
                "from recruitment_ivykids_records" in stmt_lower
                and stmt_lower.lstrip().startswith("select")
            ):
                select_record_count += 1

        result = sync_service._run_sync(session, max_pages=1, trigger="test")

        assert result["sync_success"] is True
        assert result["inserted"] == 20
        # 修前：20 筆 records ⇒ 20 次「SELECT ... FROM recruitment_ivykids_records」
        #       （另外還有 _prune_records_before_cutoff 一次，但 cutoff=None 時不會跑）
        # 修後：1 次批次 in_()（或 0 次，若選擇 dict 預載）
        assert select_record_count <= 2, (
            f"N+1 未修：對 recruitment_ivykids_records 執行了 {select_record_count} 次 SELECT，"
            "預期 ≤ 2 次（批次 in_() lookup）"
        )

    def test_idempotent_second_run_updates(self, monkeypatch, sqlite_session):
        """同樣 records 跑兩次：第二次應該全部走 update path，inserted=0、updated=20。"""
        _engine, session, session_factory = sqlite_session
        records = [
            _make_backend_record(str(i), child_name=f"小孩{i}") for i in range(20)
        ]
        _install_run_sync_mocks(monkeypatch, records, session_factory=session_factory)

        first = sync_service._run_sync(session, max_pages=1, trigger="test")
        assert first["inserted"] == 20
        assert first["updated"] == 0

        second = sync_service._run_sync(session, max_pages=1, trigger="test")
        assert second["inserted"] == 0
        assert second["updated"] == 20
