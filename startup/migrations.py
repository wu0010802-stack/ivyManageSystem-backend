"""
startup/migrations.py — Alembic migration + 資料遷移函式
"""

import logging
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect as sa_inspect

from models.base import Base
from models.database import get_engine, get_session

logger = logging.getLogger(__name__)

ALEMBIC_BASELINE_REVISION = "4ddf3ebad3e8"


def _detect_alembic_state() -> str:
    """偵測 DB 對 alembic 的初始化狀態，回傳三態之一：

    - ``empty``         全新空 DB（沒有 user table 也沒有 alembic_version）
    - ``needs_baseline`` 既有 schema 但無 alembic_version（舊部署首次接上 alembic）
    - ``versioned``     已版控（alembic_version 存在）

    Why: baseline migration `4ddf3ebad3e8` 內容皆為 ``op.alter_column`` 修改既有
    table 的 comment/nullable，**沒有** CREATE TABLE。在「empty」狀態直接
    ``alembic upgrade heads`` 會撞到 ``UndefinedTable: relation "allowance_types"
    does not exist``。三態必須分流：empty → ORM ``create_all`` + ``stamp heads``；
    needs_baseline → ``stamp baseline`` + ``upgrade heads``；versioned → ``upgrade heads``。
    """
    inspector = sa_inspect(get_engine())
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return "versioned"
    user_tables = tables - {"alembic_version"}
    if not user_tables:
        return "empty"
    return "needs_baseline"


def needs_alembic_baseline_stamp() -> bool:
    """舊部署若已有 schema 但未建立 alembic_version，先 stamp baseline。

    保留為 ``_detect_alembic_state`` 的 thin wrapper 以維持向後相容；新程式碼
    直接呼叫 ``_detect_alembic_state`` 取得完整三態判斷。
    """
    return _detect_alembic_state() == "needs_baseline"


def run_alembic_upgrade():
    """執行 Alembic schema bootstrap + migration（三態分流，見 ``_detect_alembic_state``）。"""
    backend_root = Path(__file__).resolve().parent.parent

    # 一律用「app 自己的直譯器」跑 migration（sys.executable -m alembic），不可用
    # shutil.which("alembic") 抓 PATH 上第一個 alembic。後者可能是系統或別的 Python
    # 版本（例如 framework 3.14）的 alembic，與 app venv（3.13）的 site-packages 不一致，
    # env.py import models / 連線時行為不可預期，且常見「PATH 抓錯 alembic」導致啟動失敗。
    # -m alembic 保證 migration 與 runtime 同一環境，prod / 多 venv 皆可靠自動執行。
    base_cmd = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(backend_root / "alembic.ini"),
    ]

    def _run(args: list[str], label: str) -> None:
        """執行 alembic 子指令並在失敗時把 stdout/stderr 完整吐到日誌。

        為何不直接 inherit parent stdio：Zeabur 等容器平台對子程序輸出的緩衝
        順序常與 Python traceback 錯置，導致使用者看不到真正的錯誤訊息。改抓
        進來再 print，可確保失敗時錯誤訊息與 traceback 同位置出現。
        """
        proc = subprocess.run(
            args,
            cwd=backend_root,
            capture_output=True,
            text=True,
        )
        if proc.stdout:
            sys.stdout.write(proc.stdout)
            sys.stdout.flush()
        if proc.stderr:
            sys.stderr.write(proc.stderr)
            sys.stderr.flush()
        if proc.returncode != 0:
            raise RuntimeError(
                f"alembic {label} 失敗（exit={proc.returncode}）。"
                f"指令：{' '.join(args)}"
            )

    state = _detect_alembic_state()
    if state == "empty":
        logger.info(
            "偵測到全新空 DB，先以 ORM Base.metadata.create_all 建立完整 schema，"
            "再 alembic stamp heads 標已 fully migrated（不執行 baseline migration）"
        )
        Base.metadata.create_all(get_engine())
        _run([*base_cmd, "stamp", "heads"], "stamp heads")
    elif state == "needs_baseline":
        logger.info(
            "偵測到既有 schema 但沒有 alembic_version，先 stamp baseline=%s",
            ALEMBIC_BASELINE_REVISION,
        )
        _run([*base_cmd, "stamp", ALEMBIC_BASELINE_REVISION], "stamp baseline")
        _run([*base_cmd, "upgrade", "heads"], "upgrade heads")
    else:
        _run([*base_cmd, "upgrade", "heads"], "upgrade heads")


def migrate_school_year_to_roc():
    """將 school_year / period 從西元年遷移為民國年（幂等，只處理 > 1911 的值）。"""
    from sqlalchemy import text
    from models.classroom import Classroom

    session = get_session()
    try:
        count = (
            session.query(Classroom)
            .filter(Classroom.school_year > 1911)
            .update(
                {"school_year": Classroom.school_year - 1911}, synchronize_session=False
            )
        )

        # period 欄位所在的費用表。逐表前先確認存在：缺表（歷史改名/移除，例如
        # 已不存在的 fee_items）若直接 UPDATE 會拋 UndefinedTable，連帶 rollback
        # 整個交易（含上面的 Classroom 更新），使 ROC 轉換在啟動時靜默失效。
        existing_tables = set(sa_inspect(session.get_bind()).get_table_names())
        for table_name in ("student_fee_records",):
            if table_name not in existing_tables:
                logger.info(
                    "migrate_school_year_to_roc：表 %s 不存在，略過 period 轉換",
                    table_name,
                )
                continue
            session.execute(
                text(
                    f"UPDATE {table_name} "
                    f"SET period = CAST(CAST(SUBSTRING(period FROM 1 FOR 4) AS INTEGER) - 1911 AS TEXT) "
                    f"  || SUBSTRING(period FROM 5) "
                    f"WHERE period ~ '^[0-9]{{4}}-' "
                    f"  AND CAST(SUBSTRING(period FROM 1 FOR 4) AS INTEGER) > 1911"
                )
            )

        session.commit()
        if count:
            logger.info("migrate_school_year_to_roc：已遷移 %d 筆班級學年度", count)
    except Exception:
        session.rollback()
        logger.exception("migrate_school_year_to_roc 失敗")
    finally:
        session.close()
