"""年終結算 FINALIZED 守衛：核定後不可再加 special_bonus 改 total_amount。

威脅：bug sweep 2026-05-16 P0-3。
- add_special_bonus 沒檢查 settlement.status → FINALIZED 也能被改 total_amount
- 反向 race：settlement 在 special_bonus 之後建 → _recompute 函式 silently no-op
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.year_end import year_end_router
from models.auth import User
from models.database import Base
from models.employee import Employee, EmployeeType
from models.year_end import (
    EmployeeYearEndSnapshot,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "ye-finalized-guard.sqlite"
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
    app.include_router(year_end_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


YEAR_END_ALL = ["YEAR_END_READ", "YEAR_END_WRITE", "YEAR_END_FINALIZE"]


def _seed_cycle_with_settlement(sf, settlement_status: YearEndSettlementStatus):
    """建立 admin user + cycle + employee + settlement，回傳 (cycle_id, employee_id)。"""
    with sf() as s:
        emp = Employee(
            employee_id="E001",
            name="員工",
            employee_type=EmployeeType.REGULAR.value,
            is_active=True,
        )
        s.add(emp)
        s.flush()
        s.add(
            User(
                username="admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=YEAR_END_ALL,
                is_active=True,
            )
        )
        cycle = YearEndCycle(
            academic_year=114,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 7, 31),
            bonus_calc_date=date(2026, 1, 15),
            status=YearEndCycleStatus.OPEN,
        )
        s.add(cycle)
        s.flush()
        snapshot = EmployeeYearEndSnapshot(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            base_salary=Decimal("40000"),
            festival_total=Decimal("0"),
            hire_months=Decimal("12"),
        )
        s.add(snapshot)
        s.flush()
        settlement = YearEndSettlement(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            snapshot_id=snapshot.id,
            payable_amount=Decimal("50000"),
            total_amount=Decimal("50000"),
            special_bonus_total=Decimal("0"),
            status=settlement_status,
        )
        s.add(settlement)
        s.flush()
        s.commit()
        return cycle.id, emp.id


def _login(client, username="admin"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "TempPass123"}
    )
    assert res.status_code == 200, res.text


class TestAddSpecialBonusFinalizedGuard:
    def test_rejects_when_settlement_finalized(self, client_with_db):
        client, sf = client_with_db
        cycle_id, emp_id = _seed_cycle_with_settlement(
            sf, YearEndSettlementStatus.FINALIZED
        )
        _login(client)
        res = client.post(
            f"/api/year_end/cycles/{cycle_id}/special_bonuses",
            json={
                "employee_id": emp_id,
                "bonus_type": SpecialBonusType.TEACHING_EXTRA.value,
                "amount": 5000,
                "period_label": "2025下",
                "reason": "事後加錢",
            },
        )
        assert res.status_code == 400, res.text
        assert "FINALIZED" in res.json()["detail"]

        # 確認 total_amount 沒變動，且 special_bonus row 也沒寫入
        with sf() as s:
            settlement = (
                s.query(YearEndSettlement)
                .filter_by(year_end_cycle_id=cycle_id, employee_id=emp_id)
                .one()
            )
            assert settlement.total_amount == Decimal("50000")
            count = (
                s.query(SpecialBonusItem)
                .filter_by(year_end_cycle_id=cycle_id, employee_id=emp_id)
                .count()
            )
            assert count == 0

    def test_rejects_when_settlement_missing(self, client_with_db):
        """反向 race：special_bonus 在 settlement 之前建會漏算 total_amount。"""
        client, sf = client_with_db
        # 只有 cycle，沒 settlement
        with sf() as s:
            emp = Employee(
                employee_id="E001",
                name="員工",
                employee_type=EmployeeType.REGULAR.value,
                is_active=True,
            )
            s.add(emp)
            s.flush()
            s.add(
                User(
                    username="admin",
                    password_hash=hash_password("TempPass123"),
                    role="admin",
                    permission_names=YEAR_END_ALL,
                    is_active=True,
                )
            )
            cycle = YearEndCycle(
                academic_year=114,
                start_date=date(2025, 8, 1),
                end_date=date(2026, 7, 31),
                bonus_calc_date=date(2026, 1, 15),
                status=YearEndCycleStatus.OPEN,
            )
            s.add(cycle)
            s.flush()
            cycle_id = cycle.id
            emp_id = emp.id
            s.commit()

        _login(client)
        res = client.post(
            f"/api/year_end/cycles/{cycle_id}/special_bonuses",
            json={
                "employee_id": emp_id,
                "bonus_type": SpecialBonusType.TEACHING_EXTRA.value,
                "amount": 5000,
                "period_label": "2025下",
                "reason": "先加 special_bonus",
            },
        )
        assert res.status_code == 400, res.text
        assert "尚未建立" in res.json()["detail"]

    @pytest.mark.parametrize(
        "signed_status",
        [
            YearEndSettlementStatus.SUPERVISOR_SIGNED,
            YearEndSettlementStatus.ACCOUNTING_SIGNED,
        ],
    )
    def test_rejects_when_settlement_signed(self, client_with_db, signed_status):
        """P1（2026-06-16）：已簽核（主管/會計）但未核定的年終，仍不可被新增
        special_bonus 改 total_amount。

        威脅：add_special_bonus 原本只擋 FINALIZED，SUPERVISOR_SIGNED /
        ACCOUNTING_SIGNED 會被放行 → 簽章還在，但轉帳金額被 YEAR_END_WRITE 改掉。
        對齊其他 canonical 路徑（build / manual / import）一律以「非 DRAFT」為凍結。
        """
        client, sf = client_with_db
        cycle_id, emp_id = _seed_cycle_with_settlement(sf, signed_status)
        _login(client)
        res = client.post(
            f"/api/year_end/cycles/{cycle_id}/special_bonuses",
            json={
                "employee_id": emp_id,
                "bonus_type": SpecialBonusType.TEACHING_EXTRA.value,
                "amount": 5000,
                "period_label": "2025下",
                "reason": "簽核後事後加錢",
            },
        )
        assert res.status_code == 400, res.text
        assert signed_status.value in res.json()["detail"]

        # 金額未被改動，且沒有 special_bonus row 落地
        with sf() as s:
            settlement = (
                s.query(YearEndSettlement)
                .filter_by(year_end_cycle_id=cycle_id, employee_id=emp_id)
                .one()
            )
            assert settlement.total_amount == Decimal("50000")
            assert settlement.special_bonus_total == Decimal("0")
            count = (
                s.query(SpecialBonusItem)
                .filter_by(year_end_cycle_id=cycle_id, employee_id=emp_id)
                .count()
            )
            assert count == 0

    def test_allows_when_settlement_draft_and_updates_total(self, client_with_db):
        """settlement DRAFT 狀態正常加入特別獎金，total_amount 同步更新。"""
        client, sf = client_with_db
        cycle_id, emp_id = _seed_cycle_with_settlement(
            sf, YearEndSettlementStatus.DRAFT
        )
        _login(client)
        res = client.post(
            f"/api/year_end/cycles/{cycle_id}/special_bonuses",
            json={
                "employee_id": emp_id,
                "bonus_type": SpecialBonusType.TEACHING_EXTRA.value,
                "amount": 5000,
                "period_label": "2025下",
                "reason": "正常加錢",
            },
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            settlement = (
                s.query(YearEndSettlement)
                .filter_by(year_end_cycle_id=cycle_id, employee_id=emp_id)
                .one()
            )
            # special_bonus_total 應為 5000，total_amount 應為 payable(50000) + 5000 = 55000
            assert settlement.special_bonus_total == Decimal("5000")
            assert settlement.total_amount == Decimal("55000")

    def test_duplicate_add_upserts_instead_of_500(self, client_with_db):
        """#8（2026-06-16）：重複 (cycle, emp, bonus_type, period_label) 改為 upsert。

        威脅：對既有 (cycle, emp, bonus_type, period_label) 盲目 INSERT 會撞
        uq_special_bonus_item → IntegrityError 500 並中止交易。改為先查既有列：
        存在則更新 amount，否則新增（比照 _recompute / Excel 匯入路徑）。
        """
        client, sf = client_with_db
        cycle_id, emp_id = _seed_cycle_with_settlement(
            sf, YearEndSettlementStatus.DRAFT
        )
        _login(client)
        body = {
            "employee_id": emp_id,
            "bonus_type": SpecialBonusType.TEACHING_EXTRA.value,
            "amount": 5000,
            "period_label": "2025下",
        }
        # 第一次新增
        res1 = client.post(
            f"/api/year_end/cycles/{cycle_id}/special_bonuses", json=body
        )
        assert res1.status_code == 200, res1.text

        # 第二次同鍵但金額不同 → 應 upsert（200），不可 500
        res2 = client.post(
            f"/api/year_end/cycles/{cycle_id}/special_bonuses",
            json={**body, "amount": 8000},
        )
        assert res2.status_code == 200, res2.text

        with sf() as s:
            items = (
                s.query(SpecialBonusItem)
                .filter_by(
                    year_end_cycle_id=cycle_id,
                    employee_id=emp_id,
                    bonus_type=SpecialBonusType.TEACHING_EXTRA,
                    period_label="2025下",
                )
                .all()
            )
            # 仍只有一筆（沒重複插入）
            assert len(items) == 1
            # amount 被更新為最新值
            assert items[0].amount == Decimal("8000")
            settlement = (
                s.query(YearEndSettlement)
                .filter_by(year_end_cycle_id=cycle_id, employee_id=emp_id)
                .one()
            )
            # total 反映 upsert 後的最新金額（payable 50000 + 8000）
            assert settlement.special_bonus_total == Decimal("8000")
            assert settlement.total_amount == Decimal("58000")
