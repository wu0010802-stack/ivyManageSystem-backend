"""finalized_by 型別回歸測試（P0）。

SalaryRecord.finalized_by 是 String(50)，finalize-month 寫入的是 username 字串
（fallback「管理員」）；但 SalaryRecordItemOut.finalized_by 曾誤標 Optional[int]，
導致第一次整月定案後 GET /salaries/records 觸發 ResponseValidationError → 500，
整個月結頁面（覆核/定案/匯出）從此每次進頁都掛。

本檔鎖定：
- 定案後 records 必須回 200 且 finalized_by 為操作者 username 字串
- 既有資料已寫入中文 fallback「管理員」時 records 仍可序列化
"""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.salary as salary_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.salary import router as salary_router
from models.database import Base, Employee, User, SalaryRecord
from utils.auth import hash_password


@pytest.fixture
def records_client(tmp_path):
    db_path = tmp_path / "finalized-by-regression.sqlite"
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

    salary_module.init_salary_services(MagicMock(), MagicMock())
    salary_module._snapshot_lazy_guard.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin_login(client, sf, username="admin", password="AdminPass123"):
    with sf() as session:
        session.add(
            User(
                employee_id=None,
                username=username,
                password_hash=hash_password(password),
                role="admin",
                permission_names=["*"],
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _seed_employee(sf, name: str, employee_id_str: str) -> int:
    with sf() as session:
        emp = Employee(
            employee_id=employee_id_str,
            name=name,
            base_salary=30000,
            employee_type="regular",
            is_active=True,
            hire_date=date(2025, 1, 1),
        )
        session.add(emp)
        session.commit()
        return emp.id


def _seed_salary_record(sf, emp_id: int, year=2026, month=3, **kwargs) -> int:
    with sf() as session:
        rec = SalaryRecord(
            employee_id=emp_id,
            salary_year=year,
            salary_month=month,
            base_salary=30000,
            gross_salary=30000,
            net_salary=28000,
            total_deduction=2000,
            **kwargs,
        )
        session.add(rec)
        session.commit()
        return rec.id


class TestFinalizedByStringSerialization:
    def test_records_ok_after_finalize_month(self, records_client):
        """整月定案後再查 records 必須 200，finalized_by 為操作者 username。"""
        client, sf = records_client
        emp_id = _seed_employee(sf, "員工甲", "A001")
        _seed_salary_record(sf, emp_id)
        _create_admin_login(client, sf)

        res = client.post(
            "/api/salaries/finalize-month", json={"year": 2026, "month": 3}
        )
        assert res.status_code == 200, res.text
        assert res.json()["finalized_by"] == "admin"

        res = client.get("/api/salaries/records", params={"year": 2026, "month": 3})
        assert res.status_code == 200, res.text
        rows = res.json()
        assert len(rows) == 1
        assert rows[0]["is_finalized"] is True
        assert rows[0]["finalized_by"] == "admin"

    def test_records_ok_with_cjk_fallback_operator(self, records_client):
        """既有資料 finalized_by=「管理員」（中文 fallback）仍可序列化。"""
        client, sf = records_client
        emp_id = _seed_employee(sf, "員工乙", "B001")
        _seed_salary_record(sf, emp_id, is_finalized=True, finalized_by="管理員")
        _create_admin_login(client, sf)

        res = client.get("/api/salaries/records", params={"year": 2026, "month": 3})
        assert res.status_code == 200, res.text
        assert res.json()[0]["finalized_by"] == "管理員"
