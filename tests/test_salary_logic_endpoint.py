"""回歸：薪資邏輯／員工薪資 debug 正式端點

Bug：前端 SalaryLogicPanel / SalarySimulatePanel 過去打 `/api/dev/salary-logic`
與 `/api/dev/employee-salary-debug`，這兩條只在 ENV 白名單內才掛 dev_router；
正式環境 / staging / 未設 ENV 一律 404，導致薪資邏輯分頁叫不出來。

修復：在 api/salary.py 新增 `/api/salaries/logic` 與
`/api/salaries/employee-salary-debug` 並抽 services/salary_logic_info.py 共用，
本檔保證即使 dev_router 未掛載仍可正常工作，且權限收斂為 SALARY_READ +
（debug 端點）self-or-full 守衛。
"""

import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts, router as auth_router
import api.salary as salary_module
from api.salary import router as salary_router
from models.database import Base, Employee, SalaryRecord, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def salary_logic_client(tmp_path):
    """純 salary_router（不掛 dev_router）+ sqlite in-memory，模擬 production ENV。"""
    db_path = tmp_path / "salary-logic.sqlite"
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

    fake_engine = MagicMock()
    fake_engine.deduction_rules = {"personal": 1.0, "sick": 0.5}
    fake_engine._attendance_policy = {"default_work_start": "08:00"}
    fake_engine._school_wide_target = 200
    fake_engine._meeting_hours = 1
    fake_engine._meeting_absence_penalty = 100
    fake_engine._bonus_base = {}
    fake_engine._target_enrollment = 200
    fake_engine._overtime_target = 200
    fake_engine._overtime_per_person = 0
    fake_engine._supervisor_dividend = {}
    fake_engine._supervisor_festival_bonus = {}
    fake_engine._office_festival_bonus_base = 0
    fake_engine.POSITION_GRADE_MAP = {}
    salary_module.init_salary_services(fake_engine, MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory, fake_engine

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, *, username, password, role, permissions, employee_id=None):
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


class TestSalaryLogicEndpoint:
    """`/api/salaries/logic` 在未掛 dev_router 時仍須可用。"""

    def test_admin_with_salary_read_returns_200(self, salary_logic_client):
        """admin 持 SALARY_READ 應拿到完整邏輯資訊，不依賴 dev_router。"""
        client, session_factory, _ = salary_logic_client
        with session_factory() as session:
            _create_user(
                session,
                username="logic_admin",
                password="LogicPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()

        login = _login(client, "logic_admin", "LogicPass123")
        assert login.status_code == 200

        res = client.get("/api/salaries/logic")
        assert res.status_code == 200, res.json()
        body = res.json()
        # 關鍵欄位：前端 SalaryLogicPanel 直接讀
        for key in (
            "salary_formula",
            "leave_deduction_rules",
            "insurance_runtime_config",
            "engine_runtime_config",
            "shift_types",
            "formula_verification",
        ):
            assert key in body, f"missing key {key}"
        # 公式比對結構
        fv = body["formula_verification"]
        for key in (
            "attendance_formulas",
            "insurance_formulas",
            "official_checks",
            "sample_bracket_checks",
            "official_sources",
            "runtime_note",
        ):
            assert key in fv

    def test_dev_router_not_mounted_in_this_app(self, salary_logic_client):
        """證明此 fixture 確實沒掛 dev_router（路徑 404），確保上題真的走正式端點。"""
        client, *_ = salary_logic_client
        res = client.get("/api/dev/salary-logic")
        assert res.status_code == 404

    def test_user_without_salary_read_gets_403(self, salary_logic_client):
        """權限不足應回 403，不應誤回 404。"""
        client, session_factory, _ = salary_logic_client
        with session_factory() as session:
            _create_user(
                session,
                username="logic_no_perm",
                password="NoPermPass123",
                role="teacher",
                permissions=int(Permission.STUDENTS_READ),
            )
            session.commit()

        login = _login(client, "logic_no_perm", "NoPermPass123")
        assert login.status_code == 200

        res = client.get("/api/salaries/logic")
        assert res.status_code == 403

    def test_unauthenticated_returns_401(self, salary_logic_client):
        client, *_ = salary_logic_client
        res = client.get("/api/salaries/logic")
        assert res.status_code == 401


class TestEmployeeSalaryDebugEndpoint:
    """`/api/salaries/employee-salary-debug`：SALARY_READ + self-or-full。"""

    def test_admin_can_query_other_employee(self, salary_logic_client, monkeypatch):
        """admin 屬 FULL_SALARY_ROLES，可查任意員工。"""
        client, session_factory, fake_engine = salary_logic_client
        with session_factory() as session:
            emp = Employee(
                employee_id="DBG001",
                name="Debug員工",
                base_salary=30000,
                is_active=True,
                employee_type="regular",
            )
            session.add(emp)
            session.flush()
            _create_user(
                session,
                username="dbg_admin",
                password="DbgPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            target_emp_id = emp.id

        # build_salary_debug_snapshot 內部會走複雜的 engine 計算，這裡只驗證授權
        # 與路由連通；用 monkeypatch 取代為 stub。
        monkeypatch.setattr(
            "api.salary.build_salary_debug_snapshot",
            lambda session, engine, emp, year, month: {
                "employee": {"name": emp.name},
                "ok": True,
            },
        )

        login = _login(client, "dbg_admin", "DbgPass123")
        assert login.status_code == 200

        res = client.get(
            "/api/salaries/employee-salary-debug",
            params={"employee_id": target_emp_id, "year": 2026, "month": 4},
        )
        assert res.status_code == 200, res.json()
        assert res.json().get("ok") is True

    def test_teacher_cannot_query_other_employee(self, salary_logic_client):
        """非全員視野角色查別人應 403（self-or-full 守衛）。"""
        client, session_factory, _ = salary_logic_client
        with session_factory() as session:
            self_emp = Employee(
                employee_id="DBG_SELF",
                name="自己",
                base_salary=30000,
                is_active=True,
                employee_type="regular",
            )
            other_emp = Employee(
                employee_id="DBG_OTHER",
                name="別人",
                base_salary=30000,
                is_active=True,
                employee_type="regular",
            )
            session.add_all([self_emp, other_emp])
            session.flush()
            _create_user(
                session,
                username="dbg_teacher",
                password="DbgTeacherPass123",
                role="teacher",
                permissions=int(Permission.SALARY_READ),
                employee_id=self_emp.id,
            )
            session.commit()
            other_id = other_emp.id

        login = _login(client, "dbg_teacher", "DbgTeacherPass123")
        assert login.status_code == 200

        res = client.get(
            "/api/salaries/employee-salary-debug",
            params={"employee_id": other_id, "year": 2026, "month": 4},
        )
        assert res.status_code == 403

    def test_hourly_employee_returns_422(self, salary_logic_client):
        """時薪員工應回 422，不應 500。"""
        client, session_factory, _ = salary_logic_client
        with session_factory() as session:
            emp = Employee(
                employee_id="DBG_HOURLY",
                name="時薪員工",
                base_salary=0,
                is_active=True,
                employee_type="hourly",
            )
            session.add(emp)
            session.flush()
            _create_user(
                session,
                username="dbg_admin2",
                password="DbgPass456",
                role="admin",
                permissions=-1,
            )
            session.commit()
            emp_id = emp.id

        login = _login(client, "dbg_admin2", "DbgPass456")
        assert login.status_code == 200

        res = client.get(
            "/api/salaries/employee-salary-debug",
            params={"employee_id": emp_id, "year": 2026, "month": 4},
        )
        assert res.status_code == 422


class TestServiceSharedWithDevRouter:
    """確認 dev.py 改用共享 service 後行為與正式端點一致。"""

    def test_dev_and_prod_endpoint_use_same_service(self):
        """smoke：dev 端點呼叫 build_salary_logic_info；正式端點同款。"""
        import api.dev as dev_module
        import api.salary as salary_module_local

        # 兩個 router 都 import 同一個 helper
        src_dev = open(dev_module.__file__).read()
        src_salary = open(salary_module_local.__file__).read()
        assert (
            "from services.salary_logic_info import build_salary_logic_info" in src_dev
        )
        assert (
            "from services.salary_logic_info import build_salary_logic_info"
            in src_salary
        )
