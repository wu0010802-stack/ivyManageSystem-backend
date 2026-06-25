"""崩潰防護（bug-hunt 2026-06-25）：_parse_hhmm 對畸形時間字串應回 None 而非 raise。

問題：services/employee_leave_attendance_sync._parse_hhmm 原本 `hh, mm = s.split(":")`，
對非空且無冒號（"0800"/"8"）或多冒號（"08:30:00"）字串 → unpack ValueError；
對非數字（"abc"）或越界（"25:00"/"12:99"）→ int()/time() ValueError。
work_start_time/work_end_time 為 String(5) 無 DB CHECK、employees schema 亦無 pattern=，
畸形舊資料會讓請假寫考勤路徑（_get_employee_schedule / partial-leave 重算）500。

修法：解析失敗一律回 None（與既有「None 字串回 None」語義一致，上游 `or DEFAULT_*`
與 compute_*_with_leave 對 None 已有 fallback）。
"""

import os
import sys
from datetime import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.employee_leave_attendance_sync import _parse_hhmm


def test_parse_hhmm_valid():
    assert _parse_hhmm("08:30") == time(8, 30)
    assert _parse_hhmm("00:00") == time(0, 0)
    assert _parse_hhmm("23:59") == time(23, 59)


def test_parse_hhmm_none_passthrough():
    assert _parse_hhmm(None) is None


@pytest.mark.parametrize(
    "bad",
    ["0800", "8", "", "abc", "25:00", "12:99", "08:30:00", "::", "1:2:3", "  ", "8:"],
)
def test_parse_hhmm_malformed_returns_none_not_raise(bad):
    # 原本對這些輸入會 raise ValueError → 上游 500；修後一律回 None。
    assert _parse_hhmm(bad) is None
