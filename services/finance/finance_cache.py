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
REPORT_DASHBOARD_CACHE_CATEGORY = "reports_dashboard"
SALARY_CONTRIBUTORS_CACHE_CATEGORY = "reports_salary_contributors"


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


def invalidate_salary_report_cache() -> None:
    """失效薪資相關報表快取：report dashboard（salary_monthly 趨勢）與 salary contributors。

    與 invalidate_finance_summary_cache 分開：這兩個快取只受「薪資寫入」影響
    （結算 / manual_adjust / 切月封存解封），不受學費 / 才藝 / 廠商付款影響，故僅在
    薪資寫入路徑呼叫——避免每筆學費繳費都清掉、churn 掉本來低頻的報表快取。

    在本 helper 出現前，reports_dashboard 與 reports_salary_contributors 全 codebase
    無任何 write 路徑失效，結完薪/改 manual_adjust 後報表概況/薪資頁最長要等 30 分鐘
    TTL 才反映新數字。（reports_dashboard 另含考勤/假單月度圖，那部分仍走 TTL 兜底。）
    """
    try:
        from services.report_cache_service import report_cache_service
    except Exception:
        logger.warning("import report_cache_service failed", exc_info=True)
        return

    for category in (
        REPORT_DASHBOARD_CACHE_CATEGORY,
        SALARY_CONTRIBUTORS_CACHE_CATEGORY,
    ):
        try:
            report_cache_service.invalidate_category(None, category)
        except Exception:
            logger.warning("invalidate %s cache failed", category, exc_info=True)
