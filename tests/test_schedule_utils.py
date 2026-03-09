"""
test_schedule_utils.py — 週工時超時預警工具的單元測試

TDD 結構：Red → Green → Refactor
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from utils.schedule_utils import (
    WEEKLY_WORK_HOURS_LIMIT,
    build_weekly_warning,
    calculate_shift_hours,
    check_weekly_hours_warning,
    compute_weekly_hours,
    get_employee_weekly_shift_hours,
    get_week_dates,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_shift_type(work_start: str, work_end: str):
    return SimpleNamespace(work_start=work_start, work_end=work_end)


# ---------------------------------------------------------------------------
# TestCalculateShiftHours
# ---------------------------------------------------------------------------

class TestCalculateShiftHours:
    def test_normal_shift_8_hours(self):
        """08:00–16:00 應得 8 小時"""
        assert calculate_shift_hours("08:00", "16:00") == pytest.approx(8.0)

    def test_normal_shift_with_minutes(self):
        """07:30–16:30 應得 9 小時"""
        assert calculate_shift_hours("07:30", "16:30") == pytest.approx(9.0)

    def test_overnight_shift(self):
        """22:00–06:00 跨夜班應得 8 小時"""
        assert calculate_shift_hours("22:00", "06:00") == pytest.approx(8.0)

    def test_overnight_exact_midnight(self):
        """20:00–00:00 應得 4 小時（end==start=0 視為跨夜）"""
        # 00:00 = 0 minutes, 20:00 = 1200 minutes → end <= start → +1440 → 1440-1200=240 min = 4h
        assert calculate_shift_hours("20:00", "00:00") == pytest.approx(4.0)

    def test_partial_hours(self):
        """08:00–12:30 應得 4.5 小時"""
        assert calculate_shift_hours("08:00", "12:30") == pytest.approx(4.5)

    def test_one_hour_shift(self):
        """09:00–10:00 應得 1 小時"""
        assert calculate_shift_hours("09:00", "10:00") == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestGetWeekDates
# ---------------------------------------------------------------------------

class TestGetWeekDates:
    def test_monday_input(self):
        """週一輸入，應回傳同一週（含自身）"""
        monday = date(2025, 3, 3)  # 週一
        result = get_week_dates(monday)
        assert result[0] == monday
        assert result[-1] == date(2025, 3, 9)  # 週日
        assert len(result) == 7

    def test_wednesday_input(self):
        """週三輸入，應回傳所在週週一到週日"""
        wednesday = date(2025, 3, 5)  # 週三
        result = get_week_dates(wednesday)
        assert result[0] == date(2025, 3, 3)  # 週一
        assert result[-1] == date(2025, 3, 9)  # 週日
        assert len(result) == 7

    def test_sunday_input(self):
        """週日輸入，應回傳所在週（週一開始）"""
        sunday = date(2025, 3, 9)  # 週日
        result = get_week_dates(sunday)
        assert result[0] == date(2025, 3, 3)
        assert result[-1] == sunday
        assert len(result) == 7

    def test_consecutive_dates(self):
        """回傳的 7 天應連續遞增"""
        result = get_week_dates(date(2025, 3, 7))
        for i in range(1, 7):
            assert (result[i] - result[i - 1]).days == 1


# ---------------------------------------------------------------------------
# TestComputeWeeklyHours
# ---------------------------------------------------------------------------

class TestComputeWeeklyHours:
    def test_five_days_nine_hours(self):
        """5 天 × 9h = 45h"""
        week = get_week_dates(date(2025, 3, 3))
        mapping = {d: 9.0 for d in week[:5]}
        mapping.update({d: None for d in week[5:]})
        assert compute_weekly_hours(mapping) == pytest.approx(45.0)

    def test_all_none(self):
        """全部 None（全休）應得 0"""
        week = get_week_dates(date(2025, 3, 3))
        mapping = {d: None for d in week}
        assert compute_weekly_hours(mapping) == pytest.approx(0.0)

    def test_exactly_forty_hours(self):
        """5 天 × 8h = 40h（剛好達到上限）"""
        week = get_week_dates(date(2025, 3, 3))
        mapping = {d: 8.0 for d in week[:5]}
        mapping.update({d: None for d in week[5:]})
        assert compute_weekly_hours(mapping) == pytest.approx(40.0)

    def test_mixed_values(self):
        """混合有值與 None"""
        week = get_week_dates(date(2025, 3, 3))
        mapping = {week[0]: 8.0, week[1]: None, week[2]: 9.5, week[3]: None, week[4]: 7.0}
        mapping.update({d: None for d in week[5:]})
        assert compute_weekly_hours(mapping) == pytest.approx(24.5)


# ---------------------------------------------------------------------------
# TestBuildWeeklyWarning
# ---------------------------------------------------------------------------

class TestBuildWeeklyWarning:
    def _week_start(self):
        return date(2025, 3, 3)

    def test_exactly_forty_returns_none(self):
        """剛好 40h 不超過上限，應回傳 None"""
        result = build_weekly_warning(1, "王小明", self._week_start(), 40.0)
        assert result is None

    def test_below_limit_returns_none(self):
        """38h 未超過上限，應回傳 None"""
        result = build_weekly_warning(1, "王小明", self._week_start(), 38.0)
        assert result is None

    def test_over_limit_returns_warning(self):
        """42h 超過上限，應回傳 warning dict"""
        result = build_weekly_warning(1, "王小明", self._week_start(), 42.0)
        assert result is not None
        assert result["code"] == "WEEKLY_HOURS_EXCEEDED"

    def test_warning_contains_employee_info(self):
        """警告訊息應包含員工名稱與工時數字"""
        result = build_weekly_warning(99, "李大華", self._week_start(), 42.5)
        assert result["employee_id"] == 99
        assert result["employee_name"] == "李大華"
        assert "42.5" in result["message"]
        assert "李大華" in result["message"]

    def test_warning_calculated_hours(self):
        """calculated_hours 應等於傳入工時（四捨五入到 2 位）"""
        result = build_weekly_warning(1, "陳美玲", self._week_start(), 41.333333)
        assert result["calculated_hours"] == pytest.approx(41.33)

    def test_warning_limit_hours(self):
        """limit_hours 應等於預設上限 40.0"""
        result = build_weekly_warning(1, "陳美玲", self._week_start(), 41.0)
        assert result["limit_hours"] == WEEKLY_WORK_HOURS_LIMIT

    def test_warning_week_start_iso(self):
        """week_start 欄位應為 ISO 格式字串"""
        result = build_weekly_warning(1, "test", self._week_start(), 45.0)
        assert result["week_start"] == "2025-03-03"

    def test_custom_limit(self):
        """可傳入自訂上限"""
        result = build_weekly_warning(1, "test", self._week_start(), 35.0, limit=30.0)
        assert result is not None  # 35 > 30

        result2 = build_weekly_warning(1, "test", self._week_start(), 30.0, limit=30.0)
        assert result2 is None  # 30 == 30，不超過


# ---------------------------------------------------------------------------
# TestGetEmployeeWeeklyShiftHours
# ---------------------------------------------------------------------------

class TestGetEmployeeWeeklyShiftHours:
    """使用 MagicMock session 測試 DB 查詢層邏輯。"""

    def _make_session(self, daily_shifts=None, weekly_sa=None):
        """建立 mock session，回傳指定的查詢結果。"""
        session = MagicMock()

        # 模擬 session.query(DailyShift).filter(...).all()
        # 模擬 session.query(ShiftAssignment).filter(...).first()
        def query_side_effect(model):
            mock_query = MagicMock()
            mock_filter = MagicMock()
            model_name = getattr(model, "__name__", str(model))

            if "DailyShift" in model_name:
                mock_filter.all.return_value = daily_shifts or []
                mock_query.filter.return_value = mock_filter
            elif "ShiftAssignment" in model_name:
                mock_filter.first.return_value = weekly_sa
                mock_query.filter.return_value = mock_filter
            else:
                mock_query.filter.return_value = mock_filter
            return mock_query

        session.query.side_effect = query_side_effect
        return session

    def _make_daily_shift(self, d: date, shift_type_id):
        ds = MagicMock()
        ds.date = d
        ds.shift_type_id = shift_type_id
        return ds

    def _make_sa(self, shift_type_id):
        sa = MagicMock()
        sa.shift_type_id = shift_type_id
        return sa

    def test_daily_shift_takes_priority_over_weekly(self):
        """DailyShift 記錄應優先於 ShiftAssignment"""
        week_dates = get_week_dates(date(2025, 3, 3))
        target_date = week_dates[0]  # 週一

        # ShiftAssignment = 班別 1（8h）
        # DailyShift on 週一 = 班別 2（9h）
        st1 = _make_shift_type("08:00", "16:00")   # 8h
        st2 = _make_shift_type("07:30", "16:30")   # 9h
        shift_type_map = {1: st1, 2: st2}

        daily = [self._make_daily_shift(target_date, shift_type_id=2)]
        sa = self._make_sa(shift_type_id=1)
        session = self._make_session(daily_shifts=daily, weekly_sa=sa)

        result = get_employee_weekly_shift_hours(session, 1, week_dates, shift_type_map)
        # 週一應得 DailyShift 的 9h
        assert result[target_date] == pytest.approx(9.0)
        # 其他天（無 DailyShift）應得 ShiftAssignment 的 8h
        assert result[week_dates[1]] == pytest.approx(8.0)

    def test_overrides_take_priority_over_daily_shift(self):
        """overrides 應優先於 DailyShift"""
        week_dates = get_week_dates(date(2025, 3, 3))
        target_date = week_dates[0]

        st1 = _make_shift_type("08:00", "16:00")   # 8h
        st3 = _make_shift_type("09:00", "19:00")   # 10h
        shift_type_map = {1: st1, 3: st3}

        # DailyShift 指定班別 1（8h），overrides 改為班別 3（10h）
        daily = [self._make_daily_shift(target_date, shift_type_id=1)]
        sa = self._make_sa(shift_type_id=1)
        session = self._make_session(daily_shifts=daily, weekly_sa=sa)

        result = get_employee_weekly_shift_hours(
            session, 1, week_dates, shift_type_map,
            overrides={target_date: 3}
        )
        assert result[target_date] == pytest.approx(10.0)

    def test_overrides_none_means_day_off(self):
        """overrides 中 None 表示換班後排休（工時為 None）"""
        week_dates = get_week_dates(date(2025, 3, 3))
        target_date = week_dates[0]

        st1 = _make_shift_type("08:00", "16:00")
        shift_type_map = {1: st1}

        # 本來有 ShiftAssignment，overrides 設為 None（排休）
        sa = self._make_sa(shift_type_id=1)
        session = self._make_session(daily_shifts=[], weekly_sa=sa)

        result = get_employee_weekly_shift_hours(
            session, 1, week_dates, shift_type_map,
            overrides={target_date: None}
        )
        assert result[target_date] is None
        # 其餘天仍有班別
        assert result[week_dates[1]] == pytest.approx(8.0)

    def test_no_assignment_no_daily_returns_none(self):
        """無 ShiftAssignment 且無 DailyShift，應回傳 None"""
        week_dates = get_week_dates(date(2025, 3, 3))
        shift_type_map = {}
        session = self._make_session(daily_shifts=[], weekly_sa=None)

        result = get_employee_weekly_shift_hours(session, 1, week_dates, shift_type_map)
        assert all(v is None for v in result.values())

    def test_daily_shift_none_means_explicit_day_off(self):
        """DailyShift.shift_type_id = None 表示明確排休，即使有週排班也應為 None"""
        week_dates = get_week_dates(date(2025, 3, 3))
        target_date = week_dates[0]

        st1 = _make_shift_type("08:00", "16:00")
        shift_type_map = {1: st1}

        # DailyShift 明確設為 None（排休覆蓋）
        daily = [self._make_daily_shift(target_date, shift_type_id=None)]
        sa = self._make_sa(shift_type_id=1)
        session = self._make_session(daily_shifts=daily, weekly_sa=sa)

        result = get_employee_weekly_shift_hours(session, 1, week_dates, shift_type_map)
        assert result[target_date] is None
        assert result[week_dates[1]] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# TestCheckWeeklyHoursWarning（整合純計算 + DB 層）
# ---------------------------------------------------------------------------

class TestCheckWeeklyHoursWarning:
    def _make_session(self, daily_shifts=None, weekly_sa=None):
        session = MagicMock()

        def query_side_effect(model):
            mock_query = MagicMock()
            mock_filter = MagicMock()
            model_name = getattr(model, "__name__", str(model))
            if "DailyShift" in model_name:
                mock_filter.all.return_value = daily_shifts or []
                mock_query.filter.return_value = mock_filter
            elif "ShiftAssignment" in model_name:
                mock_filter.first.return_value = weekly_sa
                mock_query.filter.return_value = mock_filter
            else:
                mock_query.filter.return_value = mock_filter
            return mock_query

        session.query.side_effect = query_side_effect
        return session

    def _make_sa(self, shift_type_id):
        sa = MagicMock()
        sa.shift_type_id = shift_type_id
        return sa

    def _make_daily_shift(self, d: date, shift_type_id):
        ds = MagicMock()
        ds.date = d
        ds.shift_type_id = shift_type_id
        return ds

    def test_returns_none_when_under_limit(self):
        """週工時 ≤ 40h 不回傳警告

        使用 DailyShift 精確指定 5 天 × 8h = 40h，無 ShiftAssignment。
        """
        st = _make_shift_type("08:00", "16:00")  # 8h
        shift_type_map = {1: st}
        week = get_week_dates(date(2025, 3, 5))  # 所在週週一到週日
        # 僅設定週一到週五各 8h
        daily_shifts = [self._make_daily_shift(week[i], 1) for i in range(5)]
        session = self._make_session(daily_shifts=daily_shifts, weekly_sa=None)

        result = check_weekly_hours_warning(
            session, 1, "王小明", date(2025, 3, 5), shift_type_map
        )
        assert result is None

    def test_returns_warning_when_over_limit(self):
        """週工時超過 40h 應回傳警告 dict（7 天 × 9h = 63h）"""
        st = _make_shift_type("08:00", "17:00")  # 9h
        shift_type_map = {1: st}
        sa = self._make_sa(1)
        session = self._make_session(daily_shifts=[], weekly_sa=sa)

        result = check_weekly_hours_warning(
            session, 1, "王小明", date(2025, 3, 5), shift_type_map
        )
        assert result is not None
        assert result["code"] == "WEEKLY_HOURS_EXCEEDED"
        assert result["employee_name"] == "王小明"

    def test_overrides_affect_result(self):
        """overrides 換班假設應影響計算結果。

        週一、二、四、五各 DailyShift 8h（4×8=32h），無週排班。
        override 週三為 班別 2（9h）→ 32+9=41h → 超過上限。
        """
        st_8h = _make_shift_type("08:00", "16:00")   # 8h
        st_9h = _make_shift_type("08:00", "17:00")   # 9h
        shift_type_map = {1: st_8h, 2: st_9h}

        week = get_week_dates(date(2025, 3, 5))  # 週三
        # Mon(0), Tue(1), Thu(3), Fri(4) 各 8h
        daily_shifts = [self._make_daily_shift(week[i], 1) for i in [0, 1, 3, 4]]
        session = self._make_session(daily_shifts=daily_shifts, weekly_sa=None)

        target_date = date(2025, 3, 5)  # 週三
        result = check_weekly_hours_warning(
            session, 1, "王小明", target_date, shift_type_map,
            overrides={target_date: 2}
        )
        assert result is not None
        assert result["calculated_hours"] == pytest.approx(41.0)
