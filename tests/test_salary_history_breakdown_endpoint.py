import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User, SalaryRecord
from utils.auth import hash_password, create_access_token


@pytest.fixture
def client_and_emp(tmp_path):
    from api.salary.records import router as salary_records_router

    engine = create_engine(
        f"sqlite:///{tmp_path / 'hist-breakdown.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine, old_factory = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    session = session_factory()
    try:
        emp = Employee(employee_id="T99", name="王小明", is_active=True)
        session.add(emp)
        session.commit()
        emp_id = emp.id
        admin = User(
            username="admin",
            password_hash=hash_password("Admin1234"),
            role="admin",
            is_active=True,
            permission_names=["*"],
            employee_id=emp_id,
        )
        session.add(admin)
        session.commit()
        admin_id = admin.id
        session.add(
            SalaryRecord(
                employee_id=emp_id,
                salary_year=2026,
                salary_month=6,
                base_salary=2950,
                supervisor_dividend=5000,
                festival_bonus=26000,
                labor_insurance_employee=600,
                health_insurance_employee=800,
                leave_deduction=900,
                gross_salary=7950,
                total_deduction=4604,
                net_salary=3346,
            )
        )
        session.commit()
    finally:
        session.close()

    token = create_access_token(
        {
            "user_id": admin_id,
            "employee_id": emp_id,
            "role": "admin",
            "name": "王小明",
            "permission_names": ["*"],
            "token_version": 0,
        }
    )
    app = FastAPI()
    app.include_router(salary_records_router, prefix="/api")
    client = TestClient(app)
    client.cookies.set("access_token", token)
    yield client, emp_id

    base_module._engine, base_module._SessionFactory = old_engine, old_factory
    engine.dispose()


def test_history_returns_payslip_detail_three_regions(client_and_emp):
    client, emp_id = client_and_emp
    res = client.get(f"/api/salaries/history?employee_id={emp_id}")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["in_gross_bonus"] == 5000  # gross 7950 − base 2950 − hourly 0
    assert row["separate_transfer_total"] == 26000
    detail = row["payslip_detail"]
    assert detail["income_subtotal"] == 7950
    assert detail["deduction_subtotal"] == 4604
    assert detail["net_salary"] == 3346
    income_keys = {l["key"] for l in detail["income"]}
    sep_keys = {l["key"] for l in detail["separate_transfer"]}
    assert "supervisor_dividend" in income_keys
    assert "festival_bonus" in sep_keys
    assert "festival_bonus" not in income_keys


@pytest.fixture
def client_and_emp_hourly(tmp_path):
    from api.salary.records import router as salary_records_router

    engine = create_engine(
        f"sqlite:///{tmp_path / 'hist-breakdown-hourly.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine, old_factory = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    session = session_factory()
    try:
        emp = Employee(employee_id="H01", name="時薪員", is_active=True)
        session.add(emp)
        session.commit()
        emp_id = emp.id
        admin = User(
            username="admin",
            password_hash=hash_password("Admin1234"),
            role="admin",
            is_active=True,
            permission_names=["*"],
            employee_id=emp_id,
        )
        session.add(admin)
        session.commit()
        admin_id = admin.id
        # 時薪員工：base 0、時薪總計 29500、績效獎金 2000 → gross 31500
        session.add(
            SalaryRecord(
                employee_id=emp_id,
                salary_year=2026,
                salary_month=6,
                base_salary=0,
                hourly_total=29500,
                performance_bonus=2000,
                gross_salary=31500,
                total_deduction=500,
                net_salary=31000,
            )
        )
        session.commit()
    finally:
        session.close()

    token = create_access_token(
        {
            "user_id": admin_id,
            "employee_id": emp_id,
            "role": "admin",
            "name": "時薪員",
            "permission_names": ["*"],
            "token_version": 0,
        }
    )
    app = FastAPI()
    app.include_router(salary_records_router, prefix="/api")
    client = TestClient(app)
    client.cookies.set("access_token", token)
    yield client, emp_id

    base_module._engine, base_module._SessionFactory = old_engine, old_factory
    engine.dispose()


def test_history_in_gross_bonus_excludes_hourly_total(client_and_emp_hourly):
    client, emp_id = client_and_emp_hourly
    res = client.get(f"/api/salaries/history?employee_id={emp_id}")
    assert res.status_code == 200
    row = res.json()[0]
    # in_gross_bonus = gross 31500 − base 0 − hourly 29500 = 2000（時薪不算進「獎金合計」）
    assert row["in_gross_bonus"] == 2000
    detail = row["payslip_detail"]
    assert detail["income_subtotal"] == 31500
    hourly_line = next(l for l in detail["income"] if l["key"] == "hourly_total")
    assert hourly_line["amount"] == 29500
