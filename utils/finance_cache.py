"""utils/finance_cache.py — finance-summary 快取失效統一入口。

money write path 結束後呼叫，讓 `/reports/finance-summary`（TTL 30 分）
下次請求重算。fees / salary / activity 三個模組原本各維護一份相同實作，
本檔將其合 1。

呼叫慣例：write 路徑成功 commit 後再呼叫；失敗會 log warning 但不拋例外，
不阻擋主交易。
"""

import logging

logger = logging.getLogger(__name__)


FINANCE_SUMMARY_CACHE_CATEGORY = "reports_finance_summary"


def invalidate_finance_summary_cache() -> None:
    """失效 finance-summary 快取（呼叫端通常為 fees/salary/activity write 路徑）。"""
    try:
        # lazy import 避免 module-level import 與 services 形成 cycle
        from services.report_cache_service import report_cache_service

        report_cache_service.invalidate_category(None, FINANCE_SUMMARY_CACHE_CATEGORY)
    except Exception:
        logger.warning("invalidate finance_summary cache failed", exc_info=True)
