"""F-C 回歸：刪除帶 leave_record_id 的考勤列會造成「請假扣款 0 且不算曠職」的全薪漏洞。

機制：薪資請假扣款走 att_leave_pairs（JOIN Attendance.leave_record_id ↔ LeaveRecord）
→ 刪掉 att 列即無 pair → 扣款 0；但曠職偵測獨立讀 approved_leaves（LeaveRecord）
→ 假單仍 approved → leave_covered → 不算曠職。於是 deductible 全日假變全薪。

修法（approach B）：拒絕刪除帶 leave 連結的考勤列，回可操作 409 錯誤
（指示先處理假單），讓請假對考勤的影響無法被「刪考勤」單側繞過。

用 2019 年日期（逾 5 年保存期，retention 守衛不會搶先擋）隔離 F-C 行為。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.attendance import router as attendance_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.base import Base
from models.attendance import Attendance, AttendanceStatus
from models.database import Employee, User
from models.leave import LeaveRecord
from utils.auth import hash_password


@pytest.fixture
def att_client(tmp_path):
    db_path = tmp_path / "att-del-leave.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(attendance_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login(client, username):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


def _seed(sf):
    """建員工 + 一筆已核可全日無薪事假 + 對應 status=LEAVE 的 Attendance（leave_record_id）。"""
    with sf() as s:
        emp = Employee(
            employee_id="E_dl", name="員工DL", base_salary=30000, is_active=True
        )
        s.add(emp)
        s.flush()
        lv = LeaveRecord(
            employee_id=emp.id,
            leave_type="personal",
            start_date=date(2019, 3, 5),
            end_date=date(2019, 3, 5),
            leave_hours=8.0,
            status="approved",
        )
        s.add(lv)
        s.flush()
        att = Attendance(
            employee_id=emp.id,
            attendance_date=date(2019, 3, 5),
            status=AttendanceStatus.LEAVE.value,
            leave_record_id=lv.id,
        )
        s.add(att)
        admin = User(
            username="pure_admin_dl",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
            employee_id=None,
            is_active=True,
            must_change_password=False,
        )
        s.add(admin)
        s.commit()
        return emp.id, lv.id


def _att_count(sf, emp_id):
    with sf() as s:
        return (
            s.query(Attendance)
            .filter(
                Attendance.employee_id == emp_id,
                Attendance.attendance_date == date(2019, 3, 5),
            )
            .count()
        )


class TestDeleteLeaveLinkedRejected:
    def test_delete_record_endpoint_rejects_leave_linked(self, att_client):
        """DELETE /attendance/record/{emp}/{date}：帶 leave 連結 → 拒刪，att 列保留。"""
        client, sf = att_client
        emp_id, _ = _seed(sf)
        assert _login(client, "pure_admin_dl").status_code == 200

        res = client.delete(f"/api/attendance/record/{emp_id}/2019-03-05")
        assert res.status_code == 409, res.text
        # att 列未被刪 → 薪資 pair 仍在 → 扣款不會歸零
        assert _att_count(sf, emp_id) == 1

    def test_delete_records_endpoint_rejects_leave_linked(self, att_client):
        """DELETE /attendance/records/{emp}/{date}：帶 leave 連結 → 拒刪。"""
        client, sf = att_client
        emp_id, _ = _seed(sf)
        assert _login(client, "pure_admin_dl").status_code == 200

        res = client.delete(f"/api/attendance/records/{emp_id}/2019-03-05")
        assert res.status_code == 409, res.text
        assert _att_count(sf, emp_id) == 1

    def test_month_delete_rejects_leave_linked(self, att_client):
        """DELETE /attendance/records/month/{year}/{month}：含 leave 連結列 → 拒刪整月。"""
        client, sf = att_client
        emp_id, _ = _seed(sf)
        assert _login(client, "pure_admin_dl").status_code == 200

        res = client.delete("/api/attendance/records/month/2019/3")
        assert res.status_code == 409, res.text
        assert _att_count(sf, emp_id) == 1
