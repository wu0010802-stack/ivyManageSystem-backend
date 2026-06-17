"""批次核准/駁回補打卡端點回歸測試。

覆蓋：
- test_batch_approve_updates_attendance_and_marks_succeeded：兩筆全通過，考勤被建立，status=approved
- test_batch_approve_partial_self_approval_goes_to_failed：self-approval 進 failed，其餘通過，DB 狀態正確
- test_batch_reject_requires_reason：空白 rejection_reason 400，補正常原因後 succeeded
"""

import os
import sys
from datetime import date, datetime

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
    db_path = tmp_path / "punch-correction-batch.sqlite"
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
    permission_names,
    employee_id: int | None = None,
) -> User:
    if isinstance(permission_names, str):
        permission_names = [permission_names]
    u = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=permission_names,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


def _emp_id(s, code):
    return s.query(Employee).filter(Employee.employee_id == code).first().id


def test_batch_approve_updates_attendance_and_marks_succeeded(punch_client):
    client, sf = punch_client
    with sf() as s:
        emp = _make_employee(s, employee_id="T001", name="教師甲")
        sup_emp = _make_employee(s, employee_id="S001", name="主管")
        _make_user(
            s,
            username="t1",
            role="teacher",
            permission_names=["ATTENDANCE_READ"],
            employee_id=emp.id,
        )
        _make_user(
            s,
            username="sup_ok",
            role="supervisor",
            permission_names=["APPROVALS", "ATTENDANCE_READ"],
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
        c1 = PunchCorrectionRequest(
            employee_id=emp.id,
            attendance_date=date(2026, 5, 6),
            correction_type="punch_in",
            requested_punch_in=datetime(2026, 5, 6, 8, 0),
            reason="忘刷",
            status="pending",
        )
        c2 = PunchCorrectionRequest(
            employee_id=emp.id,
            attendance_date=date(2026, 5, 7),
            correction_type="punch_in",
            requested_punch_in=datetime(2026, 5, 7, 8, 0),
            reason="忘刷",
            status="pending",
        )
        s.add_all([c1, c2])
        s.commit()
        ids = [c1.id, c2.id]
    assert _login(client, "sup_ok").status_code == 200
    res = client.post(
        "/api/punch-corrections/batch-approve", json={"ids": ids, "approved": True}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert sorted(body["succeeded"]) == sorted(ids)
    assert body["failed"] == []
    with sf() as s:
        eid = _emp_id(s, "T001")
        atts = s.query(Attendance).filter(Attendance.employee_id == eid).all()
        assert len(atts) == 2
        assert all(a.punch_in_time is not None for a in atts)
        corrs = (
            s.query(PunchCorrectionRequest)
            .filter(PunchCorrectionRequest.id.in_(ids))
            .all()
        )
        assert all(c.status == "approved" for c in corrs)


def test_batch_approve_partial_self_approval_goes_to_failed(punch_client):
    client, sf = punch_client
    with sf() as s:
        emp = _make_employee(s, employee_id="T001", name="教師甲")
        sup_emp = _make_employee(s, employee_id="S001", name="主管")
        _make_user(
            s,
            username="t1",
            role="teacher",
            permission_names=["ATTENDANCE_READ"],
            employee_id=emp.id,
        )
        _make_user(
            s,
            username="sup_ok",
            role="supervisor",
            permission_names=["APPROVALS", "ATTENDANCE_READ"],
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
        s.add(
            ApprovalPolicy(
                doc_type="punch_correction",
                submitter_role="supervisor",
                approver_roles="admin",
                is_active=True,
            )
        )
        good = PunchCorrectionRequest(
            employee_id=emp.id,
            attendance_date=date(2026, 5, 6),
            correction_type="punch_in",
            requested_punch_in=datetime(2026, 5, 6, 8, 0),
            reason="x",
            status="pending",
        )
        self_corr = PunchCorrectionRequest(
            employee_id=sup_emp.id,
            attendance_date=date(2026, 5, 6),
            correction_type="punch_in",
            requested_punch_in=datetime(2026, 5, 6, 8, 0),
            reason="x",
            status="pending",
        )
        s.add_all([good, self_corr])
        s.commit()
        good_id, self_id = good.id, self_corr.id
    assert _login(client, "sup_ok").status_code == 200
    res = client.post(
        "/api/punch-corrections/batch-approve",
        json={"ids": [good_id, self_id], "approved": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["succeeded"] == [good_id]
    assert len(body["failed"]) == 1 and body["failed"][0]["id"] == self_id
    with sf() as s:
        assert s.get(PunchCorrectionRequest, good_id).status == "approved"
        assert s.get(PunchCorrectionRequest, self_id).status == "pending"


def test_batch_reject_requires_reason(punch_client):
    client, sf = punch_client
    with sf() as s:
        emp = _make_employee(s, employee_id="T001", name="教師甲")
        sup_emp = _make_employee(s, employee_id="S001", name="主管")
        _make_user(
            s,
            username="t1",
            role="teacher",
            permission_names=["ATTENDANCE_READ"],
            employee_id=emp.id,
        )
        _make_user(
            s,
            username="sup_ok",
            role="supervisor",
            permission_names=["APPROVALS", "ATTENDANCE_READ"],
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
        c = PunchCorrectionRequest(
            employee_id=emp.id,
            attendance_date=date(2026, 5, 6),
            correction_type="punch_in",
            requested_punch_in=datetime(2026, 5, 6, 8, 0),
            reason="x",
            status="pending",
        )
        s.add(c)
        s.commit()
        cid = c.id
    assert _login(client, "sup_ok").status_code == 200
    bad = client.post(
        "/api/punch-corrections/batch-approve",
        json={"ids": [cid], "approved": False, "rejection_reason": "   "},
    )
    assert bad.status_code == 400
    ok = client.post(
        "/api/punch-corrections/batch-approve",
        json={"ids": [cid], "approved": False, "rejection_reason": "時間不符"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["succeeded"] == [cid]
    with sf() as s:
        assert s.get(PunchCorrectionRequest, cid).status == "rejected"
