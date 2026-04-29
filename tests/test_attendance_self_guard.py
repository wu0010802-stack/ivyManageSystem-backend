"""回歸測試：考勤類自我守衛缺口（F-041 / F-042 / F-046）

修補目標：員工不可透過 attendance 端點建立／修改／刪除自己的考勤紀錄，
即使持有 ATTENDANCE_WRITE 權限亦不可。對齊既有自我核准/守衛 idiom：
- api/overtimes.py:1078-1079
- api/leaves.py:1014-1018
- utils/finance_guards.require_not_self_*

涵蓋三 endpoint 群：
- F-041 attendance/records.py：POST /attendance/record、
  DELETE /attendance/record/{eid}/{date}、DELETE /attendance/records/{eid}/{date_str}
- F-042 attendance/anomalies.py：POST /attendance/anomalies/batch-confirm
- F-046 attendance/upload.py：POST /attendance/upload-csv（CSV 路徑為主，
  Excel /upload 共用同一 helper，契約等價）
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
from utils.permissions import Permission

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def att_client(tmp_path):
    """建立隔離的 sqlite 測試 app（attendance self-guard 用）。"""
    db_path = tmp_path / "att-self-guard.sqlite"
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


def _make_employee(session, *, employee_id: str, name: str) -> Employee:
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=30000,
        employee_type="regular",
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user(
    session,
    *,
    username: str,
    permissions: int,
    employee_id: int | None = None,
    role: str = "hr",
) -> User:
    user = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role=role,
        permissions=permissions,
        employee_id=employee_id,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


# 預設 ATTENDANCE 寫入權限組合
ATT_PERMS = int(Permission.ATTENDANCE_READ) | int(Permission.ATTENDANCE_WRITE)


# ══════════════════════════════════════════════════════════════════════
# F-041: api/attendance/records.py 三端點
# ══════════════════════════════════════════════════════════════════════


class TestF041AttendanceRecord:
    """三端點：POST /record、DELETE /record/{eid}/{date}、
    DELETE /records/{eid}/{date_str}。"""

    def test_hr_cannot_post_own_attendance(self, att_client):
        """hr 持 ATTENDANCE_WRITE 嘗試 POST 自己的考勤 → 403。"""
        client, sf = att_client
        with sf() as s:
            emp = _make_employee(s, employee_id="E_self", name="自我HR")
            _make_user(
                s,
                username="hr_self",
                permissions=ATT_PERMS,
                employee_id=emp.id,
                role="hr",
            )
            s.commit()
            self_eid = emp.id

        assert _login(client, "hr_self").status_code == 200

        res = client.post(
            "/api/attendance/record",
            json={
                "employee_id": self_eid,
                "date": "2026-04-15",
                "punch_in": "08:00",
                "punch_out": "17:00",
            },
        )
        assert res.status_code == 403, res.text
        assert "自己" in res.json()["detail"]

    def test_hr_cannot_delete_own_attendance_variant_record(self, att_client):
        """DELETE /attendance/record/{eid}/{date} 自我刪除 → 403。"""
        client, sf = att_client
        with sf() as s:
            emp = _make_employee(s, employee_id="E_del1", name="自我刪1")
            # 建立一筆考勤（即使 caller 是自己也應在守衛之前就被擋）
            s.add(
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(
                        2020, 1, 15
                    ),  # 已逾保存期，避免 retention 擋路
                )
            )
            _make_user(
                s,
                username="hr_del1",
                permissions=ATT_PERMS,
                employee_id=emp.id,
                role="hr",
            )
            s.commit()
            self_eid = emp.id

        assert _login(client, "hr_del1").status_code == 200

        res = client.delete(f"/api/attendance/record/{self_eid}/2020-01-15")
        assert res.status_code == 403, res.text
        assert "自己" in res.json()["detail"]

    def test_hr_cannot_delete_own_attendance_variant_records(self, att_client):
        """DELETE /attendance/records/{eid}/{date_str} 自我刪除 → 403。"""
        client, sf = att_client
        with sf() as s:
            emp = _make_employee(s, employee_id="E_del2", name="自我刪2")
            s.add(
                Attendance(
                    employee_id=emp.id,
                    attendance_date=date(2020, 1, 16),
                )
            )
            _make_user(
                s,
                username="hr_del2",
                permissions=ATT_PERMS,
                employee_id=emp.id,
                role="hr",
            )
            s.commit()
            self_eid = emp.id

        assert _login(client, "hr_del2").status_code == 200

        res = client.delete(f"/api/attendance/records/{self_eid}/2020-01-16")
        assert res.status_code == 403, res.text
        assert "自己" in res.json()["detail"]

    def test_hr_can_post_other_employees_attendance(self, att_client):
        """hr 寫他人考勤 → 通過（201）。"""
        client, sf = att_client
        with sf() as s:
            self_emp = _make_employee(s, employee_id="E_hr_ok", name="HR本人")
            other_emp = _make_employee(s, employee_id="E_other", name="他人")
            _make_user(
                s,
                username="hr_ok",
                permissions=ATT_PERMS,
                employee_id=self_emp.id,
                role="hr",
            )
            s.commit()
            other_eid = other_emp.id

        assert _login(client, "hr_ok").status_code == 200

        res = client.post(
            "/api/attendance/record",
            json={
                "employee_id": other_eid,
                "date": "2026-04-15",
                "punch_in": "08:00",
                "punch_out": "17:00",
            },
        )
        assert res.status_code in (200, 201), res.text

    def test_pure_admin_account_without_employee_id_can_post_anyone(self, att_client):
        """純管理員（user.employee_id is None）寫任何人 → 通過。"""
        client, sf = att_client
        with sf() as s:
            target = _make_employee(s, employee_id="E_target", name="目標員工")
            _make_user(
                s,
                username="pure_admin",
                permissions=ATT_PERMS,
                employee_id=None,
                role="admin",
            )
            s.commit()
            target_eid = target.id

        assert _login(client, "pure_admin").status_code == 200

        res = client.post(
            "/api/attendance/record",
            json={
                "employee_id": target_eid,
                "date": "2026-04-15",
                "punch_in": "08:00",
                "punch_out": "17:00",
            },
        )
        assert res.status_code in (200, 201), res.text

    def test_admin_with_employee_id_cannot_post_self(self, att_client):
        """admin 但綁了 employee_id 仍不可寫自己（守衛不豁免 admin）。"""
        client, sf = att_client
        with sf() as s:
            emp = _make_employee(s, employee_id="E_admin_self", name="管理員兼員工")
            _make_user(
                s,
                username="admin_self",
                permissions=ATT_PERMS,
                employee_id=emp.id,
                role="admin",
            )
            s.commit()
            self_eid = emp.id

        assert _login(client, "admin_self").status_code == 200

        res = client.post(
            "/api/attendance/record",
            json={
                "employee_id": self_eid,
                "date": "2026-04-15",
                "punch_in": "08:00",
                "punch_out": "17:00",
            },
        )
        assert res.status_code == 403, res.text
        assert "自己" in res.json()["detail"]


# ══════════════════════════════════════════════════════════════════════
# F-042: api/attendance/anomalies.py POST /anomalies/batch-confirm
# ══════════════════════════════════════════════════════════════════════


class TestF042AnomaliesBatchConfirm:
    def test_hr_cannot_batch_confirm_anomalies_containing_self(self, att_client):
        """批次內含 caller 自己的 attendance → 整批 403。"""
        client, sf = att_client
        with sf() as s:
            self_emp = _make_employee(s, employee_id="E_anom_self", name="異常自己")
            other_emp = _make_employee(s, employee_id="E_anom_other", name="異常他人")
            self_att = Attendance(
                employee_id=self_emp.id,
                attendance_date=date(2026, 4, 5),
                is_late=True,
                late_minutes=15,
            )
            other_att = Attendance(
                employee_id=other_emp.id,
                attendance_date=date(2026, 4, 6),
                is_late=True,
                late_minutes=20,
            )
            s.add_all([self_att, other_att])
            _make_user(
                s,
                username="hr_batch",
                permissions=ATT_PERMS,
                employee_id=self_emp.id,
                role="hr",
            )
            s.commit()
            self_att_id = self_att.id
            other_att_id = other_att.id

        assert _login(client, "hr_batch").status_code == 200

        res = client.post(
            "/api/attendance/anomalies/batch-confirm",
            json={
                "attendance_ids": [self_att_id, other_att_id],
                "action": "admin_waive",
            },
        )
        assert res.status_code == 403, res.text
        assert "自己" in res.json()["detail"]

    def test_hr_can_batch_confirm_when_self_not_included(self, att_client):
        """批次只含他人 → 通過（200）。"""
        client, sf = att_client
        with sf() as s:
            self_emp = _make_employee(s, employee_id="E_anom_ok_self", name="自己ok")
            other_emp = _make_employee(s, employee_id="E_anom_ok_other", name="他人ok")
            other_att = Attendance(
                employee_id=other_emp.id,
                attendance_date=date(2026, 4, 7),
                is_late=True,
                late_minutes=10,
            )
            s.add(other_att)
            _make_user(
                s,
                username="hr_batch_ok",
                permissions=ATT_PERMS,
                employee_id=self_emp.id,
                role="hr",
            )
            s.commit()
            other_att_id = other_att.id

        assert _login(client, "hr_batch_ok").status_code == 200

        res = client.post(
            "/api/attendance/anomalies/batch-confirm",
            json={"attendance_ids": [other_att_id], "action": "admin_waive"},
        )
        assert res.status_code == 200, res.text

    def test_pure_admin_without_employee_id_unrestricted(self, att_client):
        """純 admin（無 employee_id）不受守衛限制。"""
        client, sf = att_client
        with sf() as s:
            emp = _make_employee(s, employee_id="E_anom_pure", name="目標A")
            att = Attendance(
                employee_id=emp.id,
                attendance_date=date(2026, 4, 8),
                is_late=True,
                late_minutes=5,
            )
            s.add(att)
            _make_user(
                s,
                username="pure_admin_anom",
                permissions=ATT_PERMS,
                employee_id=None,
                role="admin",
            )
            s.commit()
            att_id = att.id

        assert _login(client, "pure_admin_anom").status_code == 200

        res = client.post(
            "/api/attendance/anomalies/batch-confirm",
            json={"attendance_ids": [att_id], "action": "admin_waive"},
        )
        assert res.status_code == 200, res.text


# ══════════════════════════════════════════════════════════════════════
# F-046: api/attendance/upload.py（以 /upload-csv 為主）
# ══════════════════════════════════════════════════════════════════════


class TestF046AttendanceUpload:
    def test_csv_upload_with_self_row_rejected(self, att_client):
        """CSV 內含 caller 自己列 → 整批 403。"""
        client, sf = att_client
        with sf() as s:
            self_emp = _make_employee(s, employee_id="E_csv_self", name="CSV自己")
            other_emp = _make_employee(s, employee_id="E_csv_other", name="CSV他人")
            _make_user(
                s,
                username="hr_csv",
                permissions=ATT_PERMS,
                employee_id=self_emp.id,
                role="hr",
            )
            s.commit()

        assert _login(client, "hr_csv").status_code == 200

        res = client.post(
            "/api/attendance/upload-csv",
            json={
                "year": 2026,
                "month": 4,
                "records": [
                    {
                        "department": "教學",
                        "employee_number": "E_csv_self",
                        "name": "CSV自己",
                        "date": "2026/04/10",
                        "weekday": "五",
                        "punch_in": "08:00",
                        "punch_out": "17:00",
                    },
                    {
                        "department": "教學",
                        "employee_number": "E_csv_other",
                        "name": "CSV他人",
                        "date": "2026/04/10",
                        "weekday": "五",
                        "punch_in": "08:00",
                        "punch_out": "17:00",
                    },
                ],
            },
        )
        assert res.status_code == 403, res.text
        assert "自己" in res.json()["detail"]

    def test_csv_upload_without_self_row_proceeds(self, att_client):
        """CSV 內無 caller 自己列 → 通過。"""
        client, sf = att_client
        with sf() as s:
            self_emp = _make_employee(s, employee_id="E_csv_ok_self", name="ok自己")
            other_emp = _make_employee(s, employee_id="E_csv_ok_other", name="ok他人")
            _make_user(
                s,
                username="hr_csv_ok",
                permissions=ATT_PERMS,
                employee_id=self_emp.id,
                role="hr",
            )
            s.commit()

        assert _login(client, "hr_csv_ok").status_code == 200

        res = client.post(
            "/api/attendance/upload-csv",
            json={
                "year": 2026,
                "month": 4,
                "records": [
                    {
                        "department": "教學",
                        "employee_number": "E_csv_ok_other",
                        "name": "ok他人",
                        "date": "2026/04/11",
                        "weekday": "六",
                        "punch_in": "08:00",
                        "punch_out": "17:00",
                    },
                ],
            },
        )
        assert res.status_code == 200, res.text

    def test_pure_admin_without_employee_id_unrestricted(self, att_client):
        """純 admin（無 employee_id）上傳含任何員工列 → 通過。"""
        client, sf = att_client
        with sf() as s:
            target = _make_employee(s, employee_id="E_csv_pure", name="目標CSV")
            _make_user(
                s,
                username="pure_admin_csv",
                permissions=ATT_PERMS,
                employee_id=None,
                role="admin",
            )
            s.commit()

        assert _login(client, "pure_admin_csv").status_code == 200

        res = client.post(
            "/api/attendance/upload-csv",
            json={
                "year": 2026,
                "month": 4,
                "records": [
                    {
                        "department": "教學",
                        "employee_number": "E_csv_pure",
                        "name": "目標CSV",
                        "date": "2026/04/12",
                        "weekday": "日",
                        "punch_in": "08:00",
                        "punch_out": "17:00",
                    },
                ],
            },
        )
        assert res.status_code == 200, res.text
