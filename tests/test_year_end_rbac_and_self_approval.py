"""年終 router 權限守衛測試（C7 / C8，sec-batch-2026-06-16）。

C7：整個 year_end_router 須走 require_staff_permission，role=teacher 即使誤持
    YEAR_END_* 權限也不得撞管理端（金額級）API。
C8：add_special_bonus / manual_patch_settlement 兩個寫金額端點須有自我核准守衛，
    操作者不可對自己 employee_id 對應的 settlement 寫金額。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module  # noqa: E402
from api.auth import _account_failures, _ip_attempts  # noqa: E402
from api.auth import router as auth_router  # noqa: E402
from api.year_end import year_end_router  # noqa: E402
from models.database import Base, User  # noqa: E402
from models.employee import Employee  # noqa: E402
from models.year_end import (  # noqa: E402
    EmployeeYearEndSnapshot,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from utils.auth import hash_password  # noqa: E402

WRITE_PERMS = ["YEAR_END_READ", "YEAR_END_WRITE", "YEAR_END_FINALIZE"]


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "year-end-rbac-test.sqlite"
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
    app.include_router(year_end_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login(client, username, password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, f"login failed: {res.text}"


def _seed_cycle_and_employee(sf, *, emp_id_number="A123456789"):
    with sf() as s:
        emp = Employee(
            employee_id="E_RBAC_001",
            name="王老師",
            id_number=emp_id_number,
            is_active=True,
        )
        s.add(emp)
        s.flush()
        cycle = YearEndCycle(
            academic_year=114,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 7, 31),
            bonus_calc_date=date(2026, 1, 15),
            status=YearEndCycleStatus.OPEN,
        )
        s.add(cycle)
        s.flush()
        snapshot = EmployeeYearEndSnapshot(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            base_salary=Decimal("40000"),
            festival_total=Decimal("0"),
            hire_months=Decimal("12"),
        )
        s.add(snapshot)
        s.flush()
        settlement = YearEndSettlement(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            snapshot_id=snapshot.id,
            payable_amount=Decimal("10000"),
            special_bonus_total=Decimal("0"),
            total_amount=Decimal("10000"),
            status=YearEndSettlementStatus.DRAFT,
        )
        s.add(settlement)
        s.flush()
        s.commit()
        return cycle.id, emp.id, settlement.id


# ===== C7：teacher 不得撞管理端年終 API =====


def test_teacher_blocked_from_year_end_settlements(client_with_db):
    """role=teacher 持 YEAR_END_READ 仍應被擋出 GET settlements（403）。"""
    client, sf = client_with_db
    cycle_id, _emp_id, _sid = _seed_cycle_and_employee(sf)
    with sf() as s:
        s.add(
            User(
                username="teacher",
                password_hash=hash_password("TempPass123"),
                role="teacher",
                permission_names=WRITE_PERMS,
                is_active=True,
            )
        )
        s.commit()
    _login(client, "teacher")

    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    assert res.status_code == 403, f"teacher 應被擋（403），實得 {res.status_code}"


def test_teacher_blocked_from_year_end_cycles_list(client_with_db):
    """role=teacher 持 YEAR_END_READ 仍應被擋出 GET cycles（403）。"""
    client, sf = client_with_db
    with sf() as s:
        s.add(
            User(
                username="teacher",
                password_hash=hash_password("TempPass123"),
                role="teacher",
                permission_names=WRITE_PERMS,
                is_active=True,
            )
        )
        s.commit()
    _login(client, "teacher")

    res = client.get("/api/year_end/cycles")
    assert res.status_code == 403, f"teacher 應被擋（403），實得 {res.status_code}"


def test_admin_still_allowed_on_settlements(client_with_db):
    """role=admin 持 YEAR_END_READ 仍可正常讀 settlements（守衛不誤殺合法角色）。"""
    client, sf = client_with_db
    cycle_id, _emp_id, _sid = _seed_cycle_and_employee(sf)
    with sf() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=WRITE_PERMS,
                is_active=True,
            )
        )
        s.commit()
    _login(client, "admin")

    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    assert res.status_code == 200, res.text


# ===== C8：自我核准守衛（寫金額端點） =====


def test_add_special_bonus_blocks_self(client_with_db):
    """操作者對自己 employee_id 的 settlement 加 special_bonus 應 403。"""
    client, sf = client_with_db
    cycle_id, emp_id, _sid = _seed_cycle_and_employee(sf)
    # 建立一個 admin 帳號，且其 employee_id == settlement.employee_id（自簽情境）
    with sf() as s:
        s.add(
            User(
                username="selfadmin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=WRITE_PERMS,
                employee_id=emp_id,
                is_active=True,
            )
        )
        s.commit()
    _login(client, "selfadmin")

    res = client.post(
        f"/api/year_end/cycles/{cycle_id}/special_bonuses",
        json={
            "employee_id": emp_id,
            "bonus_type": "EXCESS_ENROLLMENT",
            "period_label": "114上",
            "amount": "5000",
        },
    )
    assert res.status_code == 403, f"自簽應 403，實得 {res.status_code}: {res.text}"


def test_manual_patch_blocks_self(client_with_db):
    """操作者對自己 employee_id 的 settlement manual_patch 應 403。"""
    client, sf = client_with_db
    _cycle_id, emp_id, settlement_id = _seed_cycle_and_employee(sf)
    with sf() as s:
        s.add(
            User(
                username="selfadmin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=WRITE_PERMS,
                employee_id=emp_id,
                is_active=True,
            )
        )
        s.commit()
    _login(client, "selfadmin")

    res = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"excess_amount": "9999"},
    )
    assert res.status_code == 403, f"自簽應 403，實得 {res.status_code}: {res.text}"


def test_add_special_bonus_allows_other(client_with_db):
    """操作者對「他人」的 settlement 加 special_bonus 應放行（守衛不誤殺）。"""
    client, sf = client_with_db
    cycle_id, emp_id, _sid = _seed_cycle_and_employee(sf)
    with sf() as s:
        # admin 自己是另一位員工（employee_id 不同），不是被加獎金的對象
        other_emp = Employee(
            employee_id="E_RBAC_OTHER",
            name="李主管",
            id_number="B987654321",
            is_active=True,
        )
        s.add(other_emp)
        s.flush()
        s.add(
            User(
                username="otheradmin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=WRITE_PERMS,
                employee_id=other_emp.id,
                is_active=True,
            )
        )
        s.commit()
    _login(client, "otheradmin")

    res = client.post(
        f"/api/year_end/cycles/{cycle_id}/special_bonuses",
        json={
            "employee_id": emp_id,
            "bonus_type": "EXCESS_ENROLLMENT",
            "period_label": "114上",
            "amount": "5000",
        },
    )
    assert res.status_code == 200, f"他簽應放行，實得 {res.status_code}: {res.text}"
