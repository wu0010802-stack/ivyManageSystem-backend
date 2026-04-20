"""員工附屬資料（學歷/證照/合約）+ 遮罩回寫防護 API 測試。"""

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
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.employees import router as employees_router
from api.employees_docs import router as employees_docs_router
from models.database import Base, Employee, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "employee-docs.sqlite"
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
    app.include_router(employees_router)
    app.include_router(employees_docs_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _mk_admin(session, username="emp_admin"):
    user = User(
        username=username,
        password_hash=hash_password("TempPass123"),
        role="admin",
        permissions=int(
            Permission.EMPLOYEES_READ
            | Permission.EMPLOYEES_WRITE
            | Permission.SALARY_WRITE
        ),
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _mk_reader(session, username="emp_reader"):
    """僅有 EMPLOYEES_READ，無 WRITE。"""
    user = User(
        username=username,
        password_hash=hash_password("TempPass123"),
        role="staff",
        permissions=int(Permission.EMPLOYEES_READ),
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _mk_employee(session, employee_id="E001", name="王小明", id_number="A123456789"):
    emp = Employee(
        employee_id=employee_id,
        name=name,
        id_number=id_number,
        employee_type="regular",
        base_salary=30000,
        is_active=True,
        phone="0912000000",
        address="台北市信義區",
        emergency_contact_name="王父",
        emergency_contact_phone="0922000000",
        dependents=1,
    )
    session.add(emp)
    session.flush()
    return emp


def _login(client, username="emp_admin", password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return res.json()


class TestEmployeeDetailContactFields:
    def test_detail_includes_new_contact_fields(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.get(f"/api/employees/{emp_id}")
        assert res.status_code == 200
        body = res.json()
        assert body["phone"] == "0912000000"
        assert body["address"] == "台北市信義區"
        assert body["emergency_contact_name"] == "王父"
        assert body["emergency_contact_phone"] == "0922000000"
        assert body["dependents"] == 1


class TestMaskedFieldWriteback:
    def test_update_ignores_masked_id_number(self, client_with_db):
        """送回含 * 的 id_number 時視為未修改，DB 不該被改寫。"""
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s, id_number="A123456789")
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"id_number": "A12******", "name": "王大明"},
        )
        assert res.status_code == 200
        with sf() as s:
            emp = s.query(Employee).filter(Employee.id == emp_id).first()
            assert emp.id_number == "A123456789"  # 未被改寫
            assert emp.name == "王大明"  # 其他欄位有更新

    def test_update_ignores_masked_bank_account(self, client_with_db):
        """實際 mask_bank_account 格式為 `****1234`（末 4 碼可見）。
        這類遮罩值送回後端時不該寫入 DB。
        """
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            emp.bank_account = "1234567890"
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"bank_account": "****7890"},
        )
        assert res.status_code == 200
        with sf() as s:
            emp = s.query(Employee).filter(Employee.id == emp_id).first()
            assert emp.bank_account == "1234567890"

    def test_update_accepts_real_id_number(self, client_with_db):
        """真實身分證值仍應被寫入。"""
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s, id_number="A123456789")
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.put(f"/api/employees/{emp_id}", json={"id_number": "B987654321"})
        assert res.status_code == 200
        with sf() as s:
            emp = s.query(Employee).filter(Employee.id == emp_id).first()
            assert emp.id_number == "B987654321"


class TestEducationCrud:
    def test_create_and_list(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client)
        payload = {
            "school_name": "臺北教育大學",
            "major": "幼兒教育",
            "degree": "學士",
            "graduation_date": "2018-06-30",
            "is_highest": True,
            "remark": "",
        }
        res = client.post(f"/api/employees/{emp_id}/educations", json=payload)
        assert res.status_code == 201, res.text
        row = res.json()
        assert row["school_name"] == "臺北教育大學"
        assert row["is_highest"] is True

        list_res = client.get(f"/api/employees/{emp_id}/educations")
        assert list_res.status_code == 200
        assert len(list_res.json()) == 1

    def test_invalid_degree_returns_400(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.post(
            f"/api/employees/{emp_id}/educations",
            json={"school_name": "X", "degree": "副學士"},
        )
        assert res.status_code == 400
        assert "degree" in res.json()["detail"]

    def test_is_highest_uniqueness_enforced(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client)
        r1 = client.post(
            f"/api/employees/{emp_id}/educations",
            json={"school_name": "A大", "degree": "學士", "is_highest": True},
        )
        assert r1.status_code == 201
        first_id = r1.json()["id"]
        r2 = client.post(
            f"/api/employees/{emp_id}/educations",
            json={"school_name": "B大", "degree": "碩士", "is_highest": True},
        )
        assert r2.status_code == 201
        list_res = client.get(f"/api/employees/{emp_id}/educations")
        rows = {r["id"]: r for r in list_res.json()}
        assert rows[first_id]["is_highest"] is False
        assert rows[r2.json()["id"]]["is_highest"] is True

    def test_write_requires_employees_write_permission(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            _mk_reader(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client, username="emp_reader")
        res = client.post(
            f"/api/employees/{emp_id}/educations",
            json={"school_name": "X", "degree": "學士"},
        )
        assert res.status_code == 403


class TestCertificateCrud:
    def test_create_with_null_expiry(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.post(
            f"/api/employees/{emp_id}/certificates",
            json={
                "certificate_name": "教保員證照",
                "issuer": "勞動部",
                "issued_date": "2019-01-10",
                "expiry_date": None,
            },
        )
        assert res.status_code == 201, res.text
        assert res.json()["expiry_date"] is None


class TestContractCrud:
    def test_create_valid_contract(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.post(
            f"/api/employees/{emp_id}/contracts",
            json={
                "contract_type": "正式",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "salary_at_contract": 30000,
            },
        )
        assert res.status_code == 201, res.text

    def test_end_before_start_returns_400(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.post(
            f"/api/employees/{emp_id}/contracts",
            json={
                "contract_type": "正式",
                "start_date": "2024-06-01",
                "end_date": "2024-01-01",
            },
        )
        assert res.status_code == 400
        assert "end_date" in res.json()["detail"]

    def test_invalid_contract_type_400(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _mk_admin(s)
            emp = _mk_employee(s)
            s.commit()
            emp_id = emp.id
        _login(client)
        res = client.post(
            f"/api/employees/{emp_id}/contracts",
            json={"contract_type": "外包", "start_date": "2024-01-01"},
        )
        assert res.status_code == 400
