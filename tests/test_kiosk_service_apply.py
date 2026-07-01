"""kiosk apply_punch 整合測試（TDD）。"""

from datetime import date, datetime

import pytest

from models.database import Attendance, Employee
from services.attendance_kiosk import apply_punch


def _emp(session):
    e = Employee(
        employee_id="E902",
        name="李老師",
        work_start_time="08:00",
        work_end_time="17:00",
        is_active=True,
    )
    session.add(e)
    session.commit()
    return e


def test_apply_first_punch_writes_punch_in_and_source(test_db_session):
    emp = _emp(test_db_session)
    res = apply_punch(test_db_session, emp, datetime(2026, 6, 30, 8, 0))
    assert res.action == "punch_in"
    row = test_db_session.query(Attendance).filter_by(employee_id=emp.id).one()
    assert row.punch_in_time == datetime(2026, 6, 30, 8, 0)
    assert row.punch_out_time is None
    assert row.source == "kiosk"


def test_apply_second_punch_sets_punch_out(test_db_session):
    emp = _emp(test_db_session)
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 8, 0))
    res = apply_punch(test_db_session, emp, datetime(2026, 6, 30, 17, 0))
    assert res.action == "punch_out"
    row = test_db_session.query(Attendance).filter_by(employee_id=emp.id).one()
    assert row.punch_in_time == datetime(2026, 6, 30, 8, 0)
    assert row.punch_out_time == datetime(2026, 6, 30, 17, 0)


def test_apply_third_punch_overwrites_punch_out_not_punch_in(test_db_session):
    emp = _emp(test_db_session)
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 8, 0))
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 12, 5))
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 17, 30))
    row = test_db_session.query(Attendance).filter_by(employee_id=emp.id).one()
    assert row.punch_in_time == datetime(2026, 6, 30, 8, 0)  # 上班不被覆蓋
    assert row.punch_out_time == datetime(2026, 6, 30, 17, 30)  # 下班末次為準


# ── 金流 parity：confirmation-reset 不變量 ────────────────────────────────────


def test_apply_punch_resets_confirmed_action_on_flag_change(test_db_session):
    """kiosk 下班打卡覆寫既有 row 的旗標時，應清除 confirmed_action（防豁免殘留漏扣）。

    情境：該員工今天上班打卡後被 admin 標 admin_waive（遲到豁免）；
    下班再次打卡 → punch_out_time / is_missing_punch_out 實質改變
    → reset_confirmation_if_changed 應清空 confirmed_action。
    """
    emp = Employee(
        employee_id="E904",
        name="王老師",
        work_start_time="08:00",
        work_end_time="17:00",
        is_active=True,
    )
    test_db_session.add(emp)
    test_db_session.commit()

    # 預先建立既有列（已有 admin 豁免確認、且 punch_out 缺打）
    today = date(2026, 6, 30)
    existing = Attendance(
        employee_id=emp.id,
        attendance_date=today,
        punch_in_time=datetime(2026, 6, 30, 8, 5),
        status="late",
        is_late=True,
        late_minutes=5,
        is_missing_punch_out=True,
        confirmed_action="admin_waive",  # 豁免已確認
    )
    test_db_session.add(existing)
    test_db_session.commit()

    # 下班打卡 → punch_out_time 從 None 變成 17:00；is_missing_punch_out 從 True 變 False
    # → 旗標實質改變 → confirmed_action 應被清空（否則漏扣）
    apply_punch(test_db_session, emp, datetime(2026, 6, 30, 17, 0))

    test_db_session.refresh(existing)
    assert (
        existing.confirmed_action is None
    ), "punch_out 後旗標實質改變，confirmed_action 應被清空，但仍殘留"


# ── Step 5 封存守衛測試 ────────────────────────────────────────────────────────
from services.attendance_kiosk import MonthFinalizedError
from models.database import SalaryRecord


def test_apply_rejects_finalized_month(test_db_session):
    emp = _emp(test_db_session)
    test_db_session.add(
        SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=6,
            is_finalized=True,
            finalized_by="HR",
        )
    )
    test_db_session.commit()
    with pytest.raises(MonthFinalizedError):
        apply_punch(test_db_session, emp, datetime(2026, 6, 30, 8, 0))
