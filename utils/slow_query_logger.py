"""
utils/slow_query_logger.py — SQLAlchemy 慢查詢監控

透過 SQLAlchemy Core event listener 記錄超過閾值的 SQL 查詢，
幫助識別效能瓶頸。

使用方式：
    from utils.slow_query_logger import install_slow_query_logger
    install_slow_query_logger(engine)
"""

import logging
import time

from sqlalchemy import event

logger = logging.getLogger("slow_query")

SLOW_QUERY_THRESHOLD_MS = 500  # 超過 500ms 記錄為慢查詢


def install_slow_query_logger(engine, threshold_ms: int = SLOW_QUERY_THRESHOLD_MS):
    """為 SQLAlchemy Engine 安裝慢查詢監控。

    在 before_cursor_execute 記錄開始時間，
    在 after_cursor_execute 計算耗時並記錄慢查詢。
    """

    @event.listens_for(engine, "before_cursor_execute")
    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        conn.info.setdefault("query_start_time", []).append(time.monotonic())

    @event.listens_for(engine, "after_cursor_execute")
    def _after_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        start_times = conn.info.get("query_start_time")
        if not start_times:
            return
        start = start_times.pop()
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)

        if elapsed_ms > threshold_ms:
            # 截取前 200 字元避免日誌過長
            stmt_short = statement[:200].replace("\n", " ")
            logger.warning(
                "SLOW QUERY (%.1fms): %s",
                elapsed_ms,
                stmt_short,
            )

    logger.info(
        "慢查詢監控已啟用（閾值: %dms）",
        threshold_ms,
    )
