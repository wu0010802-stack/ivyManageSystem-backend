"""tests/test_finance_cache.py — utils/finance_cache 測試。

invalidate_finance_summary_cache 內部會 lazy import services.report_cache_service
並呼叫 invalidate_category(None, FINANCE_SUMMARY_CACHE_CATEGORY)；
任何 Exception 應吞掉並 log warning（不可拋出阻擋 write path）。
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from utils import finance_cache
from utils.finance_cache import (
    FINANCE_SUMMARY_CACHE_CATEGORY,
    invalidate_finance_summary_cache,
)


class TestFinanceSummaryCacheCategoryConstant:
    def test_category_constant_value(self):
        assert FINANCE_SUMMARY_CACHE_CATEGORY == "reports_finance_summary"


class TestInvalidateFinanceSummaryCache:
    def test_calls_service_invalidate_category(self):
        """成功路徑：應 lazy import 並用 (None, category) 呼叫 invalidate_category。"""
        fake_service = MagicMock()
        fake_module = MagicMock(report_cache_service=fake_service)

        with patch.dict("sys.modules", {"services.report_cache_service": fake_module}):
            invalidate_finance_summary_cache()

        fake_service.invalidate_category.assert_called_once_with(
            None, FINANCE_SUMMARY_CACHE_CATEGORY
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

        # 應留下 warning log
        assert any(
            "invalidate finance_summary cache failed" in rec.message
            for rec in caplog.records
        )

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
            "invalidate finance_summary cache failed" in rec.message
            for rec in caplog.records
        )

    def test_returns_none(self):
        """函式不回傳值（None）。"""
        fake_service = MagicMock()
        fake_module = MagicMock(report_cache_service=fake_service)
        with patch.dict("sys.modules", {"services.report_cache_service": fake_module}):
            assert invalidate_finance_summary_cache() is None
