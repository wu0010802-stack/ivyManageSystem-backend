"""P1-1 回歸：_build_anomaly_rows 不可讀不存在的 emp.employee_number。

Employee 工號欄位名為 employee_id（String，comment="工號"），
anomalies.py 原本誤用 emp.employee_number → 只要該月有任一異常記錄，
GET /attendance/anomalies 與 /export 會 AttributeError → 500。
"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.attendance import Attendance, AttendanceStatus
from api.attendance.anomalies import _build_anomaly_rows


@pytest.fixture
def db_session():
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
def sample_employee(db_session):
    from models.employee import Employee

    emp = Employee(
        employee_id="T001",
        name="測試員工",
        base_salary=36000,
        is_active=True,
    )
    db_session.add(emp)
    db_session.commit()
    return emp


def test_build_anomaly_rows_uses_employee_id_as_number(db_session, sample_employee):
    """有遲到異常時應正常回傳 row，且 employee_number 來自 employee_id（工號）。"""
    att = Attendance(
        employee_id=sample_employee.id,
        attendance_date=date(2026, 5, 15),
        status=AttendanceStatus.LATE.value,
        is_late=True,
        late_minutes=30,
        punch_in_time=datetime.combine(date(2026, 5, 15), datetime.min.time()),
    )
    db_session.add(att)
    db_session.commit()

    rows = _build_anomaly_rows(db_session, 2026, 5, "all")

    assert len(rows) >= 1
    assert rows[0]["employee_number"] == "T001"
