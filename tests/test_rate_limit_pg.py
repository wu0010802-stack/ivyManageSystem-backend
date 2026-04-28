"""tests/test_rate_limit_pg.py — LOW-1 PostgresLimiter 行為測試（SQLite 替代）

PG 與 SQLite 的 ON CONFLICT 語法相容；測試以 SQLite + 兩張安全支援表驗證：
- 同視窗內第 N+1 次呼叫拋 429
- 不同視窗起點獨立計數
- DB 失敗時 fail-open（不擋）
- GC 清除舊視窗
- create_limiter() factory 切換 backend
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "rl.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    yield engine

    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


def test_postgres_limiter_blocks_after_max_calls(db):
    from utils.rate_limit import PostgresLimiter

    limiter = PostgresLimiter(max_calls=3, window_seconds=60, name="test")
    # 前 3 次：放行
    for _ in range(3):
        limiter.check("user-A")
    # 第 4 次：429
    with pytest.raises(HTTPException) as ei:
        limiter.check("user-A")
    assert ei.value.status_code == 429


def test_postgres_limiter_isolates_keys(db):
    from utils.rate_limit import PostgresLimiter

    limiter = PostgresLimiter(max_calls=2, window_seconds=60, name="test")
    limiter.check("user-A")
    limiter.check("user-A")
    # user-B 不受 user-A 影響
    limiter.check("user-B")
    limiter.check("user-B")
    with pytest.raises(HTTPException):
        limiter.check("user-A")
    with pytest.raises(HTTPException):
        limiter.check("user-B")


def test_postgres_limiter_window_alignment(db):
    """同一秒內的請求落在同一個視窗；視窗切齊用 floor(now / window) * window。"""
    from utils.rate_limit import PostgresLimiter

    limiter = PostgresLimiter(max_calls=2, window_seconds=60, name="aligned")
    limiter.check("k1")
    limiter.check("k1")
    # DB 中應只有一個 row（同一視窗）
    with db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT bucket_key, count FROM rate_limit_buckets WHERE bucket_key = :k"
            ),
            {"k": "aligned:k1"},
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 2


def test_postgres_limiter_fails_open_on_db_error(monkeypatch, db):
    """DB 出錯時不應該擋住正常請求（避免 DB 抖動把全站打掛）。"""
    from utils.rate_limit import PostgresLimiter

    limiter = PostgresLimiter(max_calls=1, window_seconds=60, name="failopen")

    def bad_get_engine():
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr("models.base.get_engine", bad_get_engine)
    # 不應拋出
    limiter.check("user")
    limiter.check("user")
    limiter.check("user")


def test_cleanup_rate_limit_buckets(db):
    from utils.rate_limit import cleanup_rate_limit_buckets

    old_window = datetime.now(timezone.utc) - timedelta(hours=2)
    new_window = datetime.now(timezone.utc)

    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rate_limit_buckets (bucket_key, window_start, count) VALUES (:k, :w, 1)"
            ),
            [
                {"k": "old", "w": old_window},
                {"k": "new", "w": new_window},
            ],
        )

    deleted = cleanup_rate_limit_buckets(retention_minutes=60)
    assert deleted == 1
    with db.connect() as conn:
        remaining = conn.execute(
            text("SELECT bucket_key FROM rate_limit_buckets")
        ).fetchall()
    assert [r[0] for r in remaining] == ["new"]


def test_create_limiter_factory_respects_env(monkeypatch, db):
    from utils.rate_limit import (
        PostgresLimiter,
        SlidingWindowLimiter,
        create_limiter,
    )

    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    assert isinstance(
        create_limiter(max_calls=1, window_seconds=1, name="m"),
        SlidingWindowLimiter,
    )

    monkeypatch.setenv("RATE_LIMIT_BACKEND", "postgres")
    assert isinstance(
        create_limiter(max_calls=1, window_seconds=1, name="p"),
        PostgresLimiter,
    )

    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    assert isinstance(
        create_limiter(max_calls=1, window_seconds=1, name="d"),
        SlidingWindowLimiter,
    )
