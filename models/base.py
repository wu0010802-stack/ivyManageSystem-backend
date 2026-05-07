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


def _env_int(name: str, default: int) -> int:
    """讀取 int 環境變數；無效或缺失時 fallback 到 default。"""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "環境變數 %s=%r 不是合法整數，回退使用預設 %s", name, raw, default
        )
        return default


# 連線池參數可由 env 覆寫（audit J.P0.1）。
# 預設值（5+5=10/pod）對 Supabase Session Mode 安全；單機開發或 Transaction
# Mode 部署可調高（例：DB_POOL_SIZE=10 DB_POOL_MAX_OVERFLOW=20）。
_DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 5)
_DB_POOL_MAX_OVERFLOW = _env_int("DB_POOL_MAX_OVERFLOW", 5)
_DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", 15)
_DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 1800)

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
            # Why: Supabase Session Mode pooler 硬上限 15 clients；即便切到
            # Transaction Mode（port 6543），單 pod 仍應節制連線以免多副本互搶。
            # 5 base + 5 overflow = 10/pod，3 副本約 30 條，遠低於 Transaction
            # Mode 的容量上限，也不會把 Session Mode 撐爆。
            kwargs = dict(
                pool_size=_DB_POOL_SIZE,
                max_overflow=_DB_POOL_MAX_OVERFLOW,
                pool_pre_ping=True,
                pool_recycle=_DB_POOL_RECYCLE,  # 30 分鐘回收連線，避免 server 端斷線
                pool_timeout=_DB_POOL_TIMEOUT,
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
