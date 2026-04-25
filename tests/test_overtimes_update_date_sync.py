"""Bug 回歸:加班改日期會留下舊日期的 start/end datetime(P2-4)。

Bug 描述:
    api/overtimes.py:update_overtime 在 line 838-852,當 data.overtime_date
    改變但 data.start_time/end_time 沒給時:
      - check_date 為新日期
      - new_start_dt = ot.start_time(完整 datetime,日期=舊日期)
      - new_end_dt = ot.end_time(同上)
    line 911-916 接著只在 data.start_time/end_time 存在時才寫回 ORM,
    造成 ot.overtime_date(新)與 ot.start_time/end_time 的日期部分(舊)不一致。

    後果:
      1. 資料矛盾:overtime_date 與 start/end 的日期欄位差好幾天
      2. _check_overtime_overlap 在 SQL 端比較 datetime 整體值,跨日期 datetime
         會產生不正確的重疊判斷

修復方向:
    當 data.overtime_date 改了但 data.start_time/end_time 沒給,以
    「新日期 + 舊時間」重組 new_start_dt/new_end_dt,並且寫回 ORM。
"""

import sys
import os
import types
from datetime import date, datetime, time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_ot():
    """建立既有加班記錄:2026-03-15 18:00-20:00,未核准。"""
    ot = types.SimpleNamespace()
    ot.id = 1
    ot.employee_id = 10
    ot.overtime_date = date(2026, 3, 15)
    ot.overtime_type = "weekday"
    ot.start_time = datetime(2026, 3, 15, 18, 0)
    ot.end_time = datetime(2026, 3, 15, 20, 0)
    ot.hours = 2.0
    ot.overtime_pay = 0
    ot.use_comp_leave = False
    ot.comp_leave_granted = False
    ot.reason = None
    ot.is_approved = False
    ot.approved_by = None
    return ot


def _make_emp():
    emp = types.SimpleNamespace()
    emp.id = 10
    emp.base_salary = 30000
    return emp


def _common_patches(ot, emp):
    session = MagicMock()
    # session.query(OvertimeRecord).filter(...).first() → ot
    # session.query(Employee).filter(...).first() → emp
    # 用 side_effect 區分兩種查詢:第一次回 ot、第二次回 emp
    first_results = [ot, emp, emp, emp]

    def first_side_effect():
        return first_results.pop(0) if first_results else None

    session.query.return_value.filter.return_value.first.side_effect = first_side_effect
    session.query.return_value.filter.return_value.with_for_update.return_value.first.side_effect = first_side_effect

    return session, [
        patch("api.overtimes.get_session", return_value=session),
        patch("api.overtimes._check_overtime_overlap", return_value=None),
        patch("api.overtimes._check_monthly_overtime_cap"),
        patch("api.overtimes._check_overtime_type_calendar"),
        patch("api.overtimes._check_salary_month_not_finalized"),
        patch("api.overtimes._revoke_comp_leave_grant"),
        patch("api.overtimes.calculate_overtime_pay", return_value=400.0),
        patch("api.overtimes._recalculate_salary_for_overtime_months"),
    ]


class TestUpdateOvertimeDateSync:

    def test_changing_only_overtime_date_keeps_start_end_in_sync(self):
        """只改 overtime_date,start/end 的日期部分必須跟著移到新日期,時間維持。"""
        from api.overtimes import update_overtime, OvertimeUpdate

        ot = _make_ot()
        emp = _make_emp()
        session, patches = _common_patches(ot, emp)
        for p in patches:
            p.start()
        try:
            data = OvertimeUpdate(overtime_date=date(2026, 3, 22))
            update_overtime(
                overtime_id=ot.id,
                data=data,
                current_user={"username": "admin"},
            )
        finally:
            for p in patches:
                p.stop()

        assert ot.overtime_date == date(2026, 3, 22)
        assert ot.start_time.date() == date(2026, 3, 22), (
            f"start_time 日期未同步:{ot.start_time}"
        )
        assert ot.start_time.time() == time(18, 0)
        assert ot.end_time.date() == date(2026, 3, 22)
        assert ot.end_time.time() == time(20, 0)

    def test_overlap_check_called_with_new_date_datetime(self):
        """_check_overtime_overlap 必須以「新日期 + 舊時間」的 datetime 被呼叫。"""
        from api.overtimes import update_overtime, OvertimeUpdate

        ot = _make_ot()
        emp = _make_emp()
        session, patches = _common_patches(ot, emp)
        # 把 overlap check 拆出來單獨追蹤
        overlap_mock = MagicMock(return_value=None)
        patches = [p for p in patches if "_check_overtime_overlap" not in str(p.attribute)]
        patches.append(patch("api.overtimes._check_overtime_overlap", overlap_mock))

        for p in patches:
            p.start()
        try:
            data = OvertimeUpdate(overtime_date=date(2026, 3, 22))
            update_overtime(
                overtime_id=ot.id,
                data=data,
                current_user={"username": "admin"},
            )
        finally:
            for p in patches:
                p.stop()

        assert overlap_mock.called
        args = overlap_mock.call_args.args
        # 訊號:_check_overtime_overlap(session, employee_id, check_date, start_dt, end_dt, ...)
        new_start_dt, new_end_dt = args[3], args[4]
        assert new_start_dt.date() == date(2026, 3, 22), (
            f"overlap 用了舊日期 datetime:{new_start_dt}"
        )
        assert new_start_dt.time() == time(18, 0)
        assert new_end_dt.date() == date(2026, 3, 22)
        assert new_end_dt.time() == time(20, 0)

    def test_changing_only_start_time_does_not_break_date(self):
        """只改 start_time(不動 overtime_date),overtime_date 與 end_time 維持。"""
        from api.overtimes import update_overtime, OvertimeUpdate

        ot = _make_ot()
        emp = _make_emp()
        session, patches = _common_patches(ot, emp)
        for p in patches:
            p.start()
        try:
            data = OvertimeUpdate(start_time="19:00")
            update_overtime(
                overtime_id=ot.id,
                data=data,
                current_user={"username": "admin"},
            )
        finally:
            for p in patches:
                p.stop()

        assert ot.overtime_date == date(2026, 3, 15)
        assert ot.start_time.date() == date(2026, 3, 15)
        assert ot.start_time.time() == time(19, 0)
        assert ot.end_time.date() == date(2026, 3, 15)
        assert ot.end_time.time() == time(20, 0)

    def test_no_start_end_on_record_remains_none(self):
        """既有記錄沒有 start/end(都是 None),改日期後仍應是 None,不報錯。"""
        from api.overtimes import update_overtime, OvertimeUpdate

        ot = _make_ot()
        ot.start_time = None
        ot.end_time = None
        emp = _make_emp()
        session, patches = _common_patches(ot, emp)
        for p in patches:
            p.start()
        try:
            data = OvertimeUpdate(overtime_date=date(2026, 3, 22))
            update_overtime(
                overtime_id=ot.id,
                data=data,
                current_user={"username": "admin"},
            )
        finally:
            for p in patches:
                p.stop()

        assert ot.overtime_date == date(2026, 3, 22)
        assert ot.start_time is None
        assert ot.end_time is None
