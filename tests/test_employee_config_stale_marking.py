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


# ─────────────────────────────────────────────────────────────────────────────
# sync_position_salary：把員工底薪同步至職位標準後 → mark stale（#1 P0）
# 對稱依據：PUT /employees 改 base_salary 走 _mark_employee_salary_stale；
#          同檔 PUT /position-salary 走 _mark_existing_salary_stale_for_config。
# ─────────────────────────────────────────────────────────────────────────────


def _seed_position_employee(
    sf, *, name, emp_no, base_salary, position, title=None, bonus_grade=None
):
    from models.database import Employee

    with sf() as session:
        emp = Employee(
            employee_id=emp_no,
            name=name,
            base_salary=base_salary,
            employee_type="regular",
            is_active=True,
            hire_date=date(2025, 1, 1),
            position=position,
            title=title,
            bonus_grade=bonus_grade,
        )
        session.add(emp)
        session.commit()
        return emp.id


class TestPositionSalarySyncMarksStale:
    def test_sync_marks_unfinalized_only(self, stale_client):
        """sync 把員工底薪改成標準後，應標該員工未封存薪資；封存的不動。"""
        client, sf, _ = stale_client
        # 班導 a 級 → 標準 head_teacher_a=39240（預設）；base 39000 → delta 240 (<1000，免簽核)
        emp_id = _seed_position_employee(
            sf,
            name="班導A",
            emp_no="P001",
            base_salary=39000,
            position="班導",
            bonus_grade="a",
        )
        unfin = _seed_record(sf, emp_id, year=2026, month=3)
        fin = _seed_record(sf, emp_id, year=2026, month=2, is_finalized=True)

        _login_admin(client, sf)
        res = client.post(
            "/api/config/position-salary/sync",
            json={"adjustment_reason": "年度調薪同步至職位標準"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=unfin).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=fin).one().needs_recalc
                is False
            )

    def test_sync_no_delta_does_not_mark_stale(self, stale_client):
        """員工底薪已等於標準（無 delta、不進 planned_updates）→ 不標 stale。"""
        client, sf, _ = stale_client
        emp_id = _seed_position_employee(
            sf,
            name="班導B",
            emp_no="P002",
            base_salary=39240,
            position="班導",
            bonus_grade="a",
        )
        rec = _seed_record(sf, emp_id, year=2026, month=3)

        _login_admin(client, sf)
        res = client.post(
            "/api/config/position-salary/sync",
            json={"adjustment_reason": "嘗試同步但已對齊標準"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec).one().needs_recalc
                is False
            )


# ─────────────────────────────────────────────────────────────────────────────
# update_job_title：改 bonus_grade → mark 持該職稱員工 stale（#2 P1）
# 引擎 grade_map = {JobTitle.name: bonus_grade}，員工以 Employee.title 字串對應。
# 對稱依據：員工側 bonus_grade 在 _SALARY_INPUT_FIELDS，改員工職稱會標 stale。
# ─────────────────────────────────────────────────────────────────────────────


class TestUpdateJobTitleBonusGradeMarksStale:
    def _seed_title_holder(self, sf, *, title_name, grade):
        from models.database import JobTitle

        with sf() as session:
            jt = JobTitle(name=title_name, bonus_grade=grade, is_active=True)
            session.add(jt)
            session.commit()
            jt_id = jt.id
        emp_id = _seed_position_employee(
            sf,
            name="持職員",
            emp_no="T001",
            base_salary=35000,
            position="班導",
            title=title_name,
        )
        return jt_id, emp_id

    def test_change_bonus_grade_marks_holders_stale(self, stale_client):
        """改職稱 bonus_grade → 持該職稱（title 字串對應 grade_map）員工未封存薪資 stale。"""
        client, sf, _ = stale_client
        jt_id, emp_id = self._seed_title_holder(sf, title_name="幼兒園教師", grade="A")
        unfin = _seed_record(sf, emp_id, year=2026, month=3)
        fin = _seed_record(sf, emp_id, year=2026, month=2, is_finalized=True)

        _login_admin(client, sf)
        res = client.put(
            f"/api/config/titles/{jt_id}",
            json={"name": "幼兒園教師", "bonus_grade": "B"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=unfin).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=fin).one().needs_recalc
                is False
            )

    def _seed_fk_linked_holder(self, sf, *, title_name, grade, emp_title=None):
        """建立以 job_title_id（FK）連結職稱、但 Employee.title 可為 NULL/drift 的在職員工。

        模擬 legacy/migration 殘列或 title 被獨立清空者：引擎以 title_name property
        （FK 的 JobTitle.name 優先、fallback legacy title）解析 bonus_grade，故這類
        員工的節慶獎金等級仍隨 JobTitle.bonus_grade 變動，必須一併標 stale。
        """
        from models.database import JobTitle, Employee

        with sf() as session:
            jt = JobTitle(name=title_name, bonus_grade=grade, is_active=True)
            session.add(jt)
            session.commit()
            jt_id = jt.id

            emp = Employee(
                employee_id="TFK01",
                name="FK持職員",
                base_salary=35000,
                employee_type="regular",
                is_active=True,
                hire_date=date(2025, 1, 1),
                position="班導",
                job_title_id=jt_id,
                title=emp_title,
            )
            session.add(emp)
            session.commit()
            emp_id = emp.id
        return jt_id, emp_id

    def test_change_bonus_grade_marks_fk_linked_holder_with_null_title_stale(
        self, stale_client
    ):
        """FK 連結（job_title_id）但 Employee.title 為 NULL 的員工，改職稱 bonus_grade
        後也須標 stale。引擎以 title_name（FK 優先）解析 grade，純 title 字串過濾會漏
        掉這類員工 → finalize 以舊節慶獎金等級封存（P0 錯帳）。"""
        client, sf, _ = stale_client
        jt_id, emp_id = self._seed_fk_linked_holder(
            sf, title_name="幼兒園教師", grade="A", emp_title=None
        )
        unfin = _seed_record(sf, emp_id, year=2026, month=3)
        fin = _seed_record(sf, emp_id, year=2026, month=2, is_finalized=True)

        _login_admin(client, sf)
        res = client.put(
            f"/api/config/titles/{jt_id}",
            json={"name": "幼兒園教師", "bonus_grade": "B"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=unfin).one().needs_recalc
                is True
            )
            assert (
                session.query(SalaryRecord).filter_by(id=fin).one().needs_recalc
                is False
            )

    def test_change_name_only_does_not_mark_stale(self, stale_client):
        """只改 name、bonus_grade 不送（不變）→ 不標 stale。"""
        client, sf, _ = stale_client
        jt_id, emp_id = self._seed_title_holder(sf, title_name="教保員", grade="B")
        rec = _seed_record(sf, emp_id, year=2026, month=3)

        _login_admin(client, sf)
        res = client.put(
            f"/api/config/titles/{jt_id}",
            json={"name": "教保員（資深）"},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            assert (
                session.query(SalaryRecord).filter_by(id=rec).one().needs_recalc
                is False
            )
