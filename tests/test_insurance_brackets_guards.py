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


def _create_user(session, username, permission_names):
    if isinstance(permission_names, str):
        permission_names = [permission_names]
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role="hr",
        permission_names=permission_names,
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


SALARY_ONLY = ["SALARY_WRITE", "SALARY_READ"]
SALARY_PLUS_FINANCE = SALARY_ONLY + ["ACTIVITY_PAYMENT_APPROVE"]


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
            _create_user(s, "no_finance", permission_names=SALARY_ONLY)
            s.commit()

        assert _login(client, "no_finance").status_code == 200
        res = client.put("/api/insurance/brackets", json=_BRACKET_PAYLOAD)
        assert res.status_code == 403, res.text
        assert "金流簽核" in res.json()["detail"]

    def test_upsert_blocked_when_reason_too_short(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "with_finance", permission_names=SALARY_PLUS_FINANCE)
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
            _create_user(s, "with_finance", permission_names=SALARY_PLUS_FINANCE)
            # 兩筆未封存薪資（同年）+ 一筆封存薪資（同年）+ 一筆其他年的紀錄
            _seed_salary_record(s, 1, 2026, 1, finalized=False)
            _seed_salary_record(s, 2, 2026, 3, finalized=False)
            _seed_salary_record(s, 3, 2026, 5, finalized=True)
            _seed_salary_record(s, 4, 2025, 12, finalized=False)
            s.commit()

        assert _login(client, "with_finance").status_code == 200
        # 資安掃描 2026-05-07 P2：該年度有封存月份，需 acknowledge_finalized_months=True
        payload_with_ack = dict(_BRACKET_PAYLOAD, acknowledge_finalized_months=True)
        res = client.put("/api/insurance/brackets", json=payload_with_ack)
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
            _create_user(s, "no_finance2", permission_names=SALARY_ONLY)
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
            _create_user(s, "with_finance2", permission_names=SALARY_PLUS_FINANCE)
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
            _create_user(s, "with_finance3", permission_names=SALARY_PLUS_FINANCE)
            b = _seed_bracket(s, year=2026, amount=40000)
            s.commit()
            bid = b.id

        assert _login(client, "with_finance3").status_code == 200
        # 沒帶 body
        res = client.request("DELETE", f"/api/insurance/brackets/{bid}")
        assert res.status_code == 422, res.text


class TestFinalizedMonthsLock:
    """資安掃描 2026-05-07 P2：該年度有封存月份時，PUT/DELETE 預設拒絕，需 ack。"""

    def test_put_blocked_when_finalized_months_exist(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "fin_a", permission_names=SALARY_PLUS_FINANCE)
            _seed_salary_record(s, 10, 2026, 3, finalized=True)
            _seed_salary_record(s, 11, 2026, 4, finalized=True)
            s.commit()

        assert _login(client, "fin_a").status_code == 200
        # 沒帶 ack → 409
        res = client.put("/api/insurance/brackets", json=_BRACKET_PAYLOAD)
        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        assert "封存" in detail
        assert "acknowledge_finalized_months" in detail

    def test_put_succeeds_with_acknowledgement(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "fin_b", permission_names=SALARY_PLUS_FINANCE)
            _seed_salary_record(s, 12, 2026, 3, finalized=True)
            s.commit()

        assert _login(client, "fin_b").status_code == 200
        payload = dict(_BRACKET_PAYLOAD, acknowledge_finalized_months=True)
        res = client.put("/api/insurance/brackets", json=payload)
        assert res.status_code == 200, res.text

    def test_put_no_lock_when_no_finalized_months(self, insurance_client):
        """該年沒有任何封存月份 → 不需要 ack。"""
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "fin_c", permission_names=SALARY_PLUS_FINANCE)
            _seed_salary_record(s, 13, 2026, 5, finalized=False)
            s.commit()

        assert _login(client, "fin_c").status_code == 200
        res = client.put("/api/insurance/brackets", json=_BRACKET_PAYLOAD)
        assert res.status_code == 200, res.text

    def test_delete_blocked_when_finalized_months_exist(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "fin_d", permission_names=SALARY_PLUS_FINANCE)
            b = _seed_bracket(s, year=2026, amount=30000)
            _seed_salary_record(s, 14, 2026, 6, finalized=True)
            s.commit()
            bid = b.id

        assert _login(client, "fin_d").status_code == 200
        res = client.request(
            "DELETE",
            f"/api/insurance/brackets/{bid}",
            json={"reason": "新年度政府公告刪除這級距列"},
        )
        assert res.status_code == 409, res.text
        assert "封存" in res.json()["detail"]

    def test_delete_succeeds_with_acknowledgement(self, insurance_client):
        client, sf = insurance_client
        with sf() as s:
            _create_user(s, "fin_e", permission_names=SALARY_PLUS_FINANCE)
            b = _seed_bracket(s, year=2026, amount=30000)
            _seed_salary_record(s, 15, 2026, 6, finalized=True)
            s.commit()
            bid = b.id

        assert _login(client, "fin_e").status_code == 200
        res = client.request(
            "DELETE",
            f"/api/insurance/brackets/{bid}",
            json={
                "reason": "新年度政府公告刪除這級距列",
                "acknowledge_finalized_months": True,
            },
        )
        assert res.status_code == 200, res.text
