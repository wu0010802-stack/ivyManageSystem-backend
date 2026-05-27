"""月出勤日曆 parity test：新版（outerjoin）vs legacy（leave_map）

P-01 ~ P-35：在相同的 Attendance + LeaveRecord 資料下，
  _get_attendance_calendar_new() 與 _get_attendance_calendar_legacy()
  應輸出 byte-identical 的 dict。

前提：只有在 sync.apply() 正確寫入 Attendance.leave_record_id 後，
  兩版才會對齊。所有含 leave 的 scenario 都先呼叫 sync.apply()。

已知設計差異（不是 bug，不在 parity scope 內）：
  - 有 Attendance row 但 leave_record_id=NULL（手動補打卡、未 sync），
    legacy 會從 leave_map 取 leave，new 版看不到。
  - leave.is_approved 事後翻 False 後，new 版仍跟 FK 走，
    legacy 會過濾掉；屬 follow-up 修項，在 xfail 個案記錄。

Merge 後一週 follow-up：刪除 _get_attendance_calendar_legacy 與本檔。
"""

import pytest
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.attendance import Attendance, AttendanceStatus
from models.employee import Employee
from models.leave import LeaveRecord
from models.overtime import OvertimeRecord
import services.employee_leave_attendance_sync as sync
from api.attendance.reports import (
    _get_attendance_calendar_legacy,
    _get_attendance_calendar_new,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db_session():
    """In-memory SQLite session，建全套 schema。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def emp(db_session):
    """測試員工（月薪制）。"""
    employee = Employee(
        employee_id="P001",
        name="測試員工",
        base_salary=40000,
        is_active=True,
    )
    db_session.add(employee)
    db_session.commit()
    return employee


# ── Helper builders ─────────────────────────────────────────────────────────


def _make_leave(
    db_session,
    emp_id: int,
    *,
    leave_type: str = "personal",
    start_date: date,
    end_date: date,
    leave_hours: float = 8.0,
    start_time: str | None = None,
    end_time: str | None = None,
    status: str = "approved",
) -> LeaveRecord:
    lv = LeaveRecord(
        employee_id=emp_id,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        leave_hours=leave_hours,
        start_time=start_time,
        end_time=end_time,
        status=status,
    )
    db_session.add(lv)
    db_session.commit()
    return lv


def _make_attendance(
    db_session,
    emp_id: int,
    *,
    att_date: date,
    status: str = AttendanceStatus.NORMAL.value,
    punch_in: time | None = None,
    punch_out: time | None = None,
    is_late: bool = False,
    late_minutes: int = 0,
    is_early_leave: bool = False,
    remark: str | None = None,
) -> Attendance:
    att = Attendance(
        employee_id=emp_id,
        attendance_date=att_date,
        status=status,
        punch_in_time=(datetime.combine(att_date, punch_in) if punch_in else None),
        punch_out_time=(datetime.combine(att_date, punch_out) if punch_out else None),
        is_late=is_late,
        late_minutes=late_minutes,
        is_early_leave=is_early_leave,
        remark=remark,
    )
    db_session.add(att)
    db_session.commit()
    return att


def _make_overtime(
    db_session,
    emp_id: int,
    *,
    ot_date: date,
    hours: float = 2.0,
    overtime_type: str = "weekday",
    status: str = "approved",
) -> OvertimeRecord:
    ot = OvertimeRecord(
        employee_id=emp_id,
        overtime_date=ot_date,
        hours=hours,
        overtime_type=overtime_type,
        status=status,
    )
    db_session.add(ot)
    db_session.commit()
    return ot


def _assert_parity(db_session, emp, year: int, month: int, label: str = ""):
    """呼叫兩版取 calendar dict，斷言 byte-identical。"""
    legacy = _get_attendance_calendar_legacy(db_session, emp, emp.id, year, month)
    new = _get_attendance_calendar_new(db_session, emp, emp.id, year, month)
    assert legacy == new, (
        f"[{label}] parity mismatch:\n" f"  legacy={legacy}\n" f"  new   ={new}"
    )
    return new  # 讓呼叫端做額外斷言


# ── P-01: 完全空月（無 attendance / 無 leave） ─────────────────────────────


def test_p01_empty_month(db_session, emp):
    result = _assert_parity(db_session, emp, 2026, 5, "P-01 empty month")
    assert result["summary"]["work_days"] == 0
    assert result["summary"]["leave_days"] == 0
    assert all(d["status"] is None for d in result["days"])


# ── P-02: 只有出勤、無請假 ─────────────────────────────────────────────────


def test_p02_attendance_only_no_leave(db_session, emp):
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 6),
        punch_in=time(9, 0),
        punch_out=time(18, 0),
    )
    _assert_parity(db_session, emp, 2026, 5, "P-02 attendance only")


# ── P-03: 全天 personal 假單日 ─────────────────────────────────────────────


def test_p03_single_full_day_personal(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 10),
        end_date=date(2026, 5, 10),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-03 single full-day personal")
    assert result["summary"]["leave_days"] == 1.0


# ── P-04: 全天 sick 假 ─────────────────────────────────────────────────────


def test_p04_single_full_day_sick(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        leave_type="sick",
        start_date=date(2026, 5, 12),
        end_date=date(2026, 5, 12),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-04 single full-day sick")
    day = next(d for d in result["days"] if d["date"] == "2026-05-12")
    assert day["leave_type"] == "sick"
    assert day["leave_type_label"] == "病假"


# ── P-05: 全天 annual 假 3 日連假 ─────────────────────────────────────────


def test_p05_multi_full_day_annual(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        leave_type="annual",
        start_date=date(2026, 5, 19),
        end_date=date(2026, 5, 21),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-05 multi full-day annual")
    assert result["summary"]["leave_days"] == 3.0


# ── P-06: 半天假（上午 09:00-13:00） ──────────────────────────────────────


def test_p06_half_day_morning(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 7),
        end_date=date(2026, 5, 7),
        leave_hours=4.0,
        start_time="09:00",
        end_time="13:00",
    )
    # 先建出勤（09:30 到、18:00 下），sync 後 late 應被 leave-aware 清零
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 7),
        punch_in=time(9, 30),
        punch_out=time(18, 0),
        is_late=True,
        late_minutes=30,
        status=AttendanceStatus.LATE.value,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-06 half-day morning")
    day = next(d for d in result["days"] if d["date"] == "2026-05-07")
    assert day["leave_hours"] == 4.0


# ── P-07: 半天假（下午 13:00-17:00） ──────────────────────────────────────


def test_p07_half_day_afternoon(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 8),
        end_date=date(2026, 5, 8),
        leave_hours=4.0,
        start_time="13:00",
        end_time="17:00",
    )
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 8),
        punch_in=time(9, 0),
        punch_out=time(17, 0),
        status=AttendanceStatus.NORMAL.value,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    _assert_parity(db_session, emp, 2026, 5, "P-07 half-day afternoon")


# ── P-08: 小時假（2 hr） ──────────────────────────────────────────────────


def test_p08_hourly_leave_2h(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 13),
        end_date=date(2026, 5, 13),
        leave_hours=2.0,
        start_time="09:00",
        end_time="11:00",
    )
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 13),
        punch_in=time(11, 0),
        punch_out=time(18, 0),
        status=AttendanceStatus.NORMAL.value,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-08 hourly 2h")
    day = next(d for d in result["days"] if d["date"] == "2026-05-13")
    assert day["leave_hours"] == 2.0


# ── P-09: 小時假（6 hr） ──────────────────────────────────────────────────


def test_p09_hourly_leave_6h(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 14),
        end_date=date(2026, 5, 14),
        leave_hours=6.0,
        start_time="09:00",
        end_time="15:00",
    )
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 14),
        punch_in=time(9, 0),
        punch_out=time(18, 0),
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    _assert_parity(db_session, emp, 2026, 5, "P-09 hourly 6h")


# ── P-10: 月初第一天全天假 ────────────────────────────────────────────────


def test_p10_first_day_of_month_leave(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 1),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-10 first day of month")
    day = result["days"][0]
    assert day["date"] == "2026-05-01"
    assert day["status"] == AttendanceStatus.LEAVE.value


# ── P-11: 月底最後一天全天假 ─────────────────────────────────────────────


def test_p11_last_day_of_month_leave(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 31),
        end_date=date(2026, 5, 31),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-11 last day of month")
    day = result["days"][-1]
    assert day["date"] == "2026-05-31"
    assert day["status"] == AttendanceStatus.LEAVE.value


# ── P-12: 月底跨月假（假單含上個月，只算本月日數） ────────────────────────


def test_p12_boundary_leave_starts_prev_month(db_session, emp):
    """假單 4/28~5/3，只有 5/1~5/3 在目標月。"""
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 4, 28),
        end_date=date(2026, 5, 3),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-12 boundary prev month")
    # 5/1, 5/2, 5/3 應有 leave
    leave_dates = [d["date"] for d in result["days"] if d["leave_type"] is not None]
    assert "2026-05-01" in leave_dates
    assert "2026-05-03" in leave_dates


# ── P-13: 跨月假（假單含下個月，只算本月日數） ───────────────────────────


def test_p13_boundary_leave_ends_next_month(db_session, emp):
    """假單 5/29~6/2，只有 5/29~5/31 在目標月。"""
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 29),
        end_date=date(2026, 6, 2),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-13 boundary next month")
    leave_dates = [d["date"] for d in result["days"] if d["leave_type"] is not None]
    assert "2026-05-29" in leave_dates
    assert "2026-05-31" in leave_dates


# ── P-14: 假單完全在上個月（目標月無 leave） ─────────────────────────────


def test_p14_leave_entirely_prev_month(db_session, emp):
    """假單 4/20~4/25，目標月 5 月應完全不受影響。"""
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 4, 20),
        end_date=date(2026, 4, 25),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-14 leave entirely prev month")
    assert result["summary"]["leave_days"] == 0


# ── P-15: 病假 + 事假 兩筆不連續假單 ────────────────────────────────────


def test_p15_two_non_overlapping_leaves(db_session, emp):
    lv1 = _make_leave(
        db_session,
        emp.id,
        leave_type="sick",
        start_date=date(2026, 5, 5),
        end_date=date(2026, 5, 6),
        leave_hours=8.0,
    )
    lv2 = _make_leave(
        db_session,
        emp.id,
        leave_type="personal",
        start_date=date(2026, 5, 20),
        end_date=date(2026, 5, 21),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv1.id)
    sync.apply(db_session, lv2.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-15 two non-overlapping leaves")
    assert result["summary"]["leave_days"] == 4.0


# ── P-16: 全天假 + 加班同一天 ────────────────────────────────────────────


def test_p16_leave_and_overtime_same_day(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 16),
        end_date=date(2026, 5, 16),
        leave_hours=8.0,
    )
    _make_overtime(db_session, emp.id, ot_date=date(2026, 5, 16), hours=3.0)
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-16 leave + overtime same day")
    day = next(d for d in result["days"] if d["date"] == "2026-05-16")
    assert day["overtime_hours"] == 3.0


# ── P-17: 加班（無請假） ─────────────────────────────────────────────────


def test_p17_overtime_only(db_session, emp):
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 9),
        punch_in=time(9, 0),
        punch_out=time(21, 0),
    )
    _make_overtime(db_session, emp.id, ot_date=date(2026, 5, 9), hours=2.0)
    _assert_parity(db_session, emp, 2026, 5, "P-17 overtime only")


# ── P-18: 全天假 + 有打卡（臨時上班） ────────────────────────────────────


def test_p18_full_day_leave_with_punch(db_session, emp):
    """全天 leave 但當天仍打卡（臨時上班），leave_record_id 仍應連結。"""
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 18),
        punch_in=time(9, 0),
        punch_out=time(18, 0),
        status=AttendanceStatus.NORMAL.value,
    )
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 18),
        end_date=date(2026, 5, 18),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    _assert_parity(db_session, emp, 2026, 5, "P-18 full-day leave with punch")


# ── P-19: 遲到（有出勤、無請假） ─────────────────────────────────────────


def test_p19_late_attendance_no_leave(db_session, emp):
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 4),
        punch_in=time(9, 15),
        punch_out=time(18, 0),
        is_late=True,
        late_minutes=15,
        status=AttendanceStatus.LATE.value,
    )
    result = _assert_parity(db_session, emp, 2026, 5, "P-19 late attendance no leave")
    day = next(d for d in result["days"] if d["date"] == "2026-05-04")
    assert day["is_late"] is True
    assert day["late_minutes"] == 15


# ── P-20: 早退（有出勤、無請假） ─────────────────────────────────────────


def test_p20_early_leave_attendance(db_session, emp):
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 5),
        punch_in=time(9, 0),
        punch_out=time(16, 0),
        is_early_leave=True,
        status=AttendanceStatus.EARLY_LEAVE.value,
    )
    result = _assert_parity(db_session, emp, 2026, 5, "P-20 early leave attendance")
    day = next(d for d in result["days"] if d["date"] == "2026-05-05")
    assert day["is_early_leave"] is True


# ── P-21: 缺勤 ─────────────────────────────────────────────────────────────


def test_p21_absent(db_session, emp):
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 11),
        status=AttendanceStatus.ABSENT.value,
    )
    result = _assert_parity(db_session, emp, 2026, 5, "P-21 absent")
    day = next(d for d in result["days"] if d["date"] == "2026-05-11")
    assert day["status"] == AttendanceStatus.ABSENT.value


# ── P-22: 補休假 ──────────────────────────────────────────────────────────


def test_p22_compensatory_leave(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        leave_type="compensatory",
        start_date=date(2026, 5, 15),
        end_date=date(2026, 5, 15),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-22 compensatory leave")
    day = next(d for d in result["days"] if d["date"] == "2026-05-15")
    assert day["leave_type_label"] == "補休"


# ── P-23: 婚假 5 日 ────────────────────────────────────────────────────────


def test_p23_marriage_leave_5_days(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        leave_type="marriage",
        start_date=date(2026, 5, 11),
        end_date=date(2026, 5, 15),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-23 marriage leave 5 days")
    assert result["summary"]["leave_days"] == 5.0


# ── P-24: 2 月 28 日整月（2026 平年）──────────────────────────────────────


def test_p24_february_non_leap(db_session, emp):
    """2026 年 2 月 28 天，整月無出勤確認 days 陣列長度正確。"""
    result = _assert_parity(db_session, emp, 2026, 2, "P-24 Feb 2026 non-leap")
    assert len(result["days"]) == 28
    assert result["days"][0]["date"] == "2026-02-01"
    assert result["days"][-1]["date"] == "2026-02-28"


# ── P-25: 2 月 29 日整月（2024 閏年）─────────────────────────────────────


def test_p25_february_leap_year(db_session, emp):
    """2024 年 2 月 29 天（閏年）。"""
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2024, 2, 29),
        end_date=date(2024, 2, 29),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2024, 2, "P-25 Feb 2024 leap")
    assert len(result["days"]) == 29
    day = result["days"][-1]
    assert day["date"] == "2024-02-29"
    assert day["status"] == AttendanceStatus.LEAVE.value


# ── P-26: 全月請假（31 天） ───────────────────────────────────────────────


def test_p26_entire_month_leave(db_session, emp):
    """整個 5 月都請假（極端情況）。"""
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 31),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-26 entire month leave")
    assert result["summary"]["leave_days"] == 31.0
    leave_statuses = [d["status"] for d in result["days"]]
    assert all(s == AttendanceStatus.LEAVE.value for s in leave_statuses)


# ── P-27: 多筆加班不同日 ──────────────────────────────────────────────────


def test_p27_multiple_overtime_records(db_session, emp):
    for att_date, ot_date, hrs in [
        (date(2026, 5, 4), date(2026, 5, 4), 2.0),
        (date(2026, 5, 11), date(2026, 5, 11), 3.0),
        (date(2026, 5, 18), date(2026, 5, 18), 1.5),
    ]:
        _make_attendance(
            db_session,
            emp.id,
            att_date=att_date,
            punch_in=time(9, 0),
            punch_out=time(21, 0),
        )
        _make_overtime(db_session, emp.id, ot_date=ot_date, hours=hrs)
    result = _assert_parity(db_session, emp, 2026, 5, "P-27 multiple overtime records")
    assert result["summary"]["overtime_hours"] == round(2.0 + 3.0 + 1.5, 1)


# ── P-28: 假單 + 出勤 + 加班混合同月 ─────────────────────────────────────


def test_p28_mixed_leave_attendance_overtime(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        leave_type="sick",
        start_date=date(2026, 5, 7),
        end_date=date(2026, 5, 7),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 8),
        punch_in=time(9, 0),
        punch_out=time(18, 0),
    )
    _make_overtime(db_session, emp.id, ot_date=date(2026, 5, 8), hours=2.0)
    _assert_parity(db_session, emp, 2026, 5, "P-28 mixed leave attendance overtime")


# ── P-29: 備注（remark）欄位保留 ─────────────────────────────────────────


def test_p29_remark_preserved(db_session, emp):
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 6),
        punch_in=time(9, 0),
        punch_out=time(18, 0),
        remark="特殊備注",
    )
    result = _assert_parity(db_session, emp, 2026, 5, "P-29 remark preserved")
    day = next(d for d in result["days"] if d["date"] == "2026-05-06")
    assert day["remark"] == "特殊備注"


# ── P-30: 全天假 + 部分假同月混搭 ────────────────────────────────────────


def test_p30_full_day_and_half_day_same_month(db_session, emp):
    lv_full = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 4),
        leave_hours=8.0,
    )
    lv_half = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 6),
        end_date=date(2026, 5, 6),
        leave_hours=4.0,
        start_time="09:00",
        end_time="13:00",
    )
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 6),
        punch_in=time(13, 0),
        punch_out=time(18, 0),
    )
    sync.apply(db_session, lv_full.id)
    sync.apply(db_session, lv_half.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-30 full + half day same month")
    assert result["summary"]["leave_days"] == 1.5


# ── P-31: 颱風假 ──────────────────────────────────────────────────────────


def test_p31_typhoon_leave(db_session, emp):
    lv = _make_leave(
        db_session,
        emp.id,
        leave_type="typhoon",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 22),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-31 typhoon leave")
    day = next(d for d in result["days"] if d["date"] == "2026-05-22")
    assert day["leave_type_label"] == "颱風假"


# ── P-32: 跨年 12/31 假單含 1 月 ─────────────────────────────────────────


def test_p32_year_boundary_dec_to_jan(db_session, emp):
    """假單 12/30~1/2，目標月 12 月只應看到 12/30~12/31。"""
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2025, 12, 30),
        end_date=date(2026, 1, 2),
        leave_hours=8.0,
    )
    sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2025, 12, "P-32 year boundary Dec to Jan")
    leave_dates = [d["date"] for d in result["days"] if d["leave_type"] is not None]
    assert "2025-12-30" in leave_dates
    assert "2025-12-31" in leave_dates
    assert "2026-01-01" not in leave_dates


# ── P-33: 未核准假單（unapproved leave，legacy + new 均不顯示） ───────────


def test_p33_unapproved_leave_not_shown(db_session, emp):
    """未核准假單：兩版都不應顯示 leave（legacy 按 is_approved 過濾）。

    注意：parity test 仍要兩版一致；未核准假不走 sync.apply（會 raise），
    故不建 Attendance row，兩版都看不到。
    """
    _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 25),
        end_date=date(2026, 5, 25),
        leave_hours=8.0,
        status="rejected",
    )
    result = _assert_parity(db_session, emp, 2026, 5, "P-33 unapproved leave")
    day = next(d for d in result["days"] if d["date"] == "2026-05-25")
    assert day["leave_type"] is None


# ── P-34: 多筆出勤有些有 leave 有些沒有 ──────────────────────────────────


def test_p34_mixed_att_some_with_leave(db_session, emp):
    """5 天出勤，其中 2 天有半天請假（已 sync），確認 parity。"""
    for day_num in [5, 6, 7, 8, 11]:
        _make_attendance(
            db_session,
            emp.id,
            att_date=date(2026, 5, day_num),
            punch_in=time(9, 0),
            punch_out=time(18, 0),
        )
    for day_num in [6, 8]:
        lv = _make_leave(
            db_session,
            emp.id,
            start_date=date(2026, 5, day_num),
            end_date=date(2026, 5, day_num),
            leave_hours=4.0,
            start_time="13:00",
            end_time="17:00",
        )
        sync.apply(db_session, lv.id)
    db_session.flush()
    result = _assert_parity(db_session, emp, 2026, 5, "P-34 mixed att some with leave")
    assert result["summary"]["work_days"] == 5
    assert result["summary"]["leave_days"] == 1.0


# ── P-35: summary 匯總欄位計算一致性（多種假 + 加班 + 遲到） ─────────────


def test_p35_summary_totals_comprehensive(db_session, emp):
    """綜合情境：全天假 2 日 + 半天假 1 日 + 加班 3 日 + 遲到 2 日。"""
    # 全天假
    lv1 = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 5),
        leave_hours=8.0,
    )
    # 半天假
    lv2 = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 7),
        end_date=date(2026, 5, 7),
        leave_hours=4.0,
        start_time="09:00",
        end_time="13:00",
    )
    sync.apply(db_session, lv1.id)
    # 5/7 下午出勤先建（半天假後仍來），再讓 sync.apply 更新 leave_record_id
    _make_attendance(
        db_session,
        emp.id,
        att_date=date(2026, 5, 7),
        punch_in=time(13, 0),
        punch_out=time(18, 0),
    )
    sync.apply(db_session, lv2.id)
    db_session.flush()
    # 遲到兩天
    for d, lm in [(date(2026, 5, 11), 10), (date(2026, 5, 12), 5)]:
        _make_attendance(
            db_session,
            emp.id,
            att_date=d,
            punch_in=time(9, 10 + lm - 10),
            punch_out=time(18, 0),
            is_late=True,
            late_minutes=lm,
            status=AttendanceStatus.LATE.value,
        )
    # 加班
    for ot_d, hrs in [
        (date(2026, 5, 11), 2.0),
        (date(2026, 5, 12), 1.5),
        (date(2026, 5, 13), 3.0),
    ]:
        _make_overtime(db_session, emp.id, ot_date=ot_d, hours=hrs)
        if ot_d == date(2026, 5, 13):
            _make_attendance(
                db_session,
                emp.id,
                att_date=ot_d,
                punch_in=time(9, 0),
                punch_out=time(21, 0),
            )

    result = _assert_parity(
        db_session, emp, 2026, 5, "P-35 summary totals comprehensive"
    )
    assert result["summary"]["late_count"] == 2
    assert result["summary"]["leave_days"] == 2.5
    assert result["summary"]["overtime_hours"] == round(2.0 + 1.5 + 3.0, 1)


# ── xfail：已知設計差異 ────────────────────────────────────────────────────


@pytest.mark.xfail(
    reason=(
        "設計差異（follow-up 修項）：leave.is_approved 事後翻 False 後，"
        "新版仍透過 FK 取到 leave，legacy 按 status==approved 過濾掉。"
        "需在 sync.undo() 實作後清除此 xfail。"
    ),
    strict=True,
)
def test_xfail_leave_unapproved_after_sync(db_session, emp):
    """已 sync 後 is_approved 翻 False → 兩版行為分歧（已知差異）。"""
    lv = _make_leave(
        db_session,
        emp.id,
        start_date=date(2026, 5, 20),
        end_date=date(2026, 5, 20),
        leave_hours=8.0,
        status="approved",
    )
    sync.apply(db_session, lv.id)
    db_session.flush()

    # 事後撤回核准
    lv.status = "rejected"
    db_session.commit()

    # legacy 用 status="approved" filter → 5/20 無 leave
    # new 版用 FK → 仍看到 leave（設計差異）
    _assert_parity(db_session, emp, 2026, 5, "xfail unapproved after sync")
