"""F-E 回歸：薪資明細頁請假扣款必須以 Attendance 為 SoT，與引擎落地一致。

現況：salary_field_breakdown._calc_leave_deductions 直接用 LeaveRecord.leave_hours×ratio；
引擎落地（services/salary/utils._sum_leave_deduction._hours）以 Attendance（status /
partial_leave_hours）為 SoT。遇 F-A「全日假當天有打卡」情境即分叉：引擎扣 0、明細顯示整日。

修法：明細頁請假扣款改以 Attendance 為 SoT（status==LEAVE→8、partial>0→partial、
else→0，套相同 ratio / 病假上限），summary 與 rows 同源（皆來自同一份 att-leave pairs）。

本測試直接驗 _calc_leave_deductions：餵 (Attendance, LeaveRecord) pairs，
全日假有打卡（status≠LEAVE、partial=0）→ 扣款 0，與引擎 _sum_leave_deduction 一致。
"""

from datetime import date, time, datetime
from decimal import Decimal

import pytest

from services.finance.salary_field_breakdown import _calc_leave_deductions
from services.salary.utils import _sum_leave_deduction
from models.attendance import Attendance, AttendanceStatus


class _FakeLeave:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.leave_type = kw.get("leave_type", "personal")
        self.start_date = kw.get("start_date", date(2026, 5, 22))
        self.end_date = kw.get("end_date", date(2026, 5, 22))
        self.leave_hours = kw.get("leave_hours", 8.0)
        self.deduction_ratio = kw.get("deduction_ratio", None)


def _att(status, *, partial=None, punch=False, d=date(2026, 5, 22)):
    a = Attendance(employee_id=1, attendance_date=d, status=status)
    if partial is not None:
        a.partial_leave_hours = Decimal(str(partial))
    if punch:
        a.punch_in_time = datetime.combine(d, time(9, 0))
        a.punch_out_time = datetime.combine(d, time(18, 0))
    return a


DAILY = 1000.0


class TestBreakdownAttendanceSoT:
    def test_full_day_leave_with_punch_deduction_zero(self):
        """F-A 情境：全日假有打卡（視同銷假 status=NORMAL、partial=0）→ 明細扣 0。"""
        lv = _FakeLeave(leave_type="personal", leave_hours=8.0)
        att = _att(AttendanceStatus.NORMAL.value, partial=Decimal("0"), punch=True)

        result = _calc_leave_deductions([(att, lv)], DAILY)
        engine_total = _sum_leave_deduction([(att, lv)], DAILY)

        # 與引擎一致：扣 0
        assert engine_total == 0.0
        assert result["leave_deduction_total"] == 0.0

    def test_summary_and_rows_same_source(self):
        """summary 合計 == rows 各列 deduction 加總（同源，不自相矛盾）。"""
        lv = _FakeLeave(leave_type="personal", leave_hours=8.0)
        att = _att(AttendanceStatus.NORMAL.value, partial=Decimal("0"), punch=True)

        result = _calc_leave_deductions([(att, lv)], DAILY)
        rows_sum = sum(r["deduction"] for r in result["leave_breakdown"])
        assert rows_sum == result["leave_deduction_total"]

    def test_full_day_leave_no_punch_deducts_8h(self):
        """全日假無打卡（status=LEAVE）→ 明細扣 8h，與引擎一致。"""
        lv = _FakeLeave(leave_type="personal", leave_hours=8.0)
        att = _att(AttendanceStatus.LEAVE.value)

        result = _calc_leave_deductions([(att, lv)], DAILY)
        engine_total = _sum_leave_deduction([(att, lv)], DAILY)

        # personal 全扣：8h/8 * 1000 * 1.0 = 1000
        assert result["leave_deduction_total"] == engine_total == 1000.0

    def test_partial_leave_uses_attendance_partial_hours(self):
        """部分假以 Attendance.partial_leave_hours 為 SoT（多日攤分後值），與引擎一致。"""
        # 例：多日攤分後該列 partial=4/3，LeaveRecord.leave_hours 仍是整段 4
        lv = _FakeLeave(leave_type="personal", leave_hours=4.0)
        att = _att(
            AttendanceStatus.ABSENT.value, partial=Decimal("1.3333"), punch=False
        )

        result = _calc_leave_deductions([(att, lv)], DAILY)
        engine_total = _sum_leave_deduction([(att, lv)], DAILY)

        assert result["leave_deduction_total"] == engine_total
        # 應反映 partial=1.3333（≈ (1.3333/8)*1000=166），而非整段 4h（500）
        assert result["leave_deduction_total"] == pytest.approx(166.0, abs=2.0)
