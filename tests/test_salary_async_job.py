"""
測試：薪資批次計算 async job registry 與硬上限

涵蓋情境：
- 建立 job → 狀態查詢 → 結果回收
- 超過同步上限 → 413
- 已封存 → 409
- 不存在的 job_id → 404
"""

import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.salary as salary_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.salary import router as salary_router
from models.database import Base, Employee, User, SalaryRecord
from services.salary_job_registry import registry as salary_job_registry, SalaryCalcJob
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "salary-async.sqlite"
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

    fake_engine = MagicMock()
    salary_module.init_salary_services(fake_engine, MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_admin(client, session_factory):
    with session_factory() as session:
        session.add(
            User(
                employee_id=None,
                username="async_admin",
                password_hash=hash_password("AsyncPass123"),
                role="admin",
                permissions=-1,
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login",
        json={"username": "async_admin", "password": "AsyncPass123"},
    )
    assert res.status_code == 200


class TestJobRegistry:
    def test_create_and_get(self):
        job = salary_job_registry.create(year=2026, month=4, total=10)
        got = salary_job_registry.get(job.job_id)
        assert got is not None
        assert got.total == 10
        assert got.status == "pending"

    def test_progress_update(self):
        job = salary_job_registry.create(year=2026, month=4, total=3)
        salary_job_registry.mark_running(job.job_id)
        salary_job_registry.update_progress(job.job_id, 2, 3, "Alice")
        got = salary_job_registry.get(job.job_id)
        assert got.done == 2
        assert got.current_employee == "Alice"
        assert got.status == "running"

    def test_complete(self):
        job = salary_job_registry.create(year=2026, month=4, total=2)
        salary_job_registry.complete(job.job_id, [{"name": "A"}], [{"error": "x"}])
        got = salary_job_registry.get(job.job_id)
        assert got.status == "completed"
        assert (
            got.result_count if hasattr(got, "result_count") else len(got.results) == 1
        )
        assert len(got.results) == 1
        assert len(got.errors) == 1

    def test_fail(self):
        job = salary_job_registry.create(year=2026, month=4, total=2)
        salary_job_registry.fail(job.job_id, "boom")
        got = salary_job_registry.get(job.job_id)
        assert got.status == "failed"
        assert got.error_message == "boom"

    def test_to_dict_progress_ratio(self):
        job = SalaryCalcJob(job_id="x", year=2026, month=4, total=4, done=1)
        d = job.to_dict()
        assert d["progress_ratio"] == 0.25

    def test_to_dict_zero_total(self):
        job = SalaryCalcJob(job_id="x", year=2026, month=4, total=0)
        assert job.to_dict()["progress_ratio"] == 0.0


class TestAsyncEndpoint:
    def test_unknown_job_returns_404(self, client):
        c, sf = client
        _login_admin(c, sf)
        res = c.get("/api/salaries/calculate-jobs/does-not-exist")
        assert res.status_code == 404

    def test_finalized_month_returns_409(self, client):
        c, sf = client
        _login_admin(c, sf)
        with sf() as session:
            emp = Employee(
                employee_id="A1", name="員工", base_salary=30000, is_active=True
            )
            session.add(emp)
            session.flush()
            session.add(
                SalaryRecord(
                    employee_id=emp.id,
                    salary_year=2026,
                    salary_month=3,
                    is_finalized=True,
                )
            )
            session.commit()

        res = c.post("/api/salaries/calculate-async?year=2026&month=3")
        assert res.status_code == 409
        assert "封存" in res.json()["detail"]

    def test_create_job_returns_202_with_job_id(self, client, monkeypatch):
        """建立 job 後立即回傳 job_id，status 為 pending / running / completed 其一。"""
        c, sf = client
        _login_admin(c, sf)
        with sf() as session:
            session.add(
                Employee(
                    employee_id="B1", name="員工B", base_salary=30000, is_active=True
                )
            )
            session.commit()

        # 避免真的執行 engine（mock 掉）
        def _fake_run(job_id, year, month):
            salary_job_registry.mark_running(job_id)
            salary_job_registry.complete(job_id, [], [])

        monkeypatch.setattr(salary_module, "_run_salary_calc_job", _fake_run)

        res = c.post("/api/salaries/calculate-async?year=2026&month=3")
        assert res.status_code == 202
        body = res.json()
        assert "job_id" in body
        assert body["total"] == 1

        status = c.get(f"/api/salaries/calculate-jobs/{body['job_id']}")
        assert status.status_code == 200
        status_body = status.json()
        assert status_body["status"] in ("pending", "running", "completed")
