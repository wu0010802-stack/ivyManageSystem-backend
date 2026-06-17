"""年終結算批次簽核 / 核定：sign_supervisor_batch / sign_accounting_batch / finalize_batch。

23+ 員工原本逐筆點 N 次。批次端點對選取結算單逐筆套用與單筆相同守衛（狀態 +
assert_not_self_approval + 職責分離），部分成功：違規者列入 failed 不影響其餘。
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
from models.database import Base, User
from models.employee import Employee
from models.year_end import (
    EmployeeYearEndSnapshot,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from utils.auth import hash_password


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "year-end-batch-sign.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(year_end_router)
    with TestClient(app) as client:
        yield client, sf
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _login(client, username):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "TempPass123"}
    )
    assert res.status_code == 200, res.text


def _add_user(sf, username, perms, *, employee_id=None, role="admin"):
    with sf() as s:
        s.add(
            User(
                username=username,
                password_hash=hash_password("TempPass123"),
                role=role,
                permission_names=perms,
                employee_id=employee_id,
                is_active=True,
            )
        )
        s.commit()


def _seed_cycle(sf, n=3, *, status=YearEndSettlementStatus.DRAFT):
    """cycle + n 員工 + n 結算單；回 (cycle_id, [settlement_ids], [employee_ids])。"""
    with sf() as s:
        cycle = YearEndCycle(
            academic_year=114,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 7, 31),
            bonus_calc_date=date(2026, 1, 15),
            status=YearEndCycleStatus.OPEN,
        )
        s.add(cycle)
        s.flush()
        sids, eids = [], []
        for i in range(n):
            emp = Employee(employee_id=f"E_B{i}", name=f"員工{i}", is_active=True)
            s.add(emp)
            s.flush()
            snap = EmployeeYearEndSnapshot(
                year_end_cycle_id=cycle.id,
                employee_id=emp.id,
                base_salary=Decimal("40000"),
                festival_total=Decimal("0"),
                hire_months=Decimal("12"),
            )
            s.add(snap)
            s.flush()
            st = YearEndSettlement(
                year_end_cycle_id=cycle.id,
                employee_id=emp.id,
                snapshot_id=snap.id,
                payable_amount=Decimal("10000"),
                special_bonus_total=Decimal("0"),
                total_amount=Decimal("10000"),
                status=status,
            )
            s.add(st)
            s.flush()
            sids.append(st.id)
            eids.append(emp.id)
        s.commit()
        return cycle.id, sids, eids


def _status(sf, sid):
    with sf() as s:
        return s.query(YearEndSettlement).get(sid).status


def test_accounting_batch_signs_all_draft(client_with_db):
    client, sf = client_with_db
    _cid, sids, _eids = _seed_cycle(sf, 3)
    _add_user(sf, "acc", ["APPRAISAL_ACCOUNTING"])
    _login(client, "acc")
    res = client.post(
        "/api/year_end/settlements/sign_accounting_batch",
        json={"settlement_ids": sids},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["succeeded_count"] == 3
    assert sorted(data["succeeded_ids"]) == sorted(sids)
    assert data["failed"] == []
    for sid in sids:
        assert _status(sf, sid) == YearEndSettlementStatus.ACCOUNTING_SIGNED


def test_finalize_batch_by_different_user(client_with_db):
    client, sf = client_with_db
    _cid, sids, _eids = _seed_cycle(sf, 2)
    _add_user(sf, "acc", ["APPRAISAL_ACCOUNTING"])
    _add_user(sf, "boss", ["YEAR_END_FINALIZE"])
    _login(client, "acc")
    client.post(
        "/api/year_end/settlements/sign_accounting_batch",
        json={"settlement_ids": sids},
    )
    _login(client, "boss")
    res = client.post(
        "/api/year_end/settlements/finalize_batch", json={"settlement_ids": sids}
    )
    assert res.status_code == 200, res.text
    assert res.json()["succeeded_count"] == 2
    for sid in sids:
        assert _status(sf, sid) == YearEndSettlementStatus.FINALIZED


def test_partial_failure_wrong_status(client_with_db):
    """對 DRAFT 結算單做 finalize-batch → 全部失敗（非會計已簽），不誤改。"""
    client, sf = client_with_db
    _cid, sids, _eids = _seed_cycle(sf, 2)
    _add_user(sf, "boss", ["YEAR_END_FINALIZE"])
    _login(client, "boss")
    res = client.post(
        "/api/year_end/settlements/finalize_batch", json={"settlement_ids": sids}
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["succeeded_count"] == 0
    assert len(data["failed"]) == 2
    assert all("非會計已簽" in f["reason"] for f in data["failed"])
    for sid in sids:
        assert _status(sf, sid) == YearEndSettlementStatus.DRAFT


def test_self_approval_skipped_others_succeed(client_with_db):
    """操作者 employee_id == 某結算單員工 → 該筆 failed（自我核准），其餘成功。"""
    client, sf = client_with_db
    _cid, sids, eids = _seed_cycle(sf, 3)
    # acc 的 employee_id = 第 2 筆結算單的員工 → 該筆應被自我核准守衛擋下
    _add_user(sf, "acc", ["APPRAISAL_ACCOUNTING"], employee_id=eids[1])
    _login(client, "acc")
    res = client.post(
        "/api/year_end/settlements/sign_accounting_batch",
        json={"settlement_ids": sids},
    )
    data = res.json()
    assert data["succeeded_count"] == 2
    assert sids[1] not in data["succeeded_ids"]
    assert any(f["settlement_id"] == sids[1] for f in data["failed"])
    assert _status(sf, sids[1]) == YearEndSettlementStatus.DRAFT


def test_duty_separation_same_user_finalize_fails(client_with_db):
    """同一人會計簽核後又核定 → finalize 失敗（職責分離）。"""
    client, sf = client_with_db
    _cid, sids, _eids = _seed_cycle(sf, 1)
    _add_user(sf, "dual", ["APPRAISAL_ACCOUNTING", "YEAR_END_FINALIZE"])
    _login(client, "dual")
    client.post(
        "/api/year_end/settlements/sign_accounting_batch",
        json={"settlement_ids": sids},
    )
    res = client.post(
        "/api/year_end/settlements/finalize_batch", json={"settlement_ids": sids}
    )
    data = res.json()
    assert data["succeeded_count"] == 0
    assert any("職責分離" in f["reason"] for f in data["failed"])
    assert _status(sf, sids[0]) == YearEndSettlementStatus.ACCOUNTING_SIGNED


def test_empty_ids_400(client_with_db):
    client, sf = client_with_db
    _add_user(sf, "acc", ["APPRAISAL_ACCOUNTING"])
    _login(client, "acc")
    res = client.post(
        "/api/year_end/settlements/sign_accounting_batch", json={"settlement_ids": []}
    )
    assert res.status_code == 400


def test_teacher_blocked(client_with_db):
    client, sf = client_with_db
    _cid, sids, _eids = _seed_cycle(sf, 1)
    _add_user(sf, "teacher", ["APPRAISAL_ACCOUNTING"], role="teacher")
    _login(client, "teacher")
    res = client.post(
        "/api/year_end/settlements/sign_accounting_batch",
        json={"settlement_ids": sids},
    )
    assert res.status_code == 403
