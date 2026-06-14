"""PostgreSQL-native 回歸測試：守住 SQLite 測試套件照不到的 dialect 分歧路徑。

背景（系統設計審查 2026-06-14, 紅隊補的高影響漏項 #13）：
    prod 是 PostgreSQL。CI 主 job 已對 postgres:15 跑整套 `pytest tests/`，但：
      1. conftest.py import 時無條件套 SQLite 相容 monkeypatch（JSONB→JSON、
         BigInteger→Integer），即使在 PG 上跑也被套用 → 部分 PG 原生型別行為
         在 CI 仍被降級。
      2. `test_db_session` fixture 明確建 SQLite，那批測試永遠跑 SQLite。
      3. 有些函式對 dialect 顯式分流（如 list_active_user_ids_with_permission
         的 sqlite vs pg 兩分支），SQLite 測試只會走 sqlite 分支。

    本檔針對「只有真 PG 才能驗」的路徑寫回歸測試，**不依賴** conftest 的全域
    engine swap——自己建一個真 PG engine（連 PG_TEST_URL），確保走 PG 分支。

執行：
    需設環境變數 PG_TEST_URL 指向一個【可寫的測試用】PostgreSQL（**勿用 dev/prod**）：
        PG_TEST_URL=postgresql://user@localhost:5432/ivy_pg_dialect_test \\
            pytest tests/test_pg_dialect_regressions.py
    未設 PG_TEST_URL 時整檔 skip（本機 SQLite-only 跑與既有 CI 不受影響）。
    CI 在 postgres:15 service container 上設 PG_TEST_URL 即可納入覆蓋。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import sessionmaker

_PG_TEST_URL = os.environ.get("PG_TEST_URL")

pytestmark = pytest.mark.skipif(
    not _PG_TEST_URL or not _PG_TEST_URL.startswith("postgresql"),
    reason="未設 PG_TEST_URL（指向真 PostgreSQL）；本檔僅在真 PG 上有意義",
)


@pytest.fixture(scope="module")
def pg_engine():
    eng = create_engine(_PG_TEST_URL)
    from models.database import Base

    Base.metadata.create_all(eng)  # idempotent；可對既有 schema 重跑
    yield eng
    eng.dispose()


@pytest.fixture
def pg_session(pg_engine):
    """交易回滾式 session：本 test 的所有寫入在結束時 rollback，不持久化。

    如此即使 PG_TEST_URL 指向與主套件共享的 CI 測試 DB 也不會污染其他測試
    （標準 transactional-test 模式）。test 內以 flush 取得 PK、查詢可見，但不 commit。
    """
    conn = pg_engine.connect()
    trans = conn.begin()
    Session = sessionmaker(bind=conn)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        if trans.is_active:
            trans.rollback()
        conn.close()


def _User():
    from models.database import User

    return User


def _make_user(s, *, user_id_str: str, perms, is_active=True):
    from models.database import User

    u = User(
        username=user_id_str,
        password_hash="x",
        role="hr",
        is_active=is_active,
        permission_names=perms,
    )
    s.add(u)
    s.flush()  # 取得 PK、同交易內可見，但不 commit（交易結束 rollback）
    return u


def test_permission_names_is_real_array_on_pg(pg_engine):
    """前提驗證：permission_names 在真 PG 上是 ARRAY（非被 monkeypatch 降級的 JSON）。"""
    with pg_engine.connect() as conn:
        dtype = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='users' AND column_name='permission_names'"
            )
        ).scalar()
    assert dtype == "ARRAY", f"permission_names 應為 PG ARRAY，實得 {dtype}"


def test_list_active_user_ids_with_permission_works_on_pg(pg_session):
    """PG 分支回歸：list_active_user_ids_with_permission 在真 PG 不崩潰、結果正確。

    這條路徑在 SQLite 走 Python filter 分支，只有真 PG 會走
    _permission_names_contains 的 cast(... ARRAY).contains() 查詢——即 2026-06-06
    教師 portal 全 500 的 P0 修補點。SQLite 測試永遠照不到。
    """
    from utils.permissions import list_active_user_ids_with_permission

    u1 = _make_user(pg_session, user_id_str="has_perm", perms=["EMPLOYEES_READ", "X"])
    _make_user(pg_session, user_id_str="no_perm", perms=["OTHER"])
    _make_user(pg_session, user_id_str="null_perm", perms=None)
    _make_user(
        pg_session, user_id_str="inactive", perms=["EMPLOYEES_READ"], is_active=False
    )

    ids = list_active_user_ids_with_permission(pg_session, "EMPLOYEES_READ")

    # 僅顯式含 perm 且 is_active 的帳號（null/wildcard/inactive 不算）
    assert ids == [u1.id], f"預期僅 {u1.id}（顯式含 EMPLOYEES_READ 且在職），實得 {ids}"
    # 交易未被中止：能繼續查
    assert pg_session.query(_User()).count() == 4


def test_naive_contains_is_malformed_on_pg(pg_session):
    """非廢測試見證：naive User.permission_names.contains([perm]) 在真 PG 會炸。

    證明 _permission_names_contains 的 cast 修補確有必要——若有人「簡化」回 naive
    .contains()，這條會紅，提醒回歸。with_variant 只換 DDL 型別、不換 comparator。
    """
    from models.database import User

    _make_user(pg_session, user_id_str="probe", perms=["EMPLOYEES_READ"])
    with pytest.raises((DBAPIError, ProgrammingError)):
        pg_session.query(User.id).filter(
            User.permission_names.contains(["EMPLOYEES_READ"])
        ).all()
    pg_session.rollback()
