"""
models/base.py — Base、engine、session 管理
"""

import os
import logging
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# 載入 .env（backend/.env）
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

logger = logging.getLogger(__name__)

Base = declarative_base()

# ---------------------------------------------------------------------------
# 資料庫連線管理
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_is_dev = os.environ.get("ENV", "development").lower() in (
    "development",
    "dev",
    "local",
)

if not DATABASE_URL:
    if _is_dev:
        DATABASE_URL = "postgresql://localhost:5432/ivymanagement"
        logger.warning("DATABASE_URL 未設定，使用本機開發預設值。")
    else:
        raise RuntimeError("DATABASE_URL 環境變數未設定，正式環境不允許啟動。")

_engine = None
_SessionFactory = None


def _is_remote_db(url: str) -> bool:
    """判斷是否為遠端資料庫（Supabase 等）"""
    return "supabase" in url or "neon" in url or "render" in url


def get_engine():
    """取得全域 Engine（含連線池），只建立一次"""
    global _engine
    if _engine is None:
        if DATABASE_URL.startswith("sqlite"):
            _engine = create_engine(
                DATABASE_URL,
                connect_args={"check_same_thread": False},
                echo=False,
            )
        else:
            connect_args: dict = {}
            if _is_remote_db(DATABASE_URL):
                connect_args["sslmode"] = "require"
            connect_args["options"] = "-c statement_timeout=30000"
            kwargs = dict(
                pool_size=20,
                max_overflow=40,
                pool_pre_ping=True,
                pool_recycle=1800,  # 30 分鐘回收連線，避免 server 端斷線
                pool_timeout=15,
                echo=False,
                connect_args=connect_args,
            )
            _engine = create_engine(DATABASE_URL, **kwargs)
    return _engine


def get_session_factory():
    """取得 SessionFactory，只建立一次"""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory


def get_session():
    """取得資料庫 session（向下相容）"""
    return get_session_factory()()


@contextmanager
def session_scope():
    """提供 context manager 風格的 session 管理，自動 commit/rollback/close"""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database():
    """初始化資料庫連線與 session factory，不執行 schema 變更。"""
    engine = get_engine()
    logger.info("資料庫初始化完成")
    return engine, get_session_factory()
