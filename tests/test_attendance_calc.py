"""tests/test_attendance_calc.py — utils/attendance_calc.py 純函式測試。"""

from datetime import date, datetime
from types import SimpleNamespace

from utils.attendance_calc import (
    apply_attendance_status,
    recompute_attendance_status,
)

# 測試用基準日
TEST_DATE = date(2026, 5, 17)


def _dt(h: int, m: int) -> datetime:
    """快速建立 TEST_DATE 當天指定時分的 datetime。"""
    return datetime.combine(TEST_DATE, datetime.min.time()).replace(hour=h, minute=m)


class TestRecomputeAttendanceStatusOnTime:
    def test_punch_in_exactly_on_start_is_normal(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 0),
            punch_out_time=_dt(17, 0),
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_late"] is False
        assert result["is_early_leave"] is False
        assert result["late_minutes"] == 0
        assert result["early_leave_minutes"] == 0
        assert result["status"] == "normal"

    def test_punch_in_before_start_is_normal(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(7, 45),
            punch_out_time=_dt(17, 30),
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_late"] is False
        assert result["is_early_leave"] is False
        assert result["status"] == "normal"


class TestRecomputeAttendanceStatusLate:
    def test_late_5_minutes(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 5),
            punch_out_time=_dt(17, 0),
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_late"] is True
        assert result["late_minutes"] == 5
        assert result["status"] == "late"
        assert result["is_early_leave"] is False

    def test_late_30_minutes(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 30),
            punch_out_time=_dt(17, 0),
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_late"] is True
        assert result["late_minutes"] == 30
        assert result["status"] == "late"


class TestRecomputeAttendanceStatusEarlyLeave:
    def test_early_leave_15_minutes(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 0),
            punch_out_time=_dt(16, 45),
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_early_leave"] is True
        assert result["early_leave_minutes"] == 15
        assert result["status"] == "early_leave"
        assert result["is_late"] is False

    def test_late_and_early_leave_combined_status(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 10),
            punch_out_time=_dt(16, 50),
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_late"] is True
        assert result["is_early_leave"] is True
        assert result["late_minutes"] == 10
        assert result["early_leave_minutes"] == 10
        # 同時遲到+早退 → "late+early_leave"
        assert result["status"] == "late+early_leave"


class TestRecomputeAttendanceStatusMissing:
    def test_missing_punch_in(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=None,
            punch_out_time=_dt(17, 0),
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_missing_punch_in"] is True
        assert result["is_missing_punch_out"] is False
        assert result["status"] == "missing"

    def test_missing_punch_out(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 0),
            punch_out_time=None,
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_missing_punch_in"] is False
        assert result["is_missing_punch_out"] is True
        assert result["status"] == "missing"

    def test_missing_both(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=None,
            punch_out_time=None,
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_missing_punch_in"] is True
        assert result["is_missing_punch_out"] is True
        # 第一個 missing 設 "missing"，第二個變 "missing+missing_out"
        assert "missing" in result["status"]

    def test_late_with_missing_out(self):
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 15),
            punch_out_time=None,
            work_start_str="08:00",
            work_end_str="17:00",
        )
        assert result["is_late"] is True
        assert result["is_missing_punch_out"] is True
        # status 從 "late" → "late+missing_out"
        assert result["status"] == "late+missing_out"


class TestRecomputeAttendanceStatusDefaults:
    def test_default_work_times_when_none(self):
        # work_start_str=None → 預設 08:00；work_end_str=None → 預設 17:00
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 0),
            punch_out_time=_dt(17, 0),
            work_start_str=None,
            work_end_str=None,
        )
        assert result["status"] == "normal"

    def test_custom_work_start_late_against_custom(self):
        # 自訂上班時間 09:00；08:30 打卡仍是早到
        result = recompute_attendance_status(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 30),
            punch_out_time=_dt(18, 0),
            work_start_str="09:00",
            work_end_str="18:00",
        )
        assert result["is_late"] is False
        assert result["status"] == "normal"


class TestApplyAttendanceStatus:
    def test_writes_back_to_attendance_object(self):
        att = SimpleNamespace(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 10),
            punch_out_time=_dt(17, 0),
            is_late=False,
            is_early_leave=False,
            is_missing_punch_in=False,
            is_missing_punch_out=False,
            late_minutes=0,
            early_leave_minutes=0,
            status="unknown",
        )
        result = apply_attendance_status(
            att, work_start_str="08:00", work_end_str="17:00"
        )
        assert att.is_late is True
        assert att.late_minutes == 10
        assert att.status == "late"
        # 回傳值與寫入欄位一致
        assert result["is_late"] is True
        assert result["late_minutes"] == 10

    def test_writes_normal_status(self):
        att = SimpleNamespace(
            attendance_date=TEST_DATE,
            punch_in_time=_dt(8, 0),
            punch_out_time=_dt(17, 0),
            is_late=True,  # 舊值，預期被覆寫
            is_early_leave=False,
            is_missing_punch_in=False,
            is_missing_punch_out=False,
            late_minutes=10,
            early_leave_minutes=0,
            status="late",
        )
        apply_attendance_status(att, work_start_str="08:00", work_end_str="17:00")
        assert att.is_late is False
        assert att.late_minutes == 0
        assert att.status == "normal"


# ── C-1~C-6: leave-aware 遲到/早退分鐘計算 ───────────────────────────────────

from datetime import time
from utils.attendance_calc import compute_late_minutes_with_leave


class TestComputeLateMinutesWithLeave:
    """C-1~C-6:leave-aware 遲到分鐘計算"""

    def test_c1_no_leave_normal_late(self):
        # 上班 09:00、打卡 09:30、無請假 → late=30
        result = compute_late_minutes_with_leave(
            punch_in=time(9, 30),
            scheduled_start=time(9, 0),
            leave_start=None,
            leave_end=None,
        )
        assert result == 30

    def test_c2_leave_covers_punch_time(self):
        # 上班 09:00、打卡 09:30、請假 09:00-10:00 → late=0
        result = compute_late_minutes_with_leave(
            punch_in=time(9, 30),
            scheduled_start=time(9, 0),
            leave_start=time(9, 0),
            leave_end=time(10, 0),
        )
        assert result == 0

    def test_c3_leave_starts_before_work(self):
        # 上班 09:00、打卡 09:30、請假 08:00-10:00 → late=0
        result = compute_late_minutes_with_leave(
            punch_in=time(9, 30),
            scheduled_start=time(9, 0),
            leave_start=time(8, 0),
            leave_end=time(10, 0),
        )
        assert result == 0

    def test_c4_leave_ends_before_punch(self):
        # 上班 09:00、打卡 09:30、請假 09:00-09:15 → late=15
        # (請假涵蓋 09:00-09:15,有效上班開始時間變 09:15,打卡 09:30 遲 15 分)
        result = compute_late_minutes_with_leave(
            punch_in=time(9, 30),
            scheduled_start=time(9, 0),
            leave_start=time(9, 0),
            leave_end=time(9, 15),
        )
        assert result == 15

    def test_c5_leave_short_punch_late(self):
        # 上班 09:00、打卡 10:00、請假 09:00-09:30 → late=30
        # (有效上班 09:30,打卡 10:00 遲 30 分)
        result = compute_late_minutes_with_leave(
            punch_in=time(10, 0),
            scheduled_start=time(9, 0),
            leave_start=time(9, 0),
            leave_end=time(9, 30),
        )
        assert result == 30

    def test_c6_early_leave_covered(self):
        # 早退場景:scheduled_end=18:00、punch_out=17:30、請假 17:30-18:00 → early_leave=0
        # (重用同函式:把 scheduled_start 當 scheduled_end 反向計算)
        # 為了精確,實作獨立 `compute_early_leave_minutes_with_leave`,測試先寫呼叫
        from utils.attendance_calc import compute_early_leave_minutes_with_leave

        result = compute_early_leave_minutes_with_leave(
            punch_out=time(17, 30),
            scheduled_end=time(18, 0),
            leave_start=time(17, 30),
            leave_end=time(18, 0),
        )
        assert result == 0
