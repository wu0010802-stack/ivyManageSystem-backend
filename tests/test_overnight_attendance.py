"""
回歸測試：跨夜班打卡解析 - AttendanceParser 不得將跨夜班視為「曠職兩天」

Bug 情境：
  員工 18:00 上班，隔日 02:00 下班。
  舊邏輯按日曆天分組，Day1 = [18:00]（缺下班卡），Day2 = [02:00]（缺上班卡 + 缺下班卡）
  → 員工被扣 2 次考勤異常，且工時為 0。

預期修正後：
  Day1 = 正常（punch_in=18:00, punch_out=02:00+1天），Day2 被吸收不出現。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import pandas as pd
from datetime import date, datetime, time, timedelta

from services.attendance_parser import AttendanceParser


def _make_df(records: list[tuple]) -> pd.DataFrame:
    """
    建立測試用 DataFrame。
    records: list of (姓名, datetime_str)，例如 [('張小明', '2026-01-14 21:00:00')]
    """
    data = {'姓名': [r[0] for r in records], '時間': [r[1] for r in records]}
    df = pd.DataFrame(data)
    df['punch_datetime'] = pd.to_datetime(df['時間'])
    df['punch_date'] = df['punch_datetime'].dt.date
    df['punch_time'] = df['punch_datetime'].dt.time
    return df


EMP = '張小明'


class TestOvernightStitch:
    """跨夜班縫合：隔日清晨打卡應歸入前一日"""

    def _parse(self, records, schedules=None):
        parser = AttendanceParser(schedules or {})
        df = _make_df(records)
        result = parser._analyze_employee_attendance(EMP, df)
        return result

    def test_overnight_counted_as_one_day_not_two(self):
        """18:00 上班，隔日 02:00 下班 → 只出現 1 天，不出現 2 天"""
        result = self._parse([
            (EMP, '2026-01-14 21:00:00'),
            (EMP, '2026-01-15 02:00:00'),
        ])
        # 次日 02:00 應被吸收到前一天，total_days == 1
        assert result.total_days == 1, f"預期 1 天，得到 {result.total_days} 天"

    def test_overnight_no_missing_punch_flags(self):
        """跨夜班不應被標記為缺打卡"""
        result = self._parse([
            (EMP, '2026-01-14 21:00:00'),
            (EMP, '2026-01-15 02:00:00'),
        ])
        assert result.missing_punch_in_count == 0, "不應有缺上班打卡"
        assert result.missing_punch_out_count == 0, "不應有缺下班打卡"

    def test_overnight_status_normal(self):
        """跨夜班（無遲到/早退）→ status 應為 normal"""
        # 排班 21:00 – 02:00
        schedules = {EMP: {'work_start': '21:00', 'work_end': '02:00'}}
        result = self._parse([
            (EMP, '2026-01-14 21:00:00'),
            (EMP, '2026-01-15 02:00:00'),
        ], schedules=schedules)
        assert result.normal_days >= 1

    def test_non_overnight_late_not_stitched(self):
        """正常遲到（08:00 上，09:00 才到），不應被誤縫合"""
        result = self._parse([
            (EMP, '2026-01-14 09:00:00'),  # 遲到
            (EMP, '2026-01-14 17:00:00'),
        ])
        assert result.total_days == 1
        assert result.late_count == 1

    def test_two_normal_days_not_stitched(self):
        """兩個正常工作日 (08:00–17:00) 不應被縫合"""
        result = self._parse([
            (EMP, '2026-01-14 08:00:00'),
            (EMP, '2026-01-14 17:00:00'),
            (EMP, '2026-01-15 08:00:00'),
            (EMP, '2026-01-15 17:00:00'),
        ])
        assert result.total_days == 2
        assert result.missing_punch_in_count == 0
        assert result.missing_punch_out_count == 0

    def test_overnight_with_next_normal_day(self):
        """跨夜班後的普通工作日應仍正常計算"""
        result = self._parse([
            (EMP, '2026-01-14 21:00:00'),  # 跨夜班 Day1
            (EMP, '2026-01-15 02:00:00'),  # 跨夜班結束（應被吸收到 Day1）
            (EMP, '2026-01-15 08:00:00'),  # Day1 的隔日（Fri）白班
            (EMP, '2026-01-15 17:00:00'),
        ])
        # Day 14 (Wed): 一個跨夜班
        # Day 15 (Thu): 白班 08:00–17:00
        # 兩天都出現，都不是異常
        assert result.total_days == 2
        assert result.missing_punch_in_count == 0
        assert result.missing_punch_out_count == 0
