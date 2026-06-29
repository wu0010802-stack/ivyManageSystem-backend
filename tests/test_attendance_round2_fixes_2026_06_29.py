"""Track A — qa-loop round2（2026-06-29）attendance 五缺口（1 P2 + 4 P3）。

P2 夜班只補下班卡 → 早退虛灌 1440 → 扣整日薪（compute_shift_aware_status）。
P3 整月刪除缺 self-guard（delete_attendance_records）。
P3 單筆刪除日期格式錯誤回 500 而非 400（delete_single_attendance_record）。
P3 batch-confirm waive→accept 不標 salary stale（batch_confirm_anomalies）。
P3 今日出勤摘要把全日請假計為 present（get_today_attendance_summary）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from models.database import Attendance, Employee
from utils.attendance_calc import compute_shift_aware_status
from utils.taipei_time import today_taipei
import api.attendance.records as records_api
import api.attendance.anomalies as anomalies_api
import api.attendance.reports as reports_api

# ── P2：夜班只補下班卡不得虛灌早退 1440 ──────────────────────────────────────


def test_night_shift_lone_punch_out_no_early_leave():
    """夜班 22:00→06:00（shift_end 正規化到隔日 06:00），只補下班卡（缺上班卡，punch_out
    留在當日 06:00）→ 不應判早退（否則早退 1440 分 → 扣整日薪）。"""
    base = date(2026, 1, 10)
    shift_start = datetime(base.year, base.month, base.day, 22, 0)
    shift_end = datetime(base.year, base.month, base.day, 6, 0) + timedelta(days=1)
    punch_out = datetime(base.year, base.month, base.day, 6, 0)  # 留當日 06:00
    is_late, late_m, is_early, early_m, status = compute_shift_aware_status(
        None, punch_out, shift_start, shift_end
    )
    assert is_early is False, "缺上班卡的 lone punch_out 不應判早退"
    assert early_m == 0


def test_day_shift_full_pair_early_leave_still_detected():
    """回歸：日班兩卡齊全、提早走 → 仍正常判早退（新守衛不得誤殺正常路徑）。"""
    base = date(2026, 1, 10)
    shift_start = datetime(base.year, base.month, base.day, 8, 0)
    shift_end = datetime(base.year, base.month, base.day, 17, 0)
    punch_out = datetime(base.year, base.month, base.day, 15, 0)  # 早退 2h
    is_late, late_m, is_early, early_m, status = compute_shift_aware_status(
        shift_start, punch_out, shift_start, shift_end
    )
    assert is_early is True
    assert early_m == 120


# ── P3：單筆刪除日期格式錯誤回 400 ───────────────────────────────────────────


def test_delete_single_bad_date_returns_400():
    with pytest.raises(HTTPException) as exc:
        records_api.delete_single_attendance_record(
            1, "not-a-date", current_user={"employee_id": 999}
        )
    assert exc.value.status_code == 400


# ── P3：整月刪除缺 self-guard ────────────────────────────────────────────────


def test_month_delete_rejects_self(test_db_session):
    """整月刪除若含 caller 自己的考勤列 → 403（對齊單筆/批次 self-guard）。"""
    session = test_db_session
    emp = Employee(employee_id="SELF1", name="自己", is_active=True)
    session.add(emp)
    session.commit()
    # 用 >5 年舊月份讓保存期守衛放行，使 self-guard 成為實際拒因
    session.add(Attendance(employee_id=emp.id, attendance_date=date(2020, 5, 10)))
    session.commit()
    with pytest.raises(HTTPException) as exc:
        records_api.delete_attendance_records(
            2020, 5, current_user={"employee_id": emp.id}
        )
    assert exc.value.status_code == 403


# ── P3：waive→accept 標 salary stale ─────────────────────────────────────────


def test_waive_to_accept_marks_salary_stale(test_db_session, monkeypatch):
    """已 admin_waive 的列改批次 admin_accept → 應恢復扣款而標 salary stale。"""
    session = test_db_session
    att = Attendance(
        employee_id=1,
        attendance_date=date(2026, 5, 10),
        confirmed_action="admin_waive",
        is_late=True,
    )
    session.add(att)
    session.commit()

    calls = []
    monkeypatch.setattr(
        anomalies_api,
        "lock_and_premark_stale",
        lambda s, emp_id, months: calls.append((emp_id, set(months))),
    )
    anomalies_api.batch_confirm_anomalies(
        anomalies_api.BatchConfirmRequest(
            attendance_ids=[att.id], action="admin_accept"
        ),
        current_user={"employee_id": 999, "username": "adm"},
    )
    assert calls, "waive→accept 應標 salary stale 觸發重算（恢復扣款）"
    assert calls[0][0] == 1
    assert (2026, 5) in calls[0][1]


# ── P3：今日出勤摘要不把全日請假計為 present ──────────────────────────────────


def test_today_summary_excludes_full_day_leave(test_db_session):
    session = test_db_session
    emp = Employee(employee_id="LV1", name="請假員", is_active=True)
    session.add(emp)
    session.commit()
    # 全日請假列：sync 後 missing 旗標皆 False、無真實打卡 timestamp
    session.add(
        Attendance(
            employee_id=emp.id,
            attendance_date=today_taipei(),
            status="leave",
            is_missing_punch_in=False,
            is_missing_punch_out=False,
            punch_in_time=None,
            punch_out_time=None,
        )
    )
    session.commit()
    res = reports_api.get_today_attendance_summary(current_user={"employee_id": 999})
    assert res["present_count"] == 0, "全日請假不應計為出勤"
