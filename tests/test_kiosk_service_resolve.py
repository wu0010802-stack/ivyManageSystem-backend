"""Task 4 TDD — resolve_punch_action first-in/last-out 判定邏輯。"""

from datetime import date, datetime
from models.database import Employee, Attendance
from services.attendance_kiosk import resolve_punch_action


def _make_emp(session):
    emp = Employee(
        employee_id="E901",
        name="王老師",
        work_start_time="08:00",
        work_end_time="17:00",
        is_active=True,
    )
    session.add(emp)
    session.commit()
    return emp


def test_resolve_no_row_is_punch_in(test_db_session):
    emp = _make_emp(test_db_session)
    now = datetime(2026, 6, 30, 9, 0)
    p = resolve_punch_action(test_db_session, emp, now)
    assert p.action == "punch_in"
    assert p.will_overwrite is False
    assert p.employee_name == "王老師"


def test_resolve_has_in_only_is_punch_out(test_db_session):
    emp = _make_emp(test_db_session)
    test_db_session.add(
        Attendance(
            employee_id=emp.id,
            attendance_date=date(2026, 6, 30),
            punch_in_time=datetime(2026, 6, 30, 8, 0),
            status="normal",
        )
    )
    test_db_session.commit()
    p = resolve_punch_action(test_db_session, emp, datetime(2026, 6, 30, 17, 0))
    assert p.action == "punch_out"
    assert p.will_overwrite is False


def test_resolve_both_present_is_overwrite(test_db_session):
    emp = _make_emp(test_db_session)
    test_db_session.add(
        Attendance(
            employee_id=emp.id,
            attendance_date=date(2026, 6, 30),
            punch_in_time=datetime(2026, 6, 30, 8, 0),
            punch_out_time=datetime(2026, 6, 30, 12, 5),
            status="normal",
        )
    )
    test_db_session.commit()
    p = resolve_punch_action(test_db_session, emp, datetime(2026, 6, 30, 17, 30))
    assert p.action == "punch_out"
    assert p.will_overwrite is True
    assert p.current_punch_out == datetime(2026, 6, 30, 12, 5)
