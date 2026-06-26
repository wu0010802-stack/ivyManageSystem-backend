"""F-D 回歸：admin_waive 後重新上傳/補打卡/編輯不清 confirmed_action → 新遲到永久豁免。

anomalies.py 寫 confirmed_action='admin_waive'；薪資 is_attendance_waived 讀此欄
整日跳過遲到/早退/缺卡。但 upload/records/punch_corrections 在 in-place 覆寫該列
punch/旗標時從不重置 confirmed_action → 先豁免、後改寫成有真實遲到的打卡仍被豁免。

修法：這些「實質改變 punch 或遲到/早退/缺卡旗標」的寫入路徑，偵測到實質變動時
重置 confirmed_action/confirmed_by/confirmed_at（讓新異常重新需要確認）。只在
實質變動時重置，避免冪等重算誤清。
"""

import os
import sys
from datetime import date, datetime, timedelta

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
from api.punch_corrections import router as punch_corrections_router
from models.base import Base
from models.attendance import Attendance
from models.database import ApprovalPolicy, Employee, PunchCorrectionRequest, User
from utils.auth import hash_password
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def client_sf(tmp_path):
    db_path = tmp_path / "waive-reset.sqlite"
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
    app.include_router(punch_corrections_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login(client, username, pw="Passw0rd!"):
    return client.post("/api/auth/login", json={"username": username, "password": pw})


def _emp(s, **kw):
    emp = Employee(
        employee_id=kw["employee_id"],
        name=kw["name"],
        base_salary=36000,
        is_active=True,
        work_start_time=kw.get("work_start", "08:00"),
        work_end_time=kw.get("work_end", "17:00"),
    )
    s.add(emp)
    s.flush()
    return emp


def _user(s, *, username, role, perms, employee_id=None):
    u = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=perms if isinstance(perms, list) else [perms],
        is_active=True,
        must_change_password=False,
    )
    s.add(u)
    s.flush()
    return u


class TestRecordEditResetsWaive:
    def test_record_edit_to_late_clears_admin_waive(self, client_sf):
        """單筆編輯把打卡改成遲到 90 分 → admin_waive 應被清，薪資不再整日豁免。"""
        client, sf = client_sf
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _emp(s, employee_id="E_rec", name="員工Rec")
            _user(
                s,
                username="admin_rec",
                role="admin",
                perms=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
                employee_id=None,
            )
            # 既有：缺卡被 admin_waive 豁免
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                status="missing_punch_in",
                is_missing_punch_in=True,
                confirmed_action="admin_waive",
                confirmed_by="admin_rec",
                confirmed_at=now_taipei_naive(),
            )
            s.add(att)
            s.commit()
            emp_id = emp.id

        assert _login(client, "admin_rec").status_code == 200
        # 編輯打卡為 09:30 上班（遲到 90 分）
        res = client.post(
            "/api/attendance/record",
            json={
                "employee_id": emp_id,
                "date": on_date.isoformat(),
                "punch_in": "09:30",
                "punch_out": "17:00",
            },
        )
        assert res.status_code == 201, res.text

        with sf() as s:
            from services.salary.utils import is_attendance_waived

            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att.is_late is True
            assert att.late_minutes == 90
            # 核心：admin_waive 應被清，否則新遲到被永久豁免
            assert att.confirmed_action is None
            assert is_attendance_waived(att) is False


class TestPunchCorrectionResetsWaive:
    def test_approve_correction_to_late_clears_admin_waive(self, client_sf):
        """補打卡核准把打卡改成遲到 → admin_waive 應被清。"""
        client, sf = client_sf
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _emp(s, employee_id="E_pc", name="員工PC")
            sup = _emp(s, employee_id="E_pc_sup", name="主管PC")
            _user(
                s,
                username="emp_pc",
                role="teacher",
                perms=["ATTENDANCE_READ"],
                employee_id=emp.id,
            )
            _user(
                s,
                username="sup_pc",
                role="supervisor",
                perms=["APPROVALS", "ATTENDANCE_READ"],
                employee_id=sup.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            # 既有：缺卡 admin_waive
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                status="missing_punch_in",
                is_missing_punch_in=True,
                confirmed_action="admin_waive",
                confirmed_by="someone",
                confirmed_at=now_taipei_naive(),
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_in",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 9, 30
                ),
                requested_punch_out=None,
                reason="補上班打卡 09:30",
                status="pending",
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_pc").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve", json={"approved": True}
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            from services.salary.utils import is_attendance_waived

            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att.is_late is True
            # 核心：補卡核准改寫成遲到後 admin_waive 應被清
            assert att.confirmed_action is None
            assert is_attendance_waived(att) is False


class TestUploadResetsWaive:
    def test_csv_upload_to_late_clears_admin_waive(self, client_sf):
        """CSV 覆寫既有 admin_waive 列成遲到 → confirmed_action 應被清。"""
        client, sf = client_sf
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _emp(s, employee_id="E_csv", name="員工CSV")
            _user(
                s,
                username="admin_csv",
                role="admin",
                perms=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
                employee_id=None,
            )
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                status="missing_punch_in",
                is_missing_punch_in=True,
                confirmed_action="admin_waive",
                confirmed_by="someone",
                confirmed_at=now_taipei_naive(),
            )
            s.add(att)
            s.commit()
            emp_id = emp.id
            emp_number = emp.employee_id

        assert _login(client, "admin_csv").status_code == 200
        res = client.post(
            "/api/attendance/upload-csv",
            json={
                "year": on_date.year,
                "month": on_date.month,
                "records": [
                    {
                        "employee_number": emp_number,
                        "name": "員工CSV",
                        "department": "教學",
                        "date": on_date.isoformat(),
                        "weekday": "一",
                        "punch_in": "09:30",
                        "punch_out": "17:00",
                    }
                ],
            },
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            from services.salary.utils import is_attendance_waived

            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att.is_late is True
            assert att.confirmed_action is None
            assert is_attendance_waived(att) is False
