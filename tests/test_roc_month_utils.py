"""tests/test_roc_month_utils.py — utils/roc_month_utils.py 純函式測試。"""

from datetime import datetime

import pytest

from utils.roc_month_utils import (
    expand_roc_month_range,
    extract_roc_month_from_visit_date,
    normalize_roc_month,
    parse_roc_month_parts,
    roc_month_sort_key,
    roc_month_start,
    safe_normalize_roc_month,
    shift_roc_month,
)


class TestNormalizeRocMonth:
    def test_happy_path_pads_single_digit_month(self):
        assert normalize_roc_month("115.3") == "115.03"

    def test_already_padded_returns_same(self):
        assert normalize_roc_month("115.03") == "115.03"

    def test_strips_whitespace(self):
        assert normalize_roc_month("  114.12 ") == "114.12"

    def test_none_returns_none(self):
        assert normalize_roc_month(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_roc_month("") is None
        assert normalize_roc_month("   ") is None

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="月份格式應為"):
            normalize_roc_month("11503")

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError, match="月份格式錯誤"):
            normalize_roc_month("abc.def")

    def test_month_out_of_range_raises(self):
        with pytest.raises(ValueError, match="月份須在 1-12"):
            normalize_roc_month("115.13")

    def test_month_zero_raises(self):
        with pytest.raises(ValueError, match="月份須在 1-12"):
            normalize_roc_month("115.0")

    def test_zero_year_raises(self):
        with pytest.raises(ValueError, match="年份須為正整數"):
            normalize_roc_month("0.05")


class TestExtractRocMonthFromVisitDate:
    def test_happy_path_dot_format(self):
        assert extract_roc_month_from_visit_date("114.09.16") == "114.09"

    def test_dash_format(self):
        assert extract_roc_month_from_visit_date("114-09-16") == "114.09"

    def test_slash_format(self):
        assert extract_roc_month_from_visit_date("114/09/16") == "114.09"

    def test_none_returns_none(self):
        assert extract_roc_month_from_visit_date(None) is None

    def test_empty_returns_none(self):
        assert extract_roc_month_from_visit_date("") is None
        assert extract_roc_month_from_visit_date("   ") is None

    def test_no_match_returns_none(self):
        assert extract_roc_month_from_visit_date("無效格式") is None

    def test_invalid_month_returns_none(self):
        # 13 月不通過驗證
        assert extract_roc_month_from_visit_date("114.13.16") is None


class TestSafeNormalizeRocMonth:
    def test_valid_normalized(self):
        assert safe_normalize_roc_month("115.3") == "115.03"

    def test_none(self):
        assert safe_normalize_roc_month(None) is None

    def test_invalid_kept_as_stripped(self):
        # 異常資料保留原值
        assert safe_normalize_roc_month("invalid") == "invalid"

    def test_empty_returns_none(self):
        assert safe_normalize_roc_month("   ") is None


class TestRocMonthSortKey:
    def test_normal_values_sort_ascending(self):
        data = ["115.03", "114.12", "115.01"]
        result = sorted(data, key=roc_month_sort_key)
        assert result == ["114.12", "115.01", "115.03"]

    def test_unknown_sorts_last(self):
        data = ["115.03", "未知", "114.12"]
        result = sorted(data, key=roc_month_sort_key)
        assert result[-1] == "未知"

    def test_none_sorts_last(self):
        data = ["115.03", None, "114.12"]
        result = sorted(data, key=roc_month_sort_key)
        assert result[-1] is None

    def test_invalid_format_sorts_near_end(self):
        # 無效但非空：放 999998；空/未知放 999999
        key = roc_month_sort_key("garbage")
        assert key[0] == 999998


class TestParseRocMonthParts:
    def test_happy_path(self):
        assert parse_roc_month_parts("115.03") == (115, 3)

    def test_pads_one_digit(self):
        assert parse_roc_month_parts("114.7") == (114, 7)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_roc_month_parts("invalid")


class TestShiftRocMonth:
    def test_shift_forward_same_year(self):
        assert shift_roc_month("115.03", 2) == "115.05"

    def test_shift_forward_cross_year(self):
        assert shift_roc_month("115.11", 3) == "116.02"

    def test_shift_backward(self):
        assert shift_roc_month("115.03", -2) == "115.01"

    def test_shift_backward_cross_year(self):
        assert shift_roc_month("115.02", -3) == "114.11"

    def test_shift_zero(self):
        assert shift_roc_month("115.03", 0) == "115.03"

    def test_none_returns_none(self):
        assert shift_roc_month(None, 1) is None

    def test_empty_returns_none(self):
        assert shift_roc_month("", 1) is None

    def test_underflow_returns_none(self):
        # 1.01 - 100 個月會 underflow
        assert shift_roc_month("1.01", -100) is None


class TestRocMonthStart:
    def test_happy_path_converts_to_western(self):
        # 民國 115.03 → 西元 2026/03/01
        assert roc_month_start("115.03") == datetime(2026, 3, 1)

    def test_january(self):
        assert roc_month_start("114.01") == datetime(2025, 1, 1)

    def test_none_returns_none(self):
        assert roc_month_start(None) is None

    def test_empty_returns_none(self):
        assert roc_month_start("") is None

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            roc_month_start("invalid")


class TestExpandRocMonthRange:
    def test_happy_path_same_year(self):
        result = expand_roc_month_range("115.01", "115.03")
        assert result == {"115.01", "115.02", "115.03"}

    def test_cross_year(self):
        result = expand_roc_month_range("114.11", "115.02")
        assert result == {"114.11", "114.12", "115.01", "115.02"}

    def test_single_month(self):
        result = expand_roc_month_range("115.03", "115.03")
        assert result == {"115.03"}

    def test_reversed_range_handled(self):
        # caller 給反向也能處理
        result = expand_roc_month_range("115.03", "115.01")
        assert result == {"115.01", "115.02", "115.03"}

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            expand_roc_month_range("invalid", "115.03")
