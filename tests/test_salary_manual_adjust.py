"""
回歸測試：薪資手動調整 (PUT /salaries/{id}/manual-adjust)

涵蓋：
- 時薪制員工 hourly_total 不可被歸零（#1 bug）
- _recalculate_salary_record_totals 重算後 gross_salary 仍含時薪總計
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
from api.auth import router as auth_router, _account_failures, _ip_attempts
import api.salary as salary_module
from api.salary import router as salary_router, _recalculate_salary_record_totals
from models.database import Base, Employee, User, SalaryRecord
from utils.auth import hash_password


@pytest.fixture
def salary_client(tmp_path):
    db_path = tmp_path / "salary-manual-adjust.sqlite"
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

    fake_salary_engine = MagicMock()
    fake_insurance_service = MagicMock()
    salary_module.init_salary_services(fake_salary_engine, fake_insurance_service)

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


def _seed_with_meeting_absence(session_factory):
    """正職員工 + 已扣減的節慶獎金（festival 1800、meeting_absence 200，raw=2000）"""
    with session_factory() as session:
        emp = Employee(
            employee_id="M001",
            name="會議扣減測試",
            base_salary=30000,
            employee_type="regular",
            is_active=True,
        )
        session.add(emp)
        session.flush()
        record = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=6,  # 發放月
            base_salary=30000,
            festival_bonus=1800,
            meeting_absence_deduction=200,
            gross_salary=30000,
            total_deduction=0,
            net_salary=30000,
            is_finalized=False,
        )
        session.add(record)
        user = User(
            employee_id=None,
            username="adj_admin",
            password_hash=hash_password("AdjPass123"),
            role="admin",
            permissions=-1,
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()
        return record.id


def _seed_hourly(session_factory):
    """建立時薪制員工 + 既有薪資記錄（hourly_total=24,000）。"""
    with session_factory() as session:
        emp = Employee(
            employee_id="H001",
            name="時薪測試",
            base_salary=0,
            hourly_rate=200,
            employee_type="hourly",
            is_active=True,
        )
        session.add(emp)
        session.flush()
        record = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=4,
            base_salary=0,
            hourly_total=24000,
            work_hours=120,
            hourly_rate=200,
            gross_salary=24000,
            total_deduction=0,
            net_salary=24000,
            is_finalized=False,
        )
        session.add(record)
        user = User(
            employee_id=None,
            username="adj_admin",
            password_hash=hash_password("AdjPass123"),
            role="admin",
            permissions=-1,
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()
        return record.id


def _login(client):
    res = client.post(
        "/api/auth/login",
        json={"username": "adj_admin", "password": "AdjPass123"},
    )
    assert res.status_code == 200


class TestRecalculatePreservesHourlyTotal:
    def test_recalculate_includes_hourly_total(self):
        """單元測試：_recalculate 必須把 hourly_total 加回 gross_salary"""
        record = SalaryRecord(
            base_salary=0,
            hourly_total=24000,
            other_deduction=500,
        )
        _recalculate_salary_record_totals(record)
        assert record.gross_salary == 24000
        assert record.total_deduction == 500
        assert record.net_salary == 23500

    def test_edit_meeting_absence_alone_recomputes_festival_bonus(self, salary_client):
        """情境：管理員只改 meeting_absence_deduction（200→0，例：會議實際出席被誤標）。
        festival_bonus 應自動回推 raw（1800+200=2000），再以新 absence 重套：
        festival = max(0, 2000 - 0) = 2000。
        """
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "測試連動：清空 meeting_absence",
                "meeting_absence_deduction": 0,
            },
        )
        assert res.status_code == 200
        rec = res.json()["record"]
        assert rec["meeting_absence_deduction"] == 0
        assert rec["festival_bonus"] == 2000

    def test_edit_meeting_absence_partial_recomputes(self, salary_client):
        """meeting_absence 200→100：festival 應變成 max(0, 2000-100)=1900。"""
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "調整會議缺席扣減試算",
                "meeting_absence_deduction": 100,
            },
        )
        assert res.status_code == 200
        assert res.json()["record"]["festival_bonus"] == 1900

    def test_edit_both_festival_and_meeting_absence_no_auto_recompute(
        self, salary_client
    ):
        """同時手動覆寫 festival_bonus 與 meeting_absence_deduction：
        管理員的 festival 為最終值，不再自動回推 raw。"""
        client, sf = salary_client
        record_id = _seed_with_meeting_absence(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "同時覆寫節慶與扣減",
                "festival_bonus": 3000,
                "meeting_absence_deduction": 100,
            },
        )
        assert res.status_code == 200
        rec = res.json()["record"]
        # festival 維持管理員打的 3000，不再自動扣 100
        assert rec["festival_bonus"] == 3000
        assert rec["meeting_absence_deduction"] == 100

    def test_manual_adjust_does_not_zero_hourly_total(self, salary_client):
        """整合測試：時薪制員工被 manual-adjust 改任意欄位後，gross 仍含 hourly_total。"""
        client, sf = salary_client
        record_id = _seed_hourly(sf)
        _login(client)

        # 管理員加 500 元其他扣款
        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={
                "adjustment_reason": "測試時薪 gross 保留",
                "other_deduction": 500,
            },
        )
        assert res.status_code == 200
        rec = res.json()["record"]
        # hourly_total 應仍為 24000，gross_salary 應為 24000
        assert (
            rec["gross_salary"] == 24000
        ), f"時薪制 gross_salary 不應被歸零，得到 {rec['gross_salary']}"
        assert rec["net_salary"] == 23500
