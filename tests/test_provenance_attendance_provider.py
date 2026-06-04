"""考勤扣款 provider provenance 測試。

核心保證：
1. provenance value == 既有引擎 derive_attendance_deductions（零漂移）。
2. _q2(Σ source_records.amount) == value（逐筆對帳）。
3. 逐筆 source_records 內容正確（日期/標籤/金額/source_id）。
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from models.attendance import Attendance
from models.config import BonusConfig
from models.employee import Employee
from models.event import MeetingRecord
from models.leave import LeaveRecord
from models.year_end import YearEndCycle
from services.year_end.auto_derive import attendance_deductions as ad
from services.provenance.attendance_provider import derive_attendance_provenance

_Q2 = Decimal("0.01")


def _q2(x):
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def _mk_employee(db, code, name):
    emp = Employee(
        employee_id=code,
        name=name,
        id_number=f"A{code[-9:].rjust(9, '0')}",
        hire_date=date(2023, 8, 1),
        is_active=True,
    )
    db.add(emp)
    db.flush()
    return emp


def _mk_cycle(db, academic_year=114):
    cycle = YearEndCycle(
        academic_year=academic_year,
        start_date=date(academic_year + 1911, 8, 1),
        end_date=date(academic_year + 1912, 7, 31),
        bonus_calc_date=date(academic_year + 1912, 1, 15),
    )
    db.add(cycle)
    db.flush()
    return cycle


def _mk_config(db):
    cfg = BonusConfig(
        config_year=114,
        is_active=True,
        late_deduction_per_time=50,
        missing_punch_deduction_per_time=50,
        personal_leave_deduction_per_day=500,
        sick_leave_deduction_per_day=500,
        meeting_absence_penalty=100,
    )
    db.add(cfg)
    db.flush()
    return cfg


@pytest.fixture
def base(test_db_session):
    db = test_db_session
    cycle = _mk_cycle(db, 114)  # 期間 = 2025/1/1 ~ 2025/12/31
    _mk_config(db)
    emp = _mk_employee(db, "E_PROV_01", "測試員工")
    db.commit()
    return {"db": db, "cycle": cycle, "emp": emp}


def test_late_source_records_and_value(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    for i in range(5):
        a = Attendance(
            employee_id=emp.id, attendance_date=date(2025, 3, i + 1), is_late=True
        )
        db.add(a)
    db.commit()

    result = derive_attendance_provenance(db, cycle, emp)
    dv = result["attendance_late"]

    assert dv.value == ad.derive_attendance_deductions(db, cycle, emp).late
    assert dv.value == Decimal("-250.00")
    assert len(dv.source_records) == 5
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value
    assert all(
        sr.module == "attendance" and sr.amount == Decimal("-50")
        for sr in dv.source_records
    )
    assert dv.source_records[0].source_id is not None
    assert "遲到" in dv.formula_summary


def test_missing_punch_in_late_key(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    db.add(
        Attendance(
            employee_id=emp.id,
            attendance_date=date(2025, 4, 1),
            is_missing_punch_in=True,
            is_missing_punch_out=True,
        )
    )
    db.commit()

    dv = derive_attendance_provenance(db, cycle, emp)["attendance_late"]
    assert dv.value == Decimal("-100.00")  # 2 次 × -50
    assert len(dv.source_records) == 2  # 上班 + 下班各一筆
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value


def test_personal_leave_reconciles(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    db.add(
        LeaveRecord(
            employee_id=emp.id,
            leave_type="personal",
            start_date=date(2025, 5, 1),
            end_date=date(2025, 5, 2),
            leave_hours=16,
            status="approved",
        )
    )  # 2 天 × -500
    db.commit()

    dv = derive_attendance_provenance(db, cycle, emp)["personal_leave"]
    assert dv.value == ad.derive_attendance_deductions(db, cycle, emp).personal_leave
    assert dv.value == Decimal("-1000.00")
    assert len(dv.source_records) == 1
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value


def test_meeting_absence_reconciles(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    db.add(
        MeetingRecord(employee_id=emp.id, meeting_date=date(2025, 6, 1), attended=False)
    )
    db.commit()

    dv = derive_attendance_provenance(db, cycle, emp)["meeting_absence"]
    assert dv.value == Decimal("-100.00")
    assert len(dv.source_records) == 1
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value


def test_sick_leave_reconciles(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    db.add(
        LeaveRecord(
            employee_id=emp.id,
            leave_type="sick",
            start_date=date(2025, 5, 1),
            end_date=date(2025, 5, 2),
            leave_hours=16,
            status="approved",
        )
    )  # 2 天 × -500
    db.commit()

    dv = derive_attendance_provenance(db, cycle, emp)["sick_leave"]
    assert dv.value == ad.derive_attendance_deductions(db, cycle, emp).sick_leave
    assert dv.value == Decimal("-1000.00")
    assert len(dv.source_records) == 1
    assert _q2(sum(sr.amount for sr in dv.source_records)) == dv.value


def test_no_records_zero_no_error(base):
    db, cycle, emp = base["db"], base["cycle"], base["emp"]
    result = derive_attendance_provenance(db, cycle, emp)
    for key in ("attendance_late", "personal_leave", "sick_leave", "meeting_absence"):
        assert result[key].value == Decimal("0.00")
        assert result[key].source_records == []
        assert "無紀錄" in result[key].formula_summary
