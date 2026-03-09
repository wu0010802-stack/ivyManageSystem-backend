import sys
import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# ── 路徑設定：讓 alembic 能 import backend/ 下的模組 ────────────────────────
# 執行目錄應為 backend/（見 README），此行確保跨工作目錄皆可運作
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── 匯入所有 models 使其向 Base.metadata 登記 ───────────────────────────────
# models.database 是 re-export hub，import 一行即可載入所有 Table 定義
import models.database  # noqa: F401（side-effect import）
from models.base import Base, DATABASE_URL

# ── Alembic 設定物件 ─────────────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 比對目標：我們的 SQLAlchemy metadata
target_metadata = Base.metadata


# ── Offline mode（產生 SQL 腳本，不需實際 DB 連線）────────────────────────────
def run_migrations_offline() -> None:
    """輸出 SQL 腳本至 stdout，不建立 DB 連線。
    適用於：alembic upgrade head --sql > migrate.sql
    """
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,       # 偵測欄位型別變更
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online mode（直接連 DB 執行）────────────────────────────────────────────
def run_migrations_online() -> None:
    """直接對資料庫執行 migration。
    URL 從環境變數 DATABASE_URL 取得（見 models/base.py）。
    """
    connectable = engine_from_config(
        {"sqlalchemy.url": DATABASE_URL},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # migration 用完即釋放，不需連線池
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
