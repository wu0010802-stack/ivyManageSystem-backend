"""對標稽核 #1：讓 per-migration roundtrip 測試在真 PG 上跑（補 SQLite 盲區）。

背景：CI 的 ``test`` 與 ``alembic-roundtrip`` job 都設了 ``DATABASE_URL`` 指向 Postgres，
但歷史上 per-migration 測試（``test_migration_*.py``）硬編 ``sqlite:///`` 忽略該 PG →
migration 內的 PG-only backfill SQL / DDL（partial index、ARRAY、dialect-specific cast、
``op.execute`` 等）從未在真 PG 跑過，prod-only 失敗在 CI 照不到（記憶
``feedback_sqlite_test_blindspot_pg_array_contains``）。

此 helper：``DATABASE_URL`` 指向 Postgres 時，在「唯一隔離 schema」裡 create_all + 跑
migration（不污染共用 test DB 的 public schema，teardown ``DROP SCHEMA CASCADE``）；否則
fallback tmp sqlite（本地無 PG 仍可跑）。pytest-split 單進程序列、各 matrix 分片有獨立 PG
service，故固定 schema 名安全。

用法（把既有 ``db`` fixture 的 engine 來源換掉，其餘不動）：

    engine, cleanup = make_migration_engine(tmp_path, schema="mig_<name>")
    ...
    cleanup()
"""

from __future__ import annotations

import os
from collections.abc import Callable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "") or ""


def is_postgres() -> bool:
    """目前 DATABASE_URL 是否指向 Postgres（決定 migration 測試走真 PG 或 sqlite）。"""
    return _database_url().startswith("postgresql")


def make_migration_engine(tmp_path, schema: str) -> tuple[Engine, Callable[[], None]]:
    """回傳 ``(engine, cleanup)``。

    - Postgres（``DATABASE_URL`` 指向 PG）：建唯一 schema ``schema``，engine 連線以
      ``search_path`` 綁定該 schema，供 ``create_all`` + migration ``upgrade()`` 在真 PG
      隔離執行；``cleanup`` 會 ``DROP SCHEMA CASCADE``（含其 enum type / index）。
    - 否則：tmp sqlite（既有行為）。
    """
    url = _database_url()
    if url.startswith("postgresql"):
        # 先用預設連線建/清隔離 schema（避免污染共用 test DB 的 public schema）
        admin = create_engine(url, future=True)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        admin.dispose()

        # 工作 engine：所有連線 search_path 綁該 schema → create_all 與 migration 的
        # 未限定物件都落在隔離 schema。
        engine = create_engine(
            url,
            future=True,
            connect_args={"options": f"-csearch_path={schema}"},
        )

        def cleanup() -> None:
            engine.dispose()
            admin2 = create_engine(url, future=True)
            with admin2.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            admin2.dispose()

        return engine, cleanup

    db_path = tmp_path / "mig.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    return engine, engine.dispose


def run_migration(engine, migration, direction: str) -> None:
    """以**真 alembic ``Operations``/``MigrationContext``** 跑 migration 的 upgrade/downgrade，
    近似 alembic 實際 runner——支援 ``op.get_context().dialect.name`` 回報真實 dialect
    （讓 dialect-aware migration 走對分支）、``autocommit_block()`` 與 ``CREATE INDEX
    CONCURRENTLY``。比 per-test 手寫 ``op`` stub 更貼近 prod 行為。

    用法：``run_migration(engine, _load_migration(), "upgrade")``。每次呼叫開新 connection +
    ``ctx.begin_transaction()``，commit 後下一次（如 downgrade）以新 connection 見已 commit 狀態。
    """
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    conn = engine.connect()
    try:
        ctx = MigrationContext.configure(conn)
        op_obj = Operations(ctx)
        old_op = getattr(migration, "op", None)
        migration.op = op_obj
        try:
            with ctx.begin_transaction():
                getattr(migration, direction)()
        finally:
            migration.op = old_op
    finally:
        conn.close()
