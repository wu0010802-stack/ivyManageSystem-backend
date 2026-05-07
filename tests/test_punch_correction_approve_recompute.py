"""審核補打卡時整套重算 Attendance 派生欄位（is_late / is_early_leave 等）回歸測試。

修補目標：approve PunchCorrectionRequest 後須以新的 punch_in/out 與員工排班
時間整套重算 is_late、late_minutes、is_early_leave、early_leave_minutes、status、
is_missing_*；否則舊值殘留會被薪資 engine
（services/salary/engine.py 與 salary_field_breakdown.py）讀到，造成補卡通過
卻仍扣遲到金的真實漏帳。

威脅：員工 09:30 上班打卡 → Attendance.is_late=True / late_minutes=90 →
申請補單 08:00 上班 → 主管核准 → punch_in_time 改 08:00 但 is_late=True /
late_minutes=90 殘留 → 薪資仍扣 90 分鐘遲到金。

Refs: 邏輯漏洞 audit 2026-05-07 P0 (#6)。
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.punch_corrections import router as punch_corrections_router
from models.database import (
    ApprovalPolicy,
    Attendance,
    Base,
    Employee,
    PunchCorrectionRequest,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def punch_client(tmp_path):
    db_path = tmp_path / "punch-correction-recompute.sqlite"
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
    app.include_router(punch_corrections_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_employee(
    session,
    *,
    employee_id: str,
    name: str,
    work_start: str = "08:00",
    work_end: str = "17:00",
) -> Employee:
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=True,
        work_start_time=work_start,
        work_end_time=work_end,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user(
    session,
    *,
    username: str,
    role: str,
    permissions: int,
    employee_id: int | None = None,
) -> User:
    u = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


APPROVALS_PERMS = int(Permission.APPROVALS) | int(Permission.ATTENDANCE_READ)


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


class TestApproveRecomputesAttendanceFields:
    """approve 後 Attendance 派生欄位必須整套重算。"""

    def test_approve_punch_in_clears_late_when_corrected_to_on_time(self, punch_client):
        """09:30 遲到 → 補單 08:00 → 核准 → is_late/late_minutes 清零。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_late", name="遲到員工")
            sup_emp = _make_employee(s, employee_id="E_sup", name="主管")
            _make_user(
                s,
                username="emp_late",
                role="teacher",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_ok",
                role="supervisor",
                permissions=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            policy = ApprovalPolicy(
                doc_type="punch_correction",
                submitter_role="teacher",
                approver_roles="supervisor,admin",
                is_active=True,
            )
            s.add(policy)
            # 既有 Attendance：09:30 遲到 90 分
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=datetime(on_date.year, on_date.month, on_date.day, 9, 30),
                punch_out_time=datetime(
                    on_date.year, on_date.month, on_date.day, 17, 0
                ),
                status="late",
                is_late=True,
                is_early_leave=False,
                is_missing_punch_in=False,
                is_missing_punch_out=False,
                late_minutes=90,
                early_leave_minutes=0,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_in",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 8, 0
                ),
                requested_punch_out=None,
                reason="忘了刷卡，實際 8:00 到",
                is_approved=None,
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_ok").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            assert att.punch_in_time == datetime(
                on_date.year, on_date.month, on_date.day, 8, 0
            )
            # 核心驗證：補單通過後遲到欄位必須清零
            assert att.is_late is False, "補卡通過後 is_late 仍為 True → 薪資會誤扣"
            assert (
                att.late_minutes == 0
            ), f"補卡通過後 late_minutes={att.late_minutes}，應為 0"
            assert att.status == "normal"
            assert att.is_missing_punch_in is False
            assert att.is_missing_punch_out is False

    def test_approve_punch_out_clears_early_leave(self, punch_client):
        """16:00 早退 → 補單 17:00 → 核准 → is_early_leave/early_leave_minutes 清零。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_early", name="早退員工")
            sup_emp = _make_employee(s, employee_id="E_sup2", name="主管2")
            _make_user(
                s,
                username="emp_early",
                role="teacher",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_ok2",
                role="supervisor",
                permissions=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=datetime(on_date.year, on_date.month, on_date.day, 8, 0),
                punch_out_time=datetime(
                    on_date.year, on_date.month, on_date.day, 16, 0
                ),
                status="early_leave",
                is_late=False,
                is_early_leave=True,
                is_missing_punch_in=False,
                is_missing_punch_out=False,
                late_minutes=0,
                early_leave_minutes=60,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_out",
                requested_punch_in=None,
                requested_punch_out=datetime(
                    on_date.year, on_date.month, on_date.day, 17, 0
                ),
                reason="忘了刷卡，實際 17:00 才下班",
                is_approved=None,
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_ok2").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            assert att.is_early_leave is False
            assert att.early_leave_minutes == 0
            assert att.status == "normal"

    def test_approve_both_recomputes_against_employee_schedule(self, punch_client):
        """correction_type='both' 整套重算，且依員工自訂上下班時間（09:00-18:00）。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(
                s,
                employee_id="E_custom",
                name="自訂排班",
                work_start="09:00",
                work_end="18:00",
            )
            sup_emp = _make_employee(s, employee_id="E_sup3", name="主管3")
            _make_user(
                s,
                username="emp_custom",
                role="teacher",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_ok3",
                role="supervisor",
                permissions=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            # 缺打卡狀態：punch_in / punch_out 都缺，is_late/is_early_leave 殘存值
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=None,
                punch_out_time=None,
                status="missing+late",
                is_late=True,  # 殘留錯誤值
                is_early_leave=True,  # 殘留錯誤值
                is_missing_punch_in=True,
                is_missing_punch_out=True,
                late_minutes=120,
                early_leave_minutes=30,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="both",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 9, 0
                ),
                requested_punch_out=datetime(
                    on_date.year, on_date.month, on_date.day, 18, 0
                ),
                reason="一整天都忘了刷卡",
                is_approved=None,
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_ok3").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            # 09:00 準時上班、18:00 準時下班 → 無遲到、無早退、無缺打卡
            assert att.is_late is False
            assert att.is_early_leave is False
            assert att.late_minutes == 0
            assert att.early_leave_minutes == 0
            assert att.is_missing_punch_in is False
            assert att.is_missing_punch_out is False
            assert att.status == "normal"

    def test_approve_punch_in_partial_still_late_recomputes_minutes(self, punch_client):
        """補單 08:30（仍遲到）→ 核准 → late_minutes 應從原 90 改為 30。"""
        client, sf = punch_client
        on_date = date.today() - timedelta(days=2)
        with sf() as s:
            emp = _make_employee(s, employee_id="E_partial", name="部分補正")
            sup_emp = _make_employee(s, employee_id="E_sup4", name="主管4")
            _make_user(
                s,
                username="emp_partial",
                role="teacher",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=emp.id,
            )
            _make_user(
                s,
                username="sup_ok4",
                role="supervisor",
                permissions=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            s.add(
                ApprovalPolicy(
                    doc_type="punch_correction",
                    submitter_role="teacher",
                    approver_roles="supervisor,admin",
                    is_active=True,
                )
            )
            att = Attendance(
                employee_id=emp.id,
                attendance_date=on_date,
                punch_in_time=datetime(on_date.year, on_date.month, on_date.day, 9, 30),
                punch_out_time=datetime(
                    on_date.year, on_date.month, on_date.day, 17, 0
                ),
                status="late",
                is_late=True,
                is_early_leave=False,
                is_missing_punch_in=False,
                is_missing_punch_out=False,
                late_minutes=90,
                early_leave_minutes=0,
            )
            s.add(att)
            corr = PunchCorrectionRequest(
                employee_id=emp.id,
                attendance_date=on_date,
                correction_type="punch_in",
                requested_punch_in=datetime(
                    on_date.year, on_date.month, on_date.day, 8, 30
                ),
                requested_punch_out=None,
                reason="實際 8:30 到，沒有 9:30 那麼晚",
                is_approved=None,
            )
            s.add(corr)
            s.commit()
            corr_id = corr.id
            emp_id = emp.id

        assert _login(client, "sup_ok4").status_code == 200
        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            att = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == emp_id,
                    Attendance.attendance_date == on_date,
                )
                .first()
            )
            assert att is not None
            assert att.is_late is True
            assert (
                att.late_minutes == 30
            ), f"08:30 仍遲到 30 分鐘但 late_minutes={att.late_minutes}"
            assert att.status == "late"
