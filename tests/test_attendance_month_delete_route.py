"""P1-6 回歸：刪除整月考勤端點不可被單筆刪除路由遮蔽。

原本月刪除註冊為 DELETE /records/{year}/{month}，與先註冊的
DELETE /records/{employee_id}/{date_str} 同為 2-segment，先註冊先匹配，
導致 /records/2019/3 命中單筆刪除 → strptime 失敗 → 400，月刪除永遠 unreachable。

修法：月刪除改用獨立 path /records/month/{year}/{month}（前端 deleteMonthRecords 同步）。
本測試鎖定新 path 能真正刪除整月記錄。
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
from models.database import Attendance, Employee, User
from utils.auth import hash_password


@pytest.fixture
def att_client(tmp_path):
    db_path = tmp_path / "att-month-delete.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
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


def test_month_delete_endpoint_reachable_and_deletes_month(att_client):
    """月刪除走獨立 path 應 200 並刪掉整月記錄（用 2019-03，已逾保存期可刪）。"""
    client, sf = att_client
    with sf() as s:
        emp_a = Employee(
            employee_id="E_md_a", name="員工A", base_salary=30000, is_active=True
        )
        emp_b = Employee(
            employee_id="E_md_b", name="員工B", base_salary=30000, is_active=True
        )
        s.add_all([emp_a, emp_b])
        s.flush()
        s.add_all(
            [
                Attendance(employee_id=emp_a.id, attendance_date=date(2019, 3, 5)),
                Attendance(employee_id=emp_a.id, attendance_date=date(2019, 3, 6)),
                Attendance(employee_id=emp_b.id, attendance_date=date(2019, 3, 5)),
            ]
        )
        admin = User(
            username="pure_admin_md",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
            employee_id=None,
            is_active=True,
            must_change_password=False,
        )
        s.add(admin)
        s.commit()

    assert _login(client, "pure_admin_md").status_code == 200

    res = client.delete("/api/attendance/records/month/2019/3")
    assert res.status_code == 200, res.text

    with sf() as s:
        remaining = (
            s.query(Attendance)
            .filter(
                Attendance.attendance_date >= date(2019, 3, 1),
                Attendance.attendance_date <= date(2019, 3, 31),
            )
            .count()
        )
    assert remaining == 0
