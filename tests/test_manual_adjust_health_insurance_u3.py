"""U3（qa-loop 全掃 2026-06-17，業主裁示「HR 輸入視為基礎健保、系統疊加補充保費」）。

HR 透過 manual_adjust 直接編輯 health_insurance_employee = 把它重設為「基礎健保」
（原已併入的當月獎金二代健保補充保費 fee 被移除）。修補前：因 health_insurance 非
BONUS_FIELDS_FOR_YTD、不觸發 C13 即時重算、也不標 stale → fee 既不在 health_insurance、
也不入 total_deduction → 法定補充保費漏扣、supplementary_health_employee 變幽靈值。

修法（全在 manual_adjust handler）：編輯 health_insurance_employee 時先把
supplementary_health_employee 歸零，再觸發 _recompute_record_current_supplementary，
以「全額」把當月補充保費重新疊回 health_insurance_employee，維持「health_insurance_employee
恆含 fee」不變量（引擎/recompute 下游全自動正確、無 double-count）。

計算鏡像 test_manual_adjust_supplementary_c13：prior festival 100000 + 當月 80000，
threshold=120000 → ytd_after=180000, basis=120000, excess=60000 → fee=60000×0.0211=1266。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.salary as salary_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.salary import router as salary_router
from models.database import Base, Employee, User, SalaryRecord
from utils.auth import hash_password
from datetime import date


class _FakeInsuranceService:
    supplementary_health_rate = 0.0211

    def get_bracket(self, raw):
        return {"amount": raw}


@pytest.fixture
def salary_client(tmp_path):
    db_path = tmp_path / "u3-manual-adjust.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    old_eng_svc = salary_module._salary_engine
    old_ins_svc = salary_module._insurance_service
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    # _salary_engine=None → _recompute 走 SalaryEngine(load_from_db=False) fallback，
    # _load_emp_dict 用 emp.base_salary；_insurance_service 用真 fake 算補充保費。
    salary_module._salary_engine = None
    salary_module._insurance_service = _FakeInsuranceService()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    salary_module._salary_engine = old_eng_svc
    salary_module._insurance_service = old_ins_svc
    engine.dispose()


def _seed(session_factory):
    with session_factory() as session:
        emp = Employee(
            employee_id="U3EMP",
            name="U3測試",
            title="幼兒園教師",
            position="幼兒園教師",
            employee_type="regular",
            base_salary=30000,
            insurance_salary_level=30000,
            hire_date=date(2025, 1, 1),
            is_active=True,
        )
        session.add(emp)
        session.flush()
        # 前月累計 ytd_before = 100000
        session.add(
            SalaryRecord(
                employee_id=emp.id,
                salary_year=2026,
                salary_month=2,
                festival_bonus=100000,
            )
        )
        # 當月 record：fee 已正確併入（不變量維持態）：health=base 458 + fee 1266 = 1724
        rec = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=6,
            base_salary=30000,
            festival_bonus=80000,
            health_insurance_employee=1724,
            supplementary_health_employee=1266,
            gross_salary=110000,
            total_deduction=1724,
            net_salary=108276,
            is_finalized=False,
        )
        session.add(rec)
        session.add(
            User(
                employee_id=None,
                username="adj_admin",
                password_hash=hash_password("AdjPass123"),
                role="admin",
                permission_names=["*"],
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
        return rec.id


def _login(client):
    res = client.post(
        "/api/auth/login", json={"username": "adj_admin", "password": "AdjPass123"}
    )
    assert res.status_code == 200, res.text


def test_edit_health_insurance_remerges_supplementary_fee(salary_client):
    """HR 把 health_insurance 改回基礎 458 → 系統應疊加當月補充保費 1266 = 1724。"""
    client, sf = salary_client
    rec_id = _seed(sf)
    _login(client)

    res = client.put(
        f"/api/salaries/{rec_id}/manual-adjust",
        json={
            "adjustment_reason": "U3：HR 修正基礎健保自付額",
            "health_insurance_employee": 458,
        },
    )
    assert res.status_code == 200, res.text
    # 回應 record dict 未必含 supplementary_health_employee，從 DB 驗權威值
    with sf() as s:
        rec = s.get(SalaryRecord, rec_id)
        assert (
            rec.supplementary_health_employee == 1266
        ), f"當月補充保費應重算為 1266，實際 {rec.supplementary_health_employee}"
        assert rec.health_insurance_employee == 1724, (
            "HR 輸入 458 應視為基礎、系統疊加補充保費 1266 = 1724（否則法定補充保費漏扣），"
            f"實際 {rec.health_insurance_employee}"
        )
        # total_deduction 須含 fee（health_insurance_employee 已含），不可漏
        assert (
            rec.total_deduction or 0
        ) >= 1724, f"total_deduction 應含補充保費(>=1724)，實際 {rec.total_deduction}"
