from models.database import PositionSalaryConfig, AttendancePolicy


def test_position_salary_config_has_config_year(test_db_session):
    s = test_db_session
    s.add(PositionSalaryConfig(config_year=2026, head_teacher_a=39240))
    s.flush()
    row = s.query(PositionSalaryConfig).first()
    assert row.config_year == 2026


def test_attendance_policy_has_config_year(test_db_session):
    s = test_db_session
    s.add(AttendancePolicy(config_year=2026, festival_bonus_months=3))
    s.flush()
    row = s.query(AttendancePolicy).first()
    assert row.config_year == 2026
