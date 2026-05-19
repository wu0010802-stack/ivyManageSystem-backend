"""utils/finance_cache.py — finance-summary 快取失效統一入口。

money write path 結束後呼叫，讓 `/reports/finance-summary` 與
`/reports/monthly-pnl`（兩者都 TTL 30 分）下次請求重算。fees / salary /
activity 三個模組原本各維護一份相同實作，本檔將其合 1。

呼叫慣例：write 路徑成功 commit 後再呼叫；失敗會 log warning 但不拋例外，
不阻擋主交易。
"""

import logging

logger = logging.getLogger(__name__)


FINANCE_SUMMARY_CACHE_CATEGORY = "reports_finance_summary"
MONTHLY_PNL_CACHE_CATEGORY = "reports_monthly_pnl"


def invalidate_finance_summary_cache() -> None:
    """失效 finance-summary 與 monthly-pnl 快取。

    兩快取吃同一份金流寫入（學費繳費 / 才藝 POS / 薪資結算 / 廠商付款），
    因此這裡同步失效；少寫一個 invalidate 點就會在月度損益表上看到舊數字。
    """
    # lazy import 避免 module-level import 與 services 形成 cycle
    try:
        from services.report_cache_service import report_cache_service
    except Exception:
        logger.warning("import report_cache_service failed", exc_info=True)
        return

    for category in (FINANCE_SUMMARY_CACHE_CATEGORY, MONTHLY_PNL_CACHE_CATEGORY):
        try:
            report_cache_service.invalidate_category(None, category)
        except Exception:
            logger.warning("invalidate %s cache failed", category, exc_info=True)
