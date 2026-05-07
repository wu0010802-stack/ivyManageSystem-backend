"""tests/test_employee_config_stale_marking.py — 員工主檔/職位標準底薪/保險費率
變動後薪資 needs_recalc 旗標回歸測試（2026-05-06）。

問題情境：原 PUT /api/employees/{id}、DELETE /api/employees/{id}、POST
/api/employees/{id}/offboard 以及 PUT /api/config/position-salary、
PUT /api/config/insurance-rates 改變薪資輸入後，沒有把已算未封存的
SalaryRecord 標 needs_recalc=True；之後 finalize 仍可能以舊金額/舊費率/
舊標準封存。對齊 attendance/leaves/overtimes 等上游已落實的 mark_stale 規範。

涵蓋：
- 改 base_salary → 該員工未封存 record 標 stale，封存的不動，他人 record 不動
- 改非薪資欄位（phone）→ 不標 stale
- 軟刪除（DELETE /employees/{id}）→ 該員工未封存 record 標 stale
- 辦理離職（POST /employees/{id}/offboard）→ 該員工未封存 record 標 stale
- PUT /config/position-salary → 全園未封存 record 標 stale，封存的不動
- PUT /config/insurance-rates → 全園未封存 record 標 stale，封存的不動
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
import api.config as config_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.employees import router as employees_router
from api.config import router as config_router
from models.base import Base
from models.database import Employee, SalaryRecord, User
from utils.auth import hash_password

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def stale_client(tmp_path):
    db_path = tmp_path / "stale.sqlite"
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

    fake_engine = MagicMock()
    config_module.init_config_services(fake_engine, MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(employees_router)
    app.include_router(config_router)

    with TestClient(app) as client:
        yield client, session_factory, fake_engine

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_admin(client, sf, username="admin", password="AdminPass123"):
    with sf() as session:
        session.add(
            User(
                employee_id=None,
                username=username,
                password_hash=hash_password(password),
                role="admin",
                permissions=-1,
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _seed_employee(sf, *, name="員工A", emp_no="E001", base_salary=30000):
    with sf() as session:
        emp = Employee(
            employee_id=emp_no,
            name=name,
            base_salary=base_salary,
            employee_type="regular",
            is_active=True,
            hire_date=date(2025, 1, 1),
        )
        session.add(emp)
        session.commit()
        return emp.id


def _seed_record(
    sf,
    emp_id,
    *,
    year=2026,
    month=3,
    needs_recalc=False,
    is_finalized=False,
):
    with sf() as session:
        rec = SalaryRecord(
            employee_id=emp_id,
            salary_year=year,
            salary_month=month,
            base_salary=30000,
            gross_salary=30000,
            net_salary=28000,
            total_deduction=2000,
            needs_recalc=needs_recalc,
            is_finalized=is_finalized,
        )
        session.add(rec)
        session.commit()
        return rec.id


# ─────────────────────────────────────────────────────────────────────────────
# update_employee: 異動薪資輸入欄位 → mark stale
# ─────────────────────────────────────────────────────────────────────────────


class TestUpdateEmployeeMarksStale:
    def test_change_base_salary_marks_unfinalized_only(self, stale_client):
        """改 base_salary 應只標該員工未封存薪資；封存的、他人薪資不動。"""
        client, sf, _ = stale_client
        emp_id = _seed_employee(sf, name="目標A", emp_no="A001")
        other_id = _seed_employee(sf, name="他人B", emp_no="B001")

        unfin_rec = _seed_record(sf, emp_id, year=2026, month=3)
        fin_rec = _seed_record(sf, emp_id, year=2026, month=2, is_finalized=True)
        other_rec = _seed_record(sf, other_id, year=2026, month=3)

        _login_admin(client, sf)
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"base_salary": 35000, "adjustment_reason": "年度調薪生效"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=unfin_rec).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=fin_rec).one().needs_recalc
                is False
            )
            assert (
                session.query(SalaryRecord).filter_by(id=other_rec).one().needs_recalc
                is False
            )

    def test_change_indirect_salary_field_marks_stale(self, stale_client):
        """改間接薪資欄位（hire_date）也要 mark stale。"""
        client, sf, _ = stale_client
        emp_id = _seed_employee(sf, name="目標C", emp_no="C001")
        rec_id = _seed_record(sf, emp_id)

        _login_admin(client, sf)
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"hire_date": "2025-06-01"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec_id).one().needs_recalc
                is True
            )

    def test_change_non_salary_field_does_not_mark_stale(self, stale_client):
        """改 phone（非薪資輸入）不該 mark stale。"""
        client, sf, _ = stale_client
        emp_id = _seed_employee(sf, name="目標D", emp_no="D001")
        rec_id = _seed_record(sf, emp_id)

        _login_admin(client, sf)
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"phone": "0911000000"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec_id).one().needs_recalc
                is False
            )

    def test_change_job_title_cascade_marks_stale(self, stale_client):
        """改 job_title_id 會級聯改 title（_SALARY_INPUT_FIELDS 內），應 mark stale。"""
        from models.database import JobTitle

        client, sf, _ = stale_client
        emp_id = _seed_employee(sf, name="目標E2", emp_no="E002")
        with sf() as session:
            jt = JobTitle(name="班導師")
            session.add(jt)
            session.commit()
            jt_id = jt.id
        rec_id = _seed_record(sf, emp_id)

        _login_admin(client, sf)
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"job_title_id": jt_id},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec_id).one().needs_recalc
                is True
            )

    def test_unchanged_salary_value_does_not_mark_stale(self, stale_client):
        """送回相同 base_salary（delta=0）→ 不該 mark stale。"""
        client, sf, _ = stale_client
        emp_id = _seed_employee(sf, name="目標E", emp_no="E001", base_salary=30000)
        rec_id = _seed_record(sf, emp_id)

        _login_admin(client, sf)
        res = client.put(
            f"/api/employees/{emp_id}",
            json={"base_salary": 30000},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec_id).one().needs_recalc
                is False
            )


# ─────────────────────────────────────────────────────────────────────────────
# delete_employee（軟刪除）/ offboard_employee：mark stale
# ─────────────────────────────────────────────────────────────────────────────


class TestEmployeeDeleteOffboardMarksStale:
    def test_soft_delete_marks_unfinalized_stale(self, stale_client):
        """DELETE /employees/{id} 軟刪除 → 該員工未封存薪資 mark stale。"""
        client, sf, _ = stale_client
        emp_id = _seed_employee(sf, name="目標F", emp_no="F001")
        unfin_rec = _seed_record(sf, emp_id, year=2026, month=3)
        fin_rec = _seed_record(sf, emp_id, year=2026, month=2, is_finalized=True)

        _login_admin(client, sf)
        res = client.delete(f"/api/employees/{emp_id}")
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=unfin_rec).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=fin_rec).one().needs_recalc
                is False
            )

    def test_offboard_marks_unfinalized_stale(self, stale_client):
        """POST /employees/{id}/offboard → 該員工未封存薪資 mark stale。"""
        client, sf, _ = stale_client
        emp_id = _seed_employee(sf, name="目標G", emp_no="G001")
        rec_id = _seed_record(sf, emp_id)

        _login_admin(client, sf)
        res = client.post(
            f"/api/employees/{emp_id}/offboard",
            json={"resign_date": "2026-04-30", "resign_reason": "個人因素"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec_id).one().needs_recalc
                is True
            )


# ─────────────────────────────────────────────────────────────────────────────
# update_position_salary：mark stale 全園未封存
# ─────────────────────────────────────────────────────────────────────────────


class TestUpdatePositionSalaryMarksStale:
    def test_update_position_salary_marks_all_unfinalized_stale(self, stale_client):
        client, sf, fake_engine = stale_client
        emp_a = _seed_employee(sf, name="員工H", emp_no="H001")
        emp_b = _seed_employee(sf, name="員工I", emp_no="I001")

        rec_a_unfin = _seed_record(sf, emp_a, year=2026, month=3)
        rec_b_unfin = _seed_record(sf, emp_b, year=2026, month=3)
        rec_a_fin = _seed_record(sf, emp_a, year=2026, month=2, is_finalized=True)

        _login_admin(client, sf)
        res = client.put(
            "/api/config/position-salary",
            json={"head_teacher_a": 40000},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["salary_records_marked_stale"] >= 2

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec_a_unfin).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=rec_b_unfin).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=rec_a_fin).one().needs_recalc
                is False
            )
        # 同 transaction 結束後也要 reload engine（讓 simulate 拿到新標準）
        assert fake_engine.load_config_from_db.called


# ─────────────────────────────────────────────────────────────────────────────
# update_insurance_rates：mark stale 全園未封存
# ─────────────────────────────────────────────────────────────────────────────


class TestUpdateInsuranceRatesMarksStale:
    def test_update_insurance_rates_marks_all_unfinalized_stale(self, stale_client):
        client, sf, _ = stale_client
        emp_a = _seed_employee(sf, name="員工J", emp_no="J001")
        emp_b = _seed_employee(sf, name="員工K", emp_no="K001")

        rec_a_unfin = _seed_record(sf, emp_a, year=2026, month=3)
        rec_b_unfin = _seed_record(sf, emp_b, year=2026, month=3)
        rec_b_fin = _seed_record(sf, emp_b, year=2026, month=2, is_finalized=True)

        _login_admin(client, sf)
        res = client.put(
            "/api/config/insurance-rates",
            json={"labor_rate": 0.115},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["salary_records_marked_stale"] >= 2

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec_a_unfin).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=rec_b_unfin).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=rec_b_fin).one().needs_recalc
                is False
            )
