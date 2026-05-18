"""tests/test_finance_cache.py — utils/finance_cache 測試。

invalidate_finance_summary_cache 內部會 lazy import services.report_cache_service
並對兩個 category 各呼叫一次 invalidate_category：
1. FINANCE_SUMMARY_CACHE_CATEGORY = "reports_finance_summary"
2. MONTHLY_PNL_CACHE_CATEGORY = "reports_monthly_pnl"

任何 Exception 應吞掉並 log warning（不可拋出阻擋 write path）；單一 category
失敗不影響另一 category 的失效呼叫（best-effort、互不阻擋）。
"""

import logging
from unittest.mock import MagicMock, call, patch

import pytest

from utils import finance_cache
from utils.finance_cache import (
    FINANCE_SUMMARY_CACHE_CATEGORY,
    MONTHLY_PNL_CACHE_CATEGORY,
    invalidate_finance_summary_cache,
)


class TestFinanceSummaryCacheCategoryConstant:
    def test_finance_summary_category_value(self):
        assert FINANCE_SUMMARY_CACHE_CATEGORY == "reports_finance_summary"

    def test_monthly_pnl_category_value(self):
        assert MONTHLY_PNL_CACHE_CATEGORY == "reports_monthly_pnl"


class TestInvalidateFinanceSummaryCache:
    def test_calls_invalidate_for_both_categories(self):
        """成功路徑：應 lazy import 並對兩個 category 各呼叫一次 invalidate_category。"""
        fake_service = MagicMock()
        fake_module = MagicMock(report_cache_service=fake_service)

        with patch.dict("sys.modules", {"services.report_cache_service": fake_module}):
            invalidate_finance_summary_cache()

        assert fake_service.invalidate_category.call_count == 2
        fake_service.invalidate_category.assert_has_calls(
            [
                call(None, FINANCE_SUMMARY_CACHE_CATEGORY),
                call(None, MONTHLY_PNL_CACHE_CATEGORY),
            ]
        )

    def test_swallows_exception_and_logs_warning(self, caplog):
        """錯誤路徑：底層 raise 時，本函式應吞例外並 log.warning，不可外拋。"""
        fake_service = MagicMock()
        fake_service.invalidate_category.side_effect = RuntimeError("boom")
        fake_module = MagicMock(report_cache_service=fake_service)

        with patch.dict("sys.modules", {"services.report_cache_service": fake_module}):
            with caplog.at_level(logging.WARNING, logger=finance_cache.__name__):
                # 不應拋例外
                invalidate_finance_summary_cache()

        # 兩個 category 都該嘗試（互不阻擋），各留一筆 warning
        assert fake_service.invalidate_category.call_count == 2
        warnings = [
            rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING
        ]
        assert any("reports_finance_summary" in m for m in warnings)
        assert any("reports_monthly_pnl" in m for m in warnings)

    def test_first_category_failure_does_not_block_second(self, caplog):
        """單一 category 失敗時，另一 category 仍應被嘗試（best-effort）。"""
        fake_service = MagicMock()
        # 第一次呼叫 raise，第二次正常
        fake_service.invalidate_category.side_effect = [RuntimeError("boom"), None]
        fake_module = MagicMock(report_cache_service=fake_service)

        with patch.dict("sys.modules", {"services.report_cache_service": fake_module}):
            with caplog.at_level(logging.WARNING, logger=finance_cache.__name__):
                invalidate_finance_summary_cache()

        assert fake_service.invalidate_category.call_count == 2

    def test_swallows_import_error(self, caplog):
        """lazy import 失敗也不應外拋。"""
        # 用一個會 raise 的 fake module 模擬 import path 異常：
        # 直接讓 services 子套件 lookup 噴錯，藉由 patch import 機制
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "services.report_cache_service":
                raise ImportError("no such module")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            with caplog.at_level(logging.WARNING, logger=finance_cache.__name__):
                invalidate_finance_summary_cache()  # 不應 raise

        assert any(
            "import report_cache_service failed" in rec.message
            for rec in caplog.records
        )

    def test_returns_none(self):
        """函式不回傳值（None）。"""
        fake_service = MagicMock()
        fake_module = MagicMock(report_cache_service=fake_service)
        with patch.dict("sys.modules", {"services.report_cache_service": fake_module}):
            assert invalidate_finance_summary_cache() is None
