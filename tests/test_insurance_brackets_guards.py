"""驗證 InsuranceBracket PUT/DELETE 加上的金流簽核 + reason + bulk mark stale 守衛。

威脅：admin/HR 雖只需 SALARY_WRITE 即可呼叫，但級距表變更（縮表 / 下調）會
讓所有員工保費自動以新值算回，且因 _select_active_at 重讀 brackets/InsuranceRate，
歷史月份補算也跟著漂。

修補：
- PUT/DELETE 額外要求 has_finance_approve（即 ACTIVITY_PAYMENT_APPROVE）
- reason ≥10 字落 audit
- 寫入後對該 effective_year 所有未封存 SalaryRecord bulk mark needs_recalc=True

Refs: 邏輯漏洞 audit 2026-05-07 P0 (#9)。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.insurance import router as insurance_router
from models.database import Base, InsuranceBracket, SalaryRecord, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def insurance_client(tmp_path):
    db_path = tmp_path / "insurance_brackets_guards.sqlite"
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
    app.include_router(insurance_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username, permissions: int):
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role="hr",
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


def _seed_bracket(session, year=2026, amount=30000):
    b = InsuranceBracket(
        effective_year=year,
        amount=amount,
        labor_employee=600,
        labor_employer=2700,
        health_employee=440,
        health_employer=1500,
        pension=900,
    )
    session.add(b)
    session.flush()
    return b


def _seed_salary_record(session, employee_id, year, month, finalized=False):
    rec = SalaryRecord(
        employee_id=employee_id,
        salary_year=year,
        salary_month=month,
        is_finalized=finalized,
        needs_recalc=False,
    )
    session.add(rec)
    session.flush()
    return rec


SALARY_ONLY = int(Permission.SALARY_WRITE) | int(Permission.SALARY_READ)
SALARY_PLUS_FINANCE = SALARY_ONLY | int(Permission.ACTIVITY_PAYMENT_APPROVE)


_BRACKET_PAYLOAD = {
    "effective_year": 2026,
    "brackets": [
        {
            "amount": 35000,
            "labor_employee": 700,
            "labor_employer": 2900,
            "health_employee": 480,
            "health_employer": 1700,
            "pension": 1050,
        }
    ],
    "replace_existing": False,
    "reason": "新年度政府公告調整級距表內容",
}


class TestUpsertBracketsGuards:
    def test_upsert_blocked_without_finance_approve(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "no_finance", permissions=SALARY_ONLY)
            s.commit()

        assert _login(client, "no_finance").status_code == 200
        res = client.put("/api/insurance/brackets", json=_BRACKET_PAYLOAD)
        assert res.status_code == 403, res.text
        assert "金流簽核" in res.json()["detail"]

    def test_upsert_blocked_when_reason_too_short(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "with_finance", permissions=SALARY_PLUS_FINANCE)
            s.commit()

        assert _login(client, "with_finance").status_code == 200
        bad = dict(_BRACKET_PAYLOAD)
        bad["reason"] = "短"  # < 10 字
        res = client.put("/api/insurance/brackets", json=bad)
        # Pydantic 會在 schema 層擋下（min_length=10）→ 422
        assert res.status_code == 422, res.text

    def test_upsert_succeeds_and_marks_year_records_stale(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "with_finance", permissions=SALARY_PLUS_FINANCE)
            # 兩筆未封存薪資（同年）+ 一筆封存薪資（同年）+ 一筆其他年的紀錄
            _seed_salary_record(s, 1, 2026, 1, finalized=False)
            _seed_salary_record(s, 2, 2026, 3, finalized=False)
            _seed_salary_record(s, 3, 2026, 5, finalized=True)
            _seed_salary_record(s, 4, 2025, 12, finalized=False)
            s.commit()

        assert _login(client, "with_finance").status_code == 200
        res = client.put("/api/insurance/brackets", json=_BRACKET_PAYLOAD)
        assert res.status_code == 200, res.text
        body = res.json()
        # 該年未封存 2 筆都應被標 stale；封存的 / 跨年的不動
        assert body["stale_marked"] == 2

        with sf() as s:
            r1 = (
                s.query(SalaryRecord)
                .filter_by(employee_id=1, salary_year=2026, salary_month=1)
                .one()
            )
            r2 = (
                s.query(SalaryRecord)
                .filter_by(employee_id=2, salary_year=2026, salary_month=3)
                .one()
            )
            r3_finalized = (
                s.query(SalaryRecord)
                .filter_by(employee_id=3, salary_year=2026, salary_month=5)
                .one()
            )
            r4_other_year = (
                s.query(SalaryRecord)
                .filter_by(employee_id=4, salary_year=2025, salary_month=12)
                .one()
            )
            assert r1.needs_recalc is True
            assert r2.needs_recalc is True
            # 已封存的不被標 stale（保留封存語意）
            assert r3_finalized.needs_recalc is False
            assert r3_finalized.is_finalized is True
            # 跨年不影響
            assert r4_other_year.needs_recalc is False


class TestDeleteBracketGuards:
    def test_delete_blocked_without_finance_approve(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "no_finance2", permissions=SALARY_ONLY)
            b = _seed_bracket(s, year=2026, amount=30000)
            s.commit()
            bid = b.id

        assert _login(client, "no_finance2").status_code == 200
        res = client.request(
            "DELETE",
            f"/api/insurance/brackets/{bid}",
            json={"reason": "新年度政府公告刪除這級距列"},
        )
        assert res.status_code == 403, res.text
        assert "金流簽核" in res.json()["detail"]

    def test_delete_succeeds_and_marks_stale(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "with_finance2", permissions=SALARY_PLUS_FINANCE)
            b = _seed_bracket(s, year=2026, amount=30000)
            _seed_salary_record(s, 5, 2026, 4, finalized=False)
            s.commit()
            bid = b.id

        assert _login(client, "with_finance2").status_code == 200
        res = client.request(
            "DELETE",
            f"/api/insurance/brackets/{bid}",
            json={"reason": "新年度政府公告刪除這級距列"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["stale_marked"] == 1
        assert body["effective_year"] == 2026

        with sf() as s:
            rec = (
                s.query(SalaryRecord)
                .filter_by(employee_id=5, salary_year=2026, salary_month=4)
                .one()
            )
            assert rec.needs_recalc is True

    def test_delete_blocked_when_reason_missing(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "with_finance3", permissions=SALARY_PLUS_FINANCE)
            b = _seed_bracket(s, year=2026, amount=40000)
            s.commit()
            bid = b.id

        assert _login(client, "with_finance3").status_code == 200
        # 沒帶 body
        res = client.request("DELETE", f"/api/insurance/brackets/{bid}")
        assert res.status_code == 422, res.text
