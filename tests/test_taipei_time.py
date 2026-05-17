"""tests/test_taipei_time.py — utils/taipei_time.py 純函式測試。"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from utils.taipei_time import (
    TAIPEI_TZ,
    now_taipei_naive,
    today_taipei,
    validate_payment_date,
)


class TestTaipeiTzConstant:
    def test_is_asia_taipei(self):
        assert TAIPEI_TZ == ZoneInfo("Asia/Taipei")


class TestTodayTaipei:
    def test_returns_date_instance(self):
        result = today_taipei()
        assert isinstance(result, date)
        # 不是 datetime（date 的子類別也要排除）
        assert not isinstance(result, datetime)

    def test_matches_taipei_now(self):
        result = today_taipei()
        expected = datetime.now(TAIPEI_TZ).date()
        # 跨日邊界 race 容忍
        assert abs((result - expected).days) <= 1


class TestNowTaipeiNaive:
    def test_returns_naive_datetime(self):
        result = now_taipei_naive()
        assert isinstance(result, datetime)
        assert result.tzinfo is None

    def test_matches_taipei_time_within_minute(self):
        result = now_taipei_naive()
        expected = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
        # 跨秒 race 容忍：差距應該小於 60 秒
        delta = abs((result - expected).total_seconds())
        assert delta < 60


class TestValidatePaymentDate:
    def test_today_is_valid(self):
        today = today_taipei()
        assert validate_payment_date(today) == today

    def test_recent_past_is_valid(self):
        d = today_taipei() - timedelta(days=10)
        assert validate_payment_date(d) == d

    def test_future_date_raises(self):
        future = today_taipei() + timedelta(days=1)
        with pytest.raises(ValueError, match="不可為未來"):
            validate_payment_date(future)

    def test_too_old_raises_default_30(self):
        # 預設 30 天
        too_old = today_taipei() - timedelta(days=31)
        with pytest.raises(ValueError, match="超出範圍"):
            validate_payment_date(too_old)

    def test_custom_back_limit_allows_older(self):
        # 90 天上限：60 天前合法
        d = today_taipei() - timedelta(days=60)
        assert validate_payment_date(d, back_limit_days=90) == d

    def test_custom_back_limit_still_enforces(self):
        # 90 天上限：100 天前不合法
        too_old = today_taipei() - timedelta(days=100)
        with pytest.raises(ValueError, match="超出範圍"):
            validate_payment_date(too_old, back_limit_days=90)

    def test_boundary_exactly_back_limit(self):
        # 剛好在邊界（30 天前）應該合法
        d = today_taipei() - timedelta(days=30)
        assert validate_payment_date(d) == d
