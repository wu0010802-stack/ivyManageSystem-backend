"""
utils/advisory_lock.py — PostgreSQL advisory lock 封裝

用途：在多 worker 部署下保護薪資計算等關鍵區段，確保同一 (員工 / 年 / 月)
不會被兩個 worker 同時計算導致資料競態。

實作要點：
- PostgreSQL：使用 `pg_advisory_xact_lock(key)`，鎖與當前 transaction 綁定，
  commit / rollback 時自動釋放，不需要手動 unlock，進程崩潰也不會留孤鎖。
- SQLite（單元測試）：靜默降級為 no-op。SQLite 為單寫入者，測試環境無併發。
- 非阻塞版本 `try_salary_lock()` 用 `pg_try_advisory_xact_lock`，
  若鎖被其他 worker 佔用則立即回傳 False；可用於「排程 job 跳過正在計算的員工」。

Lock key 設計：
- 所有 salary 類的鎖都走同一命名空間（前綴 int），避免與其他業務混鎖。
- Key 由 (employee_id, year, month) 以 md5 取前 63-bit 整數生成，
  實際碰撞率對單一學校的員工數（< 100）來說可忽略。

使用範例（單員工）：
    with session_scope() as s:
        with salary_lock(s, employee_id=42, year=2026, month=4):
            # 以下臨界區只有單一 worker 會進入
            recalc_employee_salary(s, 42, 2026, 4)
            s.commit()

使用範例（整月封存）：
    with session_scope() as s:
        with salary_lock(s, year=2026, month=4):  # employee_id=None → 整月鎖
            finalize_month(s, 2026, 4)
            s.commit()
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 各業務域的 lock namespace，避免不同業務 key 碰撞
_NAMESPACE_SALARY = 0x5341_4C00  # 'SAL\0'
_NAMESPACE_SALARY_MONTH = 0x5341_4C4D  # 'SALM'
_NAMESPACE_SCHEDULER = 0x5343_4844  # 'SCHD'


@contextmanager
def try_scheduler_lock(
    session: Session, *, scheduler_name: str, run_key: str
) -> Iterator[bool]:
    """非阻塞 advisory lock — 用於排程 job 的「同日同名」互斥。

    多 worker 部署時，每個 worker 自啟 scheduler；以本鎖確保某個
    `(scheduler_name, run_key)` 組合在當前 transaction 期間僅被一個 worker
    取得。caller 應在取得鎖後完成業務寫入並 commit；交易結束時鎖自動釋放。

    yield True  → 已取得鎖
    yield False → 已被其他 worker 持有，呼叫端應略過本次
    """
    if not _is_postgres(session):
        # SQLite 測試環境視為單寫入者，直接 yield True
        yield True
        return
    seed = f"scheduler|{scheduler_name}|{run_key}".encode()
    raw = hashlib.md5(seed).digest()
    key = int.from_bytes(raw[:8], "big", signed=False) & 0x7FFF_FFFF_FFFF_FFFF
    row = session.execute(
        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}
    ).scalar()
    acquired = bool(row)
    if not acquired:
        logger.info(
            "scheduler_lock busy, skipping: name=%s key=%s",
            scheduler_name,
            run_key,
        )
    yield acquired


def _is_postgres(session: Session) -> bool:
    return (session.bind is not None) and session.bind.dialect.name == "postgresql"


def _key_for_salary(employee_id: Optional[int], year: int, month: int) -> int:
    """把 (emp, year, month) 雜湊成 63-bit int 作為 advisory lock key。

    employee_id=None 表示整月鎖（finalize-month），key namespace 獨立。
    """
    if employee_id is None:
        seed = f"salary_month|{year}|{month}".encode()
        raw = hashlib.md5(seed).digest()
        # 前 63 bit，確保正 int64
        return int.from_bytes(raw[:8], "big", signed=False) & 0x7FFF_FFFF_FFFF_FFFF
    seed = f"salary_emp|{employee_id}|{year}|{month}".encode()
    raw = hashlib.md5(seed).digest()
    return int.from_bytes(raw[:8], "big", signed=False) & 0x7FFF_FFFF_FFFF_FFFF


@contextmanager
def salary_lock(
    session: Session,
    *,
    employee_id: Optional[int] = None,
    year: int,
    month: int,
) -> Iterator[None]:
    """阻塞式 advisory lock。若鎖被其他 worker 持有則等待直到取得。

    - PostgreSQL：`pg_advisory_xact_lock(key)`，交易結束自動釋放
    - 非 PostgreSQL（SQLite 測試）：no-op，直接進入臨界區
    """
    if not _is_postgres(session):
        # SQLite / 其他：降級為無鎖，記一行 debug
        logger.debug(
            "salary_lock no-op (non-postgres): emp=%s year=%s month=%s",
            employee_id,
            year,
            month,
        )
        yield
        return

    key = _key_for_salary(employee_id, year, month)
    try:
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    except OperationalError as exc:
        logger.error(
            "salary_lock acquire failed: emp=%s year=%s month=%s err=%s",
            employee_id,
            year,
            month,
            exc,
        )
        raise
    logger.debug(
        "salary_lock acquired: emp=%s year=%s month=%s key=%d",
        employee_id,
        year,
        month,
        key,
    )
    yield
    # 無需手動釋放，commit / rollback 時隨 transaction 結束


def acquire_salary_lock(
    session: Session,
    *,
    employee_id: Optional[int] = None,
    year: int,
    month: int,
) -> None:
    """直接於當前 transaction 取得 advisory lock（不需 with 包住臨界區）。

    PG 的 pg_advisory_xact_lock 在 transaction 結束時自動釋放，所以取得鎖後
    後續所有讀寫都受保護。這個 API 比 context manager 更符合語意，也方便
    FastAPI 路由中直接呼叫。
    """
    if not _is_postgres(session):
        logger.debug(
            "acquire_salary_lock no-op (non-postgres): emp=%s year=%s month=%s",
            employee_id,
            year,
            month,
        )
        return
    key = _key_for_salary(employee_id, year, month)
    try:
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    except OperationalError as exc:
        logger.error(
            "acquire_salary_lock failed: emp=%s year=%s month=%s err=%s",
            employee_id,
            year,
            month,
            exc,
        )
        raise
    logger.debug(
        "acquire_salary_lock: emp=%s year=%s month=%s key=%d",
        employee_id,
        year,
        month,
        key,
    )


@contextmanager
def try_salary_lock(
    session: Session,
    *,
    employee_id: Optional[int] = None,
    year: int,
    month: int,
) -> Iterator[bool]:
    """非阻塞 advisory lock。若鎖被佔用則 context yield False。

    用於 async job 批次計算：跳過正在被其他 worker 處理的員工，
    而不是排隊等待。

    yield True  → 已取得鎖，可執行臨界區
    yield False → 未取得鎖，呼叫端應跳過
    """
    if not _is_postgres(session):
        yield True
        return

    key = _key_for_salary(employee_id, year, month)
    row = session.execute(
        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}
    ).scalar()
    acquired = bool(row)
    if not acquired:
        logger.info(
            "salary_lock busy, skipping: emp=%s year=%s month=%s",
            employee_id,
            year,
            month,
        )
    yield acquired
