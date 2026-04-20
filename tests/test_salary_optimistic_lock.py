"""
回歸測試：薪資手動調整樂觀鎖（If-Match / version）

Bug 情境：
    兩位管理員同時打開同一筆薪資編輯，A 先送出扣款調整，B 在不知情下
    送出津貼調整。舊版因無版本控制，B 的寫入會靜默覆蓋 A 的變更（資料遺失）。

修復：
    SalaryRecord 新增 version 欄位，PUT /manual-adjust 驗證 If-Match header。
    若請求版本與 DB 當前版本不符，返回 409 Conflict。
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
from api.salary import router as salary_router, _parse_if_match
from models.database import Base, Employee, User, SalaryRecord
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def salary_client(tmp_path):
    db_path = tmp_path / "salary-optimistic-lock.sqlite"
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


def _seed(session_factory):
    with session_factory() as session:
        emp = Employee(
            employee_id="L001",
            name="樂觀鎖測試員工",
            base_salary=30000,
            is_active=True,
        )
        session.add(emp)
        session.flush()
        record = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=4,
            base_salary=30000,
            gross_salary=30000,
            total_deduction=0,
            net_salary=30000,
            is_finalized=False,
        )
        session.add(record)
        user = User(
            employee_id=None,
            username="lock_admin",
            password_hash=hash_password("LockPass123"),
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
        json={"username": "lock_admin", "password": "LockPass123"},
    )
    assert res.status_code == 200


class TestIfMatchParse:
    def test_parse_plain_number(self):
        assert _parse_if_match("3") == 3

    def test_parse_quoted(self):
        assert _parse_if_match('"3"') == 3

    def test_parse_weak_etag(self):
        assert _parse_if_match('W/"3"') == 3

    def test_parse_none(self):
        assert _parse_if_match(None) is None

    def test_parse_invalid(self):
        assert _parse_if_match("abc") is None


class TestOptimisticLock:
    def test_initial_version_is_one(self, salary_client):
        client, sf = salary_client
        _seed(sf)
        _login(client)
        res = client.get("/api/salaries/records?year=2026&month=4")
        assert res.status_code == 200
        records = res.json()
        assert len(records) == 1
        assert records[0]["version"] == 1

    def test_adjust_increments_version_and_returns_etag(self, salary_client):
        client, sf = salary_client
        record_id = _seed(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"other_deduction": 1000},
        )
        assert res.status_code == 200
        assert res.headers.get("ETag") == '"2"'
        assert res.headers.get("X-Record-Version") == "2"
        assert res.json()["record"]["version"] == 2

    def test_stale_if_match_returns_409(self, salary_client):
        client, sf = salary_client
        record_id = _seed(sf)
        _login(client)

        # 第一次編輯（版本 1 → 2）
        res1 = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"other_deduction": 1000},
            headers={"If-Match": '"1"'},
        )
        assert res1.status_code == 200
        assert res1.headers.get("ETag") == '"2"'

        # 第二個客戶端仍持有 v1，嘗試編輯應被拒絕
        res2 = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"other_deduction": 500},
            headers={"If-Match": '"1"'},
        )
        assert res2.status_code == 409
        assert "已被他人修改" in res2.json()["detail"]

    def test_fresh_if_match_after_reload_succeeds(self, salary_client):
        client, sf = salary_client
        record_id = _seed(sf)
        _login(client)

        # 第一次編輯
        res1 = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"other_deduction": 1000},
            headers={"If-Match": '"1"'},
        )
        assert res1.status_code == 200

        # 重新讀取後取得 v2，再用 v2 送出
        res2 = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"other_deduction": 500},
            headers={"If-Match": '"2"'},
        )
        assert res2.status_code == 200
        assert res2.headers.get("X-Record-Version") == "3"

    def test_missing_if_match_still_allowed_for_backcompat(self, salary_client):
        """不帶 If-Match 的舊版客戶端仍可寫入（版本號仍會累加）。"""
        client, sf = salary_client
        record_id = _seed(sf)
        _login(client)

        res = client.put(
            f"/api/salaries/{record_id}/manual-adjust",
            json={"other_deduction": 1000},
        )
        assert res.status_code == 200
        assert res.json()["record"]["version"] == 2
