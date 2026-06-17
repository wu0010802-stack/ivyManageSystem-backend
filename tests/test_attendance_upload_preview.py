"""TDD：考勤匯入預覽端點（唯讀，不寫 DB）

POST /api/attendance/upload/preview
- 解析 raw_text（TSV/CSV 含標題列）
- 逐列分類：importable / employee_not_found / invalid_date / month_finalized / overwrite
- 回傳 summary + rows + normalized（僅 importable/overwrite）
- 確認不寫入 Attendance 表
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
from api.auth import _account_failures, _ip_attempts, router as auth_router
from models.base import Base
from models.database import Employee, User, Attendance
from models.salary import SalaryRecord
from utils.auth import hash_password
from utils.cache_layer import reset_cache_for_testing


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'p.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_e, old_s = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    reset_cache_for_testing()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(attendance_router)
    with TestClient(app) as c:
        yield c, sf
    _ip_attempts.clear()
    _account_failures.clear()
    reset_cache_for_testing()
    base_module._engine, base_module._SessionFactory = old_e, old_s
    engine.dispose()


def _login(c, sf):
    with sf() as s:
        s.add(
            Employee(
                employee_id="E01",
                name="王小明",
                base_salary=30000,
                is_active=True,
            )
        )
        s.add(
            User(
                username="pure_admin",
                password_hash=hash_password("Temp123456"),
                role="admin",
                permission_names=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
                employee_id=None,
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
    assert (
        c.post(
            "/api/auth/login",
            json={"username": "pure_admin", "password": "Temp123456"},
        ).status_code
        == 200
    )


def test_preview_classifies_rows(client):
    c, sf = client
    _login(c, sf)
    raw = "\n".join(
        [
            "部門\t編號\t姓名\t日期\t星期\t上班時間\t下班時間",
            "教學\tE01\t王小明\t2026/02/03\t二\t08:00\t17:00",
            "教學\tE99\t查無人\t2026/02/03\t二\t08:00\t17:00",
            "教學\tE01\t王小明\t2026/02/30\t六\t08:00\t17:00",
        ]
    )
    res = c.post(
        "/api/attendance/upload/preview",
        json={"raw_text": raw},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    checks = [r["check"] for r in body["rows"]]
    assert checks == ["importable", "employee_not_found", "invalid_date"]
    assert body["summary"]["importable"] == 1
    assert body["summary"]["problems"] == 2
    assert len(body["normalized"]) == 1
    assert body["rows"][0]["matched_employee_id"] is not None


def test_preview_does_not_write(client):
    c, sf = client
    _login(c, sf)
    raw = "編號\t姓名\t日期\t上班時間\t下班時間\nE01\t王小明\t2026/02/03\t08:00\t17:00"
    c.post(
        "/api/attendance/upload/preview",
        json={"raw_text": raw},
    )
    with sf() as s:
        assert s.query(Attendance).count() == 0


def test_preview_overwrite(client):
    """已有同員工同日出勤記錄時，preview 應標 overwrite 並計入 summary。"""
    c, sf = client
    _login(c, sf)
    # 先在 DB 建一筆該員工該日的 Attendance
    with sf() as s:
        emp = s.query(Employee).filter_by(employee_id="E01").one()
        s.add(
            Attendance(
                employee_id=emp.id,
                attendance_date=date(2026, 3, 5),
                punch_in_time=None,
                punch_out_time=None,
                status="present",
            )
        )
        s.commit()
    raw = "編號\t姓名\t日期\t上班時間\t下班時間\nE01\t王小明\t2026/03/05\t08:00\t17:00"
    res = c.post("/api/attendance/upload/preview", json={"raw_text": raw})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rows"][0]["check"] == "overwrite"
    assert body["summary"]["overwrites"] == 1
    # overwrite 列仍應進 normalized
    assert len(body["normalized"]) == 1


def test_preview_month_finalized(client):
    """該月薪資已封存時，preview 應標 month_finalized、計入 problems、不進 normalized。"""
    c, sf = client
    _login(c, sf)
    # 建一筆已封存的 SalaryRecord
    with sf() as s:
        emp = s.query(Employee).filter_by(employee_id="E01").one()
        s.add(
            SalaryRecord(
                employee_id=emp.id,
                salary_year=2026,
                salary_month=4,
                gross_salary=0,
                total_deduction=0,
                net_salary=0,
                is_finalized=True,
            )
        )
        s.commit()
    raw = "編號\t姓名\t日期\t上班時間\t下班時間\nE01\t王小明\t2026/04/10\t08:00\t17:00"
    res = c.post("/api/attendance/upload/preview", json={"raw_text": raw})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rows"][0]["check"] == "month_finalized"
    assert body["summary"]["problems"] == 1
    # month_finalized 不進 normalized
    assert len(body["normalized"]) == 0
