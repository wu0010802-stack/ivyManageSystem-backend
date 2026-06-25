"""rank 1/7 回歸測試：請假↔加班核准路徑的跨模組衝突守衛。

破口：跨類衝突檢查（leave 查 OT、OT 查 leave）只掛在 create/update/import，
核准（approve / batch-approve）路徑兩端都不重查；create 守衛排除 REJECTED，
故「reject→重建→重核」可解除 create 互斥，最終同日同時段同時核准請假與加班
→ 扣請假薪 + 付加班費雙重給付。匯入路徑亦各漏對向檢查。

修補：approve / batch-approve 落地前各補一次「只查 approved 對向紀錄」的硬擋
（include_pending=False，避免兩張 pending 互鎖）；import 兩端補對向檢查。
"""

import os
import sys
import inspect
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import api.overtimes as overtimes_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from models.database import Base, Employee, LeaveRecord, OvertimeRecord, User
from utils.auth import hash_password
from unittest.mock import MagicMock

# 統一用週間工作日，避免 validate_leave_hours_against_schedule 對週末判 0 工時。
D = date(2026, 9, 15)  # 週二


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "cross-approve.sqlite"
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

    fake_salary_engine = MagicMock()
    monkeypatch.setattr(leaves_module, "_salary_engine", fake_salary_engine)
    monkeypatch.setattr(overtimes_module, "_salary_engine", fake_salary_engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)
    app.include_router(overtimes_router)

    with TestClient(app) as client:
        yield client, session_factory, monkeypatch

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _emp(session, employee_id="X001", name="員工"):
    e = Employee(employee_id=employee_id, name=name, base_salary=36000, is_active=True)
    session.add(e)
    session.flush()
    return e


def _admin(session, username="hr_admin"):
    u = User(
        employee_id=None,
        username=username,
        password_hash=hash_password("AdminPass123"),
        role="admin",
        permission_names=["*"],
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username="hr_admin", password="AdminPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _ot_dt(d, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m)


def _leave(
    session,
    employee_id,
    *,
    status,
    start_time="08:00",
    end_time="12:00",
    leave_hours=4.0,
):
    lv = LeaveRecord(
        employee_id=employee_id,
        leave_type="personal",
        start_date=D,
        end_date=D,
        start_time=start_time,
        end_time=end_time,
        leave_hours=leave_hours,
        status=status,
        is_deductible=True,
        deduction_ratio=1.0,
    )
    session.add(lv)
    session.flush()
    return lv


def _overtime(
    session, employee_id, *, status, start_time="08:00", end_time="12:00", hours=4.0
):
    ot = OvertimeRecord(
        employee_id=employee_id,
        overtime_date=D,
        start_time=_ot_dt(D, start_time),
        end_time=_ot_dt(D, end_time),
        hours=hours,
        overtime_type="weekday",
        status=status,
        use_comp_leave=False,
    )
    session.add(ot)
    session.flush()
    return ot


# ── rank 1：單筆核准路徑 ────────────────────────────────────────────────


def test_approve_pending_leave_blocked_by_approved_overtime(app_client):
    """同日同時段已有 approved 加班 → 核准 pending 請假應被擋（防雙重給付）。"""
    client, sf, mp = app_client
    with sf() as s:
        emp = _emp(s, "X001")
        _admin(s)
        _overtime(s, emp.id, status="approved")
        lv = _leave(s, emp.id, status="pending")
        s.commit()
        lv_id = lv.id

    assert _login(client).status_code == 200
    res = client.put(f"/api/leaves/{lv_id}/approve", json={"approved": True})
    assert (
        res.status_code == 409
    ), f"應被跨模組守衛擋下；實際 {res.status_code} {res.json()}"
    assert "加班" in res.json().get("detail", "")
    with sf() as s:
        assert s.get(LeaveRecord, lv_id).status == "pending"


def test_approve_rejected_leave_reactivation_blocked_by_approved_overtime(app_client):
    """reject→重建OT→重核加班 後，把被駁回的請假直接重核應被擋（核心繞過序列）。"""
    client, sf, mp = app_client
    with sf() as s:
        emp = _emp(s, "X002")
        _admin(s)
        _overtime(s, emp.id, status="approved")
        lv = _leave(s, emp.id, status="rejected")  # 已被駁回，模擬繞過後狀態
        s.commit()
        lv_id = lv.id

    assert _login(client).status_code == 200
    res = client.put(f"/api/leaves/{lv_id}/approve", json={"approved": True})
    assert (
        res.status_code == 409
    ), f"rejected→approve 仍須被擋；實際 {res.status_code} {res.json()}"
    assert "加班" in res.json().get("detail", "")


def test_approve_pending_overtime_blocked_by_approved_leave(app_client):
    """同日同時段已有 approved 請假 → 核准 pending 加班應被擋（反向對稱）。"""
    client, sf, mp = app_client
    with sf() as s:
        emp = _emp(s, "X003")
        _admin(s)
        _leave(s, emp.id, status="approved")
        ot = _overtime(s, emp.id, status="pending")
        s.commit()
        ot_id = ot.id

    assert _login(client).status_code == 200
    res = client.put(f"/api/overtimes/{ot_id}/approve", json={"approved": True})
    assert (
        res.status_code == 409
    ), f"應被跨模組守衛擋下；實際 {res.status_code} {res.json()}"
    assert "請假" in res.json().get("detail", "")
    with sf() as s:
        assert s.get(OvertimeRecord, ot_id).status == "pending"


def test_approve_leave_allowed_when_overtime_only_pending(app_client):
    """另一側只是 pending（尚未核准）時，核准本側不應被擋（避免兩張 pending 互鎖）。"""
    client, sf, mp = app_client
    with sf() as s:
        emp = _emp(s, "X004")
        _admin(s)
        _overtime(s, emp.id, status="pending")  # 加班僅待審
        lv = _leave(s, emp.id, status="pending")
        s.commit()
        lv_id = lv.id

    assert _login(client).status_code == 200
    res = client.put(f"/api/leaves/{lv_id}/approve", json={"approved": True})
    assert (
        res.status_code == 200
    ), f"對向僅 pending 不應互鎖；實際 {res.status_code} {res.json()}"


# ── rank 1：批次核准路徑 ────────────────────────────────────────────────


def test_batch_approve_leave_blocked_by_approved_overtime(app_client):
    """批次核准請假時，與 approved 加班同時段者該筆應落入 failed。"""
    client, sf, mp = app_client
    with sf() as s:
        emp = _emp(s, "X005")
        _admin(s)
        _overtime(s, emp.id, status="approved")
        lv = _leave(s, emp.id, status="pending")
        s.commit()
        lv_id = lv.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/leaves/batch-approve", json={"ids": [lv_id], "approved": True}
    )
    body = res.json()
    failed_ids = {f.get("id") for f in (body.get("failed") or [])}
    assert lv_id in failed_ids, f"應落 failed；body={body}"
    with sf() as s:
        assert s.get(LeaveRecord, lv_id).status == "pending"


def test_batch_approve_overtime_blocked_by_approved_leave(app_client):
    """批次核准加班時，與 approved 請假同時段者該筆應落入 failed。"""
    client, sf, mp = app_client
    with sf() as s:
        emp = _emp(s, "X006")
        _admin(s)
        _leave(s, emp.id, status="approved")
        ot = _overtime(s, emp.id, status="pending")
        s.commit()
        ot_id = ot.id

    assert _login(client).status_code == 200
    res = client.post(
        "/api/overtimes/batch-approve", json={"ids": [ot_id], "approved": True}
    )
    body = res.json()
    failed_ids = {f.get("id") for f in (body.get("failed") or [])}
    assert ot_id in failed_ids, f"應落 failed；body={body}"
    with sf() as s:
        assert s.get(OvertimeRecord, ot_id).status == "pending"


# ── rank 7：匯入路徑對向檢查（source inspection，比照既有慣例）─────────────


def test_import_paths_have_cross_module_guard():
    """leave import 須查對向加班；overtime import 須查對向請假。"""
    leave_src = inspect.getsource(leaves_module._import_leaves_sync)
    ot_src = inspect.getsource(overtimes_module._import_overtimes_sync)
    assert (
        "_check_employee_has_conflicting_overtime" in leave_src
    ), "_import_leaves_sync 必須查同時段加班（rank 7）"
    assert (
        "_check_employee_has_conflicting_leave" in ot_src
    ), "_import_overtimes_sync 必須查同時段請假（rank 1/7）"
