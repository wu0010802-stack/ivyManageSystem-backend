"""
models/base.py — Base, engine, session 管理、資料庫遷移邏輯
"""

import os
import logging
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect as sa_inspect, text
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
_is_dev = os.environ.get("ENV", "development").lower() in ("development", "dev", "local")

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
            kwargs = dict(
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
                echo=False,
            )
            if _is_remote_db(DATABASE_URL):
                kwargs["connect_args"] = {"sslmode": "require"}
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


def _add_column_if_missing(engine, inspector, table: str, column: str, col_def: str):
    """若欄位不存在則執行 ALTER TABLE ADD COLUMN"""
    existing = [c["name"] for c in inspector.get_columns(table)]
    if column not in existing:
        with engine.connect() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"))
            conn.commit()
        logger.info("Migration: 已新增 %s.%s 欄位", table, column)


def _run_migrations(engine):
    """執行資料庫結構遷移（向後相容，安全重複執行）

    ⚠️  過渡說明（2026-03-07 起引入 Alembic）：
    - 新的 schema 變更請改用 Alembic：
        alembic revision --autogenerate -m "描述"
        alembic upgrade head
    - 此函式保留以相容尚未執行過 Alembic 的舊部署（冪等，可安全重複呼叫）
    - 待所有部署環境皆更新後，可逐步移除此函式的各個 ALTER TABLE 段落
    """
    inspector = sa_inspect(engine)

    # ── leave_records ──────────────────────────────────────────────────────────
    existing_cols = [c["name"] for c in inspector.get_columns("leave_records")]
    if "attachment_paths" not in existing_cols:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE leave_records ADD COLUMN attachment_paths TEXT"))
            conn.commit()
        logger.info("Migration: 已新增 leave_records.attachment_paths 欄位")
    if "start_time" not in existing_cols:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE leave_records ADD COLUMN start_time VARCHAR(5)"))
            conn.execute(text("ALTER TABLE leave_records ADD COLUMN end_time VARCHAR(5)"))
            conn.commit()
        logger.info("Migration: 已新增 leave_records.start_time 與 end_time 欄位")
    if "rejection_reason" not in existing_cols:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE leave_records ADD COLUMN rejection_reason TEXT"))
            conn.commit()
        logger.info("Migration: 已新增 leave_records.rejection_reason 欄位")

    # ── 設定版本控制（Config Versioning）──────────────────────────────────────
    # bonus_configs
    _add_column_if_missing(engine, inspector, "bonus_configs", "version", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(engine, inspector, "bonus_configs", "changed_by", "VARCHAR(50)")

    # attendance_policies
    _add_column_if_missing(engine, inspector, "attendance_policies", "version", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(engine, inspector, "attendance_policies", "changed_by", "VARCHAR(50)")

    # insurance_rates
    _add_column_if_missing(engine, inspector, "insurance_rates", "version", "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(engine, inspector, "insurance_rates", "changed_by", "VARCHAR(50)")

    # grade_targets — 關聯到 bonus_config 版本
    _add_column_if_missing(engine, inspector, "grade_targets", "bonus_config_id", "INTEGER REFERENCES bonus_configs(id)")

    # salary_records — 記錄計算時使用的設定版本
    _add_column_if_missing(engine, inspector, "salary_records", "bonus_config_id", "INTEGER REFERENCES bonus_configs(id)")
    _add_column_if_missing(engine, inspector, "salary_records", "attendance_policy_id", "INTEGER REFERENCES attendance_policies(id)")
    # salary_records — 曠職扣款
    _add_column_if_missing(engine, inspector, "salary_records", "absence_deduction", "FLOAT DEFAULT 0")
    _add_column_if_missing(engine, inspector, "salary_records", "absent_count", "INTEGER DEFAULT 0")
    # salary_records — 主管紅利獨立欄位（拆分自 bonus_amount）
    _add_column_if_missing(engine, inspector, "salary_records", "supervisor_dividend", "FLOAT DEFAULT 0")

    # employees — 節慶獎金等級覆蓋
    _add_column_if_missing(engine, inspector, "employees", "bonus_grade", "CHAR(1)")
    # employees — 勞退自提比例
    _add_column_if_missing(engine, inspector, "employees", "pension_self_rate", "FLOAT DEFAULT 0")
    # employees — 離職日期
    _add_column_if_missing(engine, inspector, "employees", "resign_date", "DATE")
    # employees — 離職原因、試用期結束日
    _add_column_if_missing(engine, inspector, "employees", "resign_reason", "VARCHAR(200)")
    _add_column_if_missing(engine, inspector, "employees", "probation_end_date", "DATE")

    # users — 強制修改密碼旗標
    _add_column_if_missing(engine, inspector, "users", "must_change_password", "BOOLEAN NOT NULL DEFAULT FALSE")
    # users — Token 版本號（用於即時撤銷：帳號停用或權限變更時 +1，使所有現有 token 無法換發）
    _add_column_if_missing(engine, inspector, "users", "token_version", "INTEGER NOT NULL DEFAULT 0")

    # users — employee_id 改為允許 NULL（純管理帳號用，不關聯員工記錄）
    user_cols = {c["name"]: c for c in inspector.get_columns("users")}
    if not user_cols.get("employee_id", {}).get("nullable", True):
        try:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE users ALTER COLUMN employee_id DROP NOT NULL"))
                conn.commit()
            logger.info("Migration: users.employee_id 已改為允許 NULL")
        except Exception as e:
            logger.warning("Migration: 無法修改 users.employee_id nullable 約束: %s", e)

    # overtime_records — 補休欄位
    _add_column_if_missing(engine, inspector, "overtime_records", "use_comp_leave",      "BOOLEAN NOT NULL DEFAULT FALSE")
    _add_column_if_missing(engine, inspector, "overtime_records", "comp_leave_granted",  "BOOLEAN NOT NULL DEFAULT FALSE")

    # attendances — 異常確認欄位
    _add_column_if_missing(engine, inspector, "attendances", "confirmed_action", "VARCHAR(20)")
    _add_column_if_missing(engine, inspector, "attendances", "confirmed_by",     "VARCHAR(100)")
    _add_column_if_missing(engine, inspector, "attendances", "confirmed_at",     "TIMESTAMP")

    # daily_shifts — shift_type_id 改為允許 NULL（換班至無班的情境需要顯式標記排休）
    ds_cols = {c["name"]: c for c in inspector.get_columns("daily_shifts")}
    if not ds_cols.get("shift_type_id", {}).get("nullable", True):
        try:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE daily_shifts ALTER COLUMN shift_type_id DROP NOT NULL"))
                conn.commit()
            logger.info("Migration: daily_shifts.shift_type_id 已改為允許 NULL")
        except Exception as e:
            logger.warning("Migration: daily_shifts.shift_type_id 允許 NULL 設定失敗（可忽略）：%s", e)


def init_database():
    """初始化資料庫並建立所有表格"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _run_migrations(engine)
    logger.info("資料庫初始化完成")
    return engine, get_session_factory()
