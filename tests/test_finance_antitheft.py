"""tests/test_finance_antitheft.py — 跨模組 A 錢守衛回歸測試（2026-04-24）。

涵蓋：
- employees.py PUT /api/employees/{id}：員工不得修改自己帳號的金流敏感欄位 → 403
- salary.py PUT /api/salaries/{id}/manual-adjust：
    * 員工不得調整自己的 SalaryRecord → 403
    * adjustment_reason 必填 ≥ 5 字 → 422
    * 單欄位變動 > FINANCE_APPROVAL_THRESHOLD 需 ACTIVITY_PAYMENT_APPROVE → 403
- fees.py POST /api/fees/records/{id}/refund：
    * reason < 5 字 → 422
    * 退款金額 > FINANCE_APPROVAL_THRESHOLD 需 ACTIVITY_PAYMENT_APPROVE → 403
    * 具簽核權限者執行相同請求 → 201
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.employees import router as employees_router
from api.fees import router as fees_router
from api.salary import router as salary_router
from models.base import Base
from models.classroom import Classroom, Student
from models.database import Employee, SalaryRecord, User
from models.fees import FeeItem, StudentFeeRecord
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def antitheft_client(tmp_path):
    db_path = tmp_path / "antitheft.sqlite"
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
    app.include_router(employees_router)
    app.include_router(salary_router)
    app.include_router(fees_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_employee(session, *, name="員工甲", base_salary=30000):
    emp = Employee(
        employee_id=f"E_{name}",
        name=name,
        base_salary=base_salary,
        employee_type="regular",
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user(session, *, username, permissions, employee_id=None, role="admin"):
    u = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role=role,
        permissions=permissions,
        employee_id=employee_id,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


# ══════════════════════════════════════════════════════════════════════
# #1 員工自改薪資欄位守衛
# ══════════════════════════════════════════════════════════════════════


class TestEmployeeSelfEdit:
    def test_employee_cannot_edit_own_base_salary(self, antitheft_client):
        """員工帳號綁定 employee_id=X，PUT /employees/X 改自己底薪 → 403。"""
        client, sf = antitheft_client
        with sf() as s:
            emp = _make_employee(s, name="自改測試", base_salary=30000)
            _make_user(
                s,
                username="self_edit",
                permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
                employee_id=emp.id,
            )
            s.commit()
            emp_id = emp.id

        assert _login(client, "self_edit").status_code == 200
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"base_salary": 99999},
        )
        assert res.status_code == 403
        detail = res.json()["detail"]
        assert detail["code"] == "SELF_FINANCE_EDIT_FORBIDDEN"
        assert "不得修改自己" in detail["message"]
        assert "base_salary" in detail["context"]["fields"]

    def test_employee_can_edit_own_non_sensitive_fields(self, antitheft_client):
        """員工可改自己非敏感欄位（如通訊地址/姓名）— 守衛不影響一般資料維護。"""
        client, sf = antitheft_client
        with sf() as s:
            emp = _make_employee(s, name="原名", base_salary=30000)
            _make_user(
                s,
                username="self_edit",
                permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
                employee_id=emp.id,
            )
            s.commit()
            emp_id = emp.id

        assert _login(client, "self_edit").status_code == 200
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"name": "更新後姓名"},
        )
        assert res.status_code == 200

    def test_pure_admin_without_employee_id_can_edit_salary(self, antitheft_client):
        """純管理員（employee_id=None）本身無員工身份，不受自改守衛限制。"""
        client, sf = antitheft_client
        with sf() as s:
            emp = _make_employee(s, name="員工乙", base_salary=30000)
            _make_user(
                s,
                username="pure_admin",
                permissions=-1,  # 全部權限
                employee_id=None,
            )
            s.commit()
            emp_id = emp.id

        assert _login(client, "pure_admin").status_code == 200
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"base_salary": 40000},
        )
        assert res.status_code == 200

    def test_employee_cannot_edit_own_indirect_salary_fields(self, antitheft_client):
        """員工改自己 bonus_grade / position / hire_date 等間接影響薪資的欄位 → 403。

        這些欄位雖非直接金額，但會影響節慶獎金資格、主管紅利、底薪標準、班級獎金。
        """
        client, sf = antitheft_client
        for field, value in [
            ("bonus_grade", "A"),
            ("position", "園長"),
            ("supervisor_role", "園長"),
            ("hire_date", "2020-01-01"),
        ]:
            with sf() as s:
                emp = _make_employee(s, name=f"間接_{field}", base_salary=30000)
                _make_user(
                    s,
                    username=f"indirect_{field}",
                    permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
                    employee_id=emp.id,
                )
                s.commit()
                emp_id = emp.id

            assert _login(client, f"indirect_{field}").status_code == 200
            res = client.put(
                f"/api/employees/{emp_id}",
                json={field: value},
            )
            assert (
                res.status_code == 403
            ), f"field={field} 應被守衛攔下，實際 {res.status_code}"
            detail = res.json()["detail"]
            assert detail["code"] == "SELF_FINANCE_EDIT_FORBIDDEN"
            assert "不得修改自己" in detail["message"]
            assert field in detail["context"]["fields"]


# ══════════════════════════════════════════════════════════════════════
# #2 薪資 manual-adjust 守衛
# ══════════════════════════════════════════════════════════════════════


def _seed_salary_record(session, *, base_salary=30000):
    emp = _make_employee(session, name="薪資測試", base_salary=base_salary)
    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=4,
        base_salary=base_salary,
        gross_salary=base_salary,
        total_deduction=0,
        net_salary=base_salary,
        is_finalized=False,
    )
    session.add(rec)
    session.flush()
    return emp, rec


class TestSalaryManualAdjustAntiTheft:
    def test_employee_cannot_adjust_own_salary_record(self, antitheft_client):
        """員工帳號綁定 employee_id=X，調整 employee_id=X 的 SalaryRecord → 403。"""
        client, sf = antitheft_client
        with sf() as s:
            emp, rec = _seed_salary_record(s)
            _make_user(
                s,
                username="self_adj",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
                employee_id=emp.id,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "self_adj").status_code == 200
        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={
                "adjustment_reason": "測試自調守衛",
                "performance_bonus": 5000,
            },
        )
        assert res.status_code == 403
        assert "自己的薪資" in res.json()["detail"]

    def test_adjustment_reason_required_422(self, antitheft_client):
        """未帶 adjustment_reason 應 422（Pydantic required）。"""
        client, sf = antitheft_client
        with sf() as s:
            _emp, rec = _seed_salary_record(s)
            _make_user(s, username="boss", permissions=-1, employee_id=None)
            s.commit()
            rec_id = rec.id

        assert _login(client, "boss").status_code == 200
        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={"performance_bonus": 3000},
        )
        assert res.status_code == 422

    def test_large_delta_without_approve_permission_403(self, antitheft_client):
        """delta > FINANCE_APPROVAL_THRESHOLD 但無 ACTIVITY_PAYMENT_APPROVE → 403。"""
        client, sf = antitheft_client
        with sf() as s:
            _emp, rec = _seed_salary_record(s)
            _make_user(
                s,
                username="write_only_hr",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
                employee_id=None,  # 純 HR 帳號，非員工本人
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "write_only_hr").status_code == 200
        # 舊 performance_bonus=0，改成 100000 → delta=100000，超過 FINANCE_APPROVAL_THRESHOLD=1000
        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={
                "adjustment_reason": "大額績效獎金測試",
                "performance_bonus": 100000,
            },
        )
        assert res.status_code == 403
        assert "審批閾值" in res.json()["detail"]

    def test_small_delta_within_threshold_allowed(self, antitheft_client):
        """delta ≤ FINANCE_APPROVAL_THRESHOLD 時，一般 SALARY_WRITE 即可。"""
        client, sf = antitheft_client
        with sf() as s:
            _emp, rec = _seed_salary_record(s)
            _make_user(
                s,
                username="write_only_hr",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
                employee_id=None,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "write_only_hr").status_code == 200
        # delta=500 < 閾值 1000
        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={
                "adjustment_reason": "小額會議加班補登",
                "meeting_overtime_pay": 500,
            },
        )
        assert res.status_code == 200, res.text

    def test_large_delta_with_approve_permission_allowed(self, antitheft_client):
        """具 ACTIVITY_PAYMENT_APPROVE 的使用者可執行大額調整。"""
        client, sf = antitheft_client
        with sf() as s:
            _emp, rec = _seed_salary_record(s)
            _make_user(
                s,
                username="finance_boss",
                permissions=Permission.SALARY_READ
                | Permission.SALARY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
                employee_id=None,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "finance_boss").status_code == 200
        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={
                "adjustment_reason": "核准一次性績效獎金",
                "performance_bonus": 50000,
            },
        )
        assert res.status_code == 200, res.text


# ══════════════════════════════════════════════════════════════════════
# #3 學費退款守衛
# ══════════════════════════════════════════════════════════════════════


def _seed_fee_record(session, *, amount_paid=5000):
    cls = Classroom(name="大班", school_year=2025, semester=1)
    session.add(cls)
    session.flush()
    st = Student(
        student_id="S00001", name="王小明", is_active=True, classroom_id=cls.id
    )
    session.add(st)
    session.flush()
    item = FeeItem(name="學費", amount=amount_paid, period="2025-1", is_active=True)
    session.add(item)
    session.flush()
    rec = StudentFeeRecord(
        student_id=st.id,
        student_name=st.name,
        classroom_name=cls.name,
        fee_item_id=item.id,
        fee_item_name=item.name,
        amount_due=amount_paid,
        amount_paid=amount_paid,
        status="paid",
        period=item.period,
        payment_date=date.today(),
    )
    session.add(rec)
    session.flush()
    return rec


class TestFeeRefundAntiTheft:
    def test_refund_reason_too_short_422(self, antitheft_client):
        """reason < 5 字應 422（Pydantic）。"""
        client, sf = antitheft_client
        with sf() as s:
            rec = _seed_fee_record(s, amount_paid=500)
            _make_user(
                s,
                username="fees_user",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
                employee_id=None,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "fees_user").status_code == 200
        res = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 500, "reason": "誤"},
        )
        assert res.status_code == 422

    def test_large_refund_without_approve_permission_403(self, antitheft_client):
        """退款金額 > FINANCE_APPROVAL_THRESHOLD 但無 ACTIVITY_PAYMENT_APPROVE → 403。"""
        client, sf = antitheft_client
        with sf() as s:
            rec = _seed_fee_record(s, amount_paid=5000)
            _make_user(
                s,
                username="fees_user",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
                employee_id=None,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "fees_user").status_code == 200
        res = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 5000, "reason": "家長要求全額退費"},
        )
        assert res.status_code == 403
        assert "審批閾值" in res.json()["detail"]

    def test_small_refund_allowed_without_approve(self, antitheft_client):
        """小額退款（≤ 閾值）不需簽核權限。"""
        client, sf = antitheft_client
        with sf() as s:
            rec = _seed_fee_record(s, amount_paid=5000)
            _make_user(
                s,
                username="fees_user",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
                employee_id=None,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "fees_user").status_code == 200
        res = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 500, "reason": "家長多繳部分小額退回"},
        )
        assert res.status_code == 201, res.text

    def test_large_refund_with_approve_permission_ok(self, antitheft_client):
        """具 ACTIVITY_PAYMENT_APPROVE 者可執行大額退款。"""
        client, sf = antitheft_client
        with sf() as s:
            rec = _seed_fee_record(s, amount_paid=5000)
            _make_user(
                s,
                username="finance_boss",
                permissions=Permission.FEES_READ
                | Permission.FEES_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
                employee_id=None,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "finance_boss").status_code == 200
        res = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 5000, "reason": "家長轉學全額退費（主管簽核）"},
        )
        assert res.status_code == 201, res.text
