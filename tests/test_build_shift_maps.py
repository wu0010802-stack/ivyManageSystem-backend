from datetime import date
from models.database import Employee
from utils.attendance_shift_window import build_shift_maps_for_employee_date


def test_build_shift_maps_empty_when_no_shifts(test_db_session):
    emp = Employee(
        employee_id="E900",
        name="測試員工",
        work_start_time="08:00",
        work_end_time="17:00",
        is_active=True,
    )
    test_db_session.add(emp)
    test_db_session.commit()

    daily_map, week_map = build_shift_maps_for_employee_date(
        test_db_session, emp, date(2026, 6, 30)
    )
    assert daily_map == {}
    assert week_map == {}
