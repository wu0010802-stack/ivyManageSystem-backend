"""單月假單在封存月份觸發 409 的 route-level 回歸測試。

T3（codebase review 2026-05-14）：
    既有 `test_leaves_finalized_guard.py` 只用 MagicMock 測 helper；
    `test_leaves_crossmonth_finalized_guard.py` 涵蓋跨月場景。但
    「單月假單落在已封存月份」是最常見的 route 路徑（管理員想改／刪／核准
    封存月份的單筆假單），需要 route-level 整合測試擋住 silent 200。

涵蓋三條 route：
    - PUT    /api/leaves/{id}         （update_leave）
    - DELETE /api/leaves/{id}         （delete_leave）
    - PUT    /api/leaves/{id}/approve （approve_leave）

每條 route 都應在偵測封存月份時回 409、且 DB 維持原狀（假單未被改動）。
"""

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from models.database import (
    Base,
    Employee,
    LeaveRecord,
    SalaryRecord,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "singlemonth-finalize.sqlite"
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

    fake_salary_engine = MagicMock()
    monkeypatch.setattr(leaves_module, "_salary_engine", fake_salary_engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _emp(session, employee_id: str, name: str) -> Employee:
    e = Employee(employee_id=employee_id, name=name, base_salary=36000, is_active=True)
    session.add(e)
    session.flush()
    return e


def _admin(session) -> User:
    u = User(
        employee_id=None,
        username="hr_admin",
        password_hash=hash_password("AdminPass123"),
        role="admin",
        permission_names=["*"],
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client: TestClient):
    return client.post(
        "/api/auth/login",
        json={"username": "hr_admin", "password": "AdminPass123"},
    )


def _make_finalized_salary(session, employee_id: int, year: int, month: int):
    rec = SalaryRecord(
        employee_id=employee_id,
        salary_year=year,
        salary_month=month,
        is_finalized=True,
        finalized_by="HR",
        finalized_at=datetime(year, month, 28),
    )
    session.add(rec)
    session.flush()
    return rec


def _make_singlemonth_leave(
    session,
    employee_id: int,
    *,
    start: date,
    end: date,
    is_approved=None,
    leave_hours: float = 4.0,
):
    """直接以 ORM 建單月假單（start.month == end.month）。"""
    assert (
        start.month == end.month and start.year == end.year
    ), "_make_singlemonth_leave 限定同年同月,跨月測試請用 crossmonth fixture"
    lv = LeaveRecord(
        employee_id=employee_id,
        leave_type="personal",
        start_date=start,
        end_date=end,
        leave_hours=leave_hours,
        is_approved=is_approved,
        is_deductible=True,
        deduction_ratio=1.0,
    )
    session.add(lv)
    session.flush()
    return lv


# ── PUT /leaves/{id}：編輯封存月份的單月已核准假單 ─────────────────────────


class TestUpdateLeaveSingleMonthFinalizedGuard:
    def test_update_blocks_when_month_finalized(self, app_client):
        client, session_factory = app_client
        with session_factory() as s:
            emp = _emp(s, "S001", "編輯教師")
            _admin(s)
            # 2026/3 單月假單,已核准
            lv = _make_singlemonth_leave(
                s,
                emp.id,
                start=date(2026, 3, 10),
                end=date(2026, 3, 10),
                is_approved=True,
                leave_hours=4.0,
            )
            _make_finalized_salary(s, emp.id, 2026, 3)
            s.commit()
            lv_id = lv.id

        assert _login(client).status_code == 200

        # 嘗試把假單時數從 4.0 改為 8.0,仍落在已封存的 3 月
        res = client.put(
            f"/api/leaves/{lv_id}",
            json={"leave_hours": 8.0},
        )
        assert (
            res.status_code == 409
        ), f"封存月份(3 月)編輯應被擋；實際 status={res.status_code} body={res.json()}"
        assert "封存" in res.json()["detail"]

        # 確認 DB 未被改動(時數仍 4.0,is_approved 仍 True)
        with session_factory() as s:
            db_lv = s.query(LeaveRecord).filter_by(id=lv_id).one()
            assert db_lv.leave_hours == 4.0
            assert db_lv.is_approved is True


# ── DELETE /leaves/{id}：刪除封存月份的單月已核准假單 ─────────────────────


class TestDeleteLeaveSingleMonthFinalizedGuard:
    def test_delete_blocks_when_month_finalized(self, app_client):
        client, session_factory = app_client
        with session_factory() as s:
            emp = _emp(s, "S002", "刪除教師")
            _admin(s)
            lv = _make_singlemonth_leave(
                s,
                emp.id,
                start=date(2026, 3, 15),
                end=date(2026, 3, 15),
                is_approved=True,
            )
            _make_finalized_salary(s, emp.id, 2026, 3)
            s.commit()
            lv_id = lv.id

        assert _login(client).status_code == 200

        res = client.delete(f"/api/leaves/{lv_id}")
        assert (
            res.status_code == 409
        ), f"封存月份(3 月)刪除應被擋；實際 status={res.status_code} body={res.json()}"
        assert "封存" in res.json()["detail"]

        # DB 假單仍應存在
        with session_factory() as s:
            db_lv = s.query(LeaveRecord).filter_by(id=lv_id).one_or_none()
            assert db_lv is not None, "假單不應被刪除"


# ── PUT /leaves/{id}/approve：核准封存月份的單月 pending 假單 ──────────────


class TestApproveLeaveSingleMonthFinalizedGuard:
    def test_approve_blocks_when_month_finalized(self, app_client):
        client, session_factory = app_client
        with session_factory() as s:
            emp = _emp(s, "S003", "核准教師")
            _admin(s)
            # pending 假單
            lv = _make_singlemonth_leave(
                s,
                emp.id,
                start=date(2026, 3, 20),
                end=date(2026, 3, 20),
                is_approved=None,
            )
            _make_finalized_salary(s, emp.id, 2026, 3)
            s.commit()
            lv_id = lv.id

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/leaves/{lv_id}/approve",
            json={"approved": True},
        )
        assert (
            res.status_code == 409
        ), f"封存月份(3 月)核准應被擋；實際 status={res.status_code} body={res.json()}"

        # 假單應仍為 pending
        with session_factory() as s:
            db_lv = s.query(LeaveRecord).filter_by(id=lv_id).one()
            assert (
                db_lv.is_approved is None
            ), f"封存月份核准被擋後 is_approved 應仍為 None,實際 {db_lv.is_approved}"
