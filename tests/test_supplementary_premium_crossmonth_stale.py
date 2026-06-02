"""P1 回歸：手動調整 YTD 累計獎金欄位 → 補充保費跨月傳播 (needs_recalc)。

二代健保補充保費採 per-payment 增額制（query_ytd_bonus_before 以 salary_month < month），
強依賴「前月已正確落帳」。手動調整某月的 festival / overtime / performance / special /
supervisor_dividend（皆列入 BONUS_FIELDS_FOR_YTD）後：
- 當月自身的 supplementary_health_employee 會 stale（reviewer P1-1）
- 同年「之後」月份的 ytd_before 基底也改變 → 補充保費 stale（reviewer P1-2）

本測試鎖定 manual_adjust 觸發器應把「該員工、同年、salary_month >= 異動月、未封存」的
SalaryRecord 標 needs_recalc=True，強制 HR 重跑 calculate 結算補充保費
（recalc 尊重 manual_overrides，故手動獎金值得以保留）。

對照範本：api/insurance.py:_bulk_mark_salary_stale_for_year（級距異動標整年 stale）。
"""

from __future__ import annotations

import os
import sys
from datetime import date

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
from utils.audit import AuditMiddleware
from utils.auth import hash_password


@pytest.fixture
def salary_client(tmp_path, monkeypatch):
    db_path = tmp_path / "suppl-crossmonth-stale.sqlite"
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

    # AuditMiddleware 預設背景寫入會與 TestClient 同步查詢 race，改同步寫入。
    import utils.audit as audit_module

    monkeypatch.setattr(
        audit_module, "_schedule_audit_write", audit_module._write_audit_sync
    )

    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_employee(sf, name="員工A", employee_id_str="A001") -> int:
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


def _seed_record(
    sf,
    emp_id: int,
    *,
    year: int = 2026,
    month: int,
    needs_recalc: bool = False,
    is_finalized: bool = False,
    **fields,
) -> int:
    with sf() as session:
        rec = SalaryRecord(
            employee_id=emp_id,
            salary_year=year,
            salary_month=month,
            base_salary=30000,
            gross_salary=30000,
            net_salary=30000,
            total_deduction=0,
            needs_recalc=needs_recalc,
            is_finalized=is_finalized,
            **fields,
        )
        session.add(rec)
        session.commit()
        return rec.id


def _seed_admin(sf):
    with sf() as session:
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


def _login(client):
    res = client.post(
        "/api/auth/login",
        json={"username": "adj_admin", "password": "AdjPass123"},
    )
    assert res.status_code == 200, res.text


def _get(sf, emp_id, *, year=2026, month):
    with sf() as session:
        return (
            session.query(SalaryRecord)
            .filter_by(employee_id=emp_id, salary_year=year, salary_month=month)
            .first()
        )


def _adjust(client, record_id, **fields):
    return client.put(
        f"/api/salaries/{record_id}/manual-adjust",
        json={"adjustment_reason": "回歸測試補充保費跨月傳播", **fields},
    )


class TestManualAdjustBonusMarksStale:
    def test_adjust_bonus_marks_current_month_stale(self, salary_client):
        """P1-1：改當月 special_bonus（YTD 累計欄位）→ 當月自己標 needs_recalc。"""
        client, sf = salary_client
        emp = _seed_employee(sf)
        rid = _seed_record(sf, emp, month=6, special_bonus=0, needs_recalc=False)
        _seed_admin(sf)
        _login(client)

        res = _adjust(client, rid, special_bonus=50000)
        assert res.status_code == 200, res.text

        rec = _get(sf, emp, month=6)
        assert rec.needs_recalc is True

    def test_adjust_bonus_marks_later_same_year_months_stale(self, salary_client):
        """P1-2：改 6 月 special_bonus → 同年 7 月（後月）也標 stale；
        5 月（前月）與其他員工不受影響。"""
        client, sf = salary_client
        emp = _seed_employee(sf)
        other = _seed_employee(sf, name="員工B", employee_id_str="B001")
        rid_jun = _seed_record(sf, emp, month=6, special_bonus=0)
        _seed_record(sf, emp, month=5)
        _seed_record(sf, emp, month=7)
        _seed_record(sf, other, month=7)  # 其他員工不受影響
        _seed_admin(sf)
        _login(client)

        res = _adjust(client, rid_jun, special_bonus=50000)
        assert res.status_code == 200, res.text

        assert _get(sf, emp, month=7).needs_recalc is True, "後月應標 stale"
        assert _get(sf, emp, month=5).needs_recalc is False, "前月不應被標"
        assert _get(sf, other, month=7).needs_recalc is False, "他員工不應被標"

    def test_adjust_non_bonus_field_does_not_mark_stale(self, salary_client):
        """surgical 守衛：只改 late_deduction（非 YTD 累計欄位）不應觸發跨月 stale。"""
        client, sf = salary_client
        emp = _seed_employee(sf)
        rid_jun = _seed_record(sf, emp, month=6, late_deduction=0)
        _seed_record(sf, emp, month=7)
        _seed_admin(sf)
        _login(client)

        res = _adjust(client, rid_jun, late_deduction=300)
        assert res.status_code == 200, res.text

        assert _get(sf, emp, month=6).needs_recalc is False
        assert _get(sf, emp, month=7).needs_recalc is False

    def test_finalized_later_month_not_marked_stale(self, salary_client):
        """finalize 守衛：同年後月若已封存，不得被標 stale（封存語意不可被重算覆寫）。"""
        client, sf = salary_client
        emp = _seed_employee(sf)
        rid_jun = _seed_record(sf, emp, month=6, special_bonus=0)
        _seed_record(sf, emp, month=7, is_finalized=True)
        _seed_admin(sf)
        _login(client)

        res = _adjust(client, rid_jun, special_bonus=50000)
        assert res.status_code == 200, res.text

        assert _get(sf, emp, month=6).needs_recalc is True
        assert _get(sf, emp, month=7).needs_recalc is False


class TestMarkSalaryStaleFromMonthHelper:
    def test_selection_semantics(self, salary_client):
        """helper 純選取語意：同員工、同年、month >= from_month、未封存才標。"""
        from services.salary.utils import mark_salary_stale_from_month

        _, sf = salary_client
        emp = _seed_employee(sf)
        other = _seed_employee(sf, name="員工B", employee_id_str="B001")
        _seed_record(sf, emp, year=2026, month=5)  # 前月：不標
        _seed_record(sf, emp, year=2026, month=6)  # 當月：標
        _seed_record(sf, emp, year=2026, month=7)  # 後月：標
        _seed_record(sf, emp, year=2026, month=8, is_finalized=True)  # 封存：不標
        _seed_record(sf, emp, year=2025, month=12)  # 他年：不標
        _seed_record(sf, other, year=2026, month=7)  # 他員工：不標

        with sf() as session:
            affected = mark_salary_stale_from_month(session, emp, 2026, 6)
            session.commit()

        assert affected == 2, "只應標當月(6)與後月(7) 兩筆"
        assert _get(sf, emp, month=5).needs_recalc is False
        assert _get(sf, emp, month=6).needs_recalc is True
        assert _get(sf, emp, month=7).needs_recalc is True
        assert _get(sf, emp, month=8).needs_recalc is False
        assert _get(sf, emp, year=2025, month=12).needs_recalc is False
        assert _get(sf, other, month=7).needs_recalc is False
