from datetime import date, timedelta
from models.database import Employee
from models.shift import DailyShift, ShiftAssignment, ShiftType
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


def test_build_shift_maps_daily_shift(test_db_session):
    """案例 A：有 DailyShift 時，daily_map 回傳班別資訊，shift_schedule_map 為空。"""
    emp = Employee(
        employee_id="E901",
        name="日班測試員工",
        work_start_time="08:00",
        work_end_time="17:00",
        is_active=True,
    )
    test_db_session.add(emp)
    test_db_session.flush()

    st = ShiftType(name="早班", work_start="07:00", work_end="15:00")
    test_db_session.add(st)
    test_db_session.flush()

    attendance_date = date(2026, 6, 30)
    ds = DailyShift(employee_id=emp.id, shift_type_id=st.id, date=attendance_date)
    test_db_session.add(ds)
    test_db_session.commit()

    daily_map, shift_schedule_map = build_shift_maps_for_employee_date(
        test_db_session, emp, attendance_date
    )

    assert shift_schedule_map == {}
    assert daily_map[(emp.id, attendance_date)] == {
        "work_start": st.work_start,
        "work_end": st.work_end,
        "name": st.name,
    }


def test_build_shift_maps_shift_assignment(test_db_session):
    """案例 B：有 ShiftAssignment 時，shift_schedule_map 回傳班別資訊，daily_map 為空。"""
    emp = Employee(
        employee_id="E902",
        name="週排班測試員工",
        work_start_time="08:00",
        work_end_time="17:00",
        is_active=True,
    )
    test_db_session.add(emp)
    test_db_session.flush()

    st = ShiftType(name="晚班", work_start="13:00", work_end="21:00")
    test_db_session.add(st)
    test_db_session.flush()

    attendance_date = date(2026, 6, 30)
    week_start = attendance_date - timedelta(days=attendance_date.weekday())
    sa = ShiftAssignment(
        employee_id=emp.id, shift_type_id=st.id, week_start_date=week_start
    )
    test_db_session.add(sa)
    test_db_session.commit()

    daily_map, shift_schedule_map = build_shift_maps_for_employee_date(
        test_db_session, emp, attendance_date
    )

    assert daily_map == {}
    assert shift_schedule_map[(emp.id, week_start)] == {
        "work_start": st.work_start,
        "work_end": st.work_end,
        "name": st.name,
    }
