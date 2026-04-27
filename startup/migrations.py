"""
startup/migrations.py — Alembic migration + 資料遷移函式
"""

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect as sa_inspect

from models.database import get_engine, get_session, User
from utils.permissions import _RW_PAIRS

logger = logging.getLogger(__name__)

ALEMBIC_BASELINE_REVISION = "4ddf3ebad3e8"


def needs_alembic_baseline_stamp():
    """舊部署若已有 schema 但未建立 alembic_version，先 stamp baseline。"""
    inspector = sa_inspect(get_engine())
    tables = set(inspector.get_table_names())
    if "alembic_version" in tables:
        return False

    user_tables = tables - {"alembic_version"}
    return bool(user_tables)


def run_alembic_upgrade():
    """執行 Alembic schema migration。"""
    backend_root = Path(__file__).resolve().parent.parent
    alembic_bin = shutil.which("alembic")
    if not alembic_bin:
        bundled_alembic = backend_root / "venv_sec" / "bin" / "alembic"
        if bundled_alembic.exists():
            alembic_bin = str(bundled_alembic)
    if not alembic_bin:
        raise RuntimeError(
            "找不到 alembic 可執行檔，請先安裝 backend/requirements.txt 或啟用正確虛擬環境。"
        )

    base_cmd = [
        alembic_bin,
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

    if needs_alembic_baseline_stamp():
        logger.info(
            "偵測到既有 schema 但沒有 alembic_version，先 stamp baseline=%s",
            ALEMBIC_BASELINE_REVISION,
        )
        _run([*base_cmd, "stamp", ALEMBIC_BASELINE_REVISION], "stamp")

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

        for table_name in ("fee_items", "student_fee_records"):
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


def migrate_permissions_rw():
    """為既有非全權用戶自動補上 _WRITE 位元（冪等）"""
    session = get_session()
    try:
        users = session.query(User).filter(User.permissions != -1).all()
        updated = 0
        for user in users:
            old = user.permissions
            new = old
            for read_bit, write_bit in _RW_PAIRS:
                if (old & read_bit.value) == read_bit.value:
                    new = new | write_bit.value
            if new != old:
                user.permissions = new
                updated += 1
        if updated:
            session.commit()
            logger.info(
                f"migrate_permissions_rw: 已更新 {updated} 位用戶的 WRITE 權限位元"
            )
        else:
            logger.info("migrate_permissions_rw: 無需遷移（所有用戶已是最新）")
    finally:
        session.close()
