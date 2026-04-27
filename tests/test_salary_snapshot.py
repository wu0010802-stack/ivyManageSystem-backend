"""薪資月底快照機制測試。

涵蓋：
- Service 層：create_month_end（idempotent）、create_finalize、create_manual、
  list、get_detail、diff_with_current
- 核心不變量：重算 SalaryRecord 後 snapshot 金額不變（歷史不可變）
- 端點：GET/POST /api/salaries/snapshots、GET /:id、GET /:id/diff
- 整合：finalize_salary_month 同步寫 type='finalize' 快照
- Lazy trigger：GET /records 觸發背景補拍
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock, patch

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
from models.database import Base, Employee, User, SalaryRecord, SalarySnapshot
from services import salary_snapshot_service as snap_svc
from services import salary_snapshot_scheduler as snap_sched
from utils.auth import hash_password

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def snap_client(tmp_path):
    """in-memory SQLite + TestClient。"""
    db_path = tmp_path / "salary-snapshot.sqlite"
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
    salary_module._snapshot_lazy_guard.clear()

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


def _seed_employee_and_record(
    sf,
    name: str = "測試員工",
    year: int = 2026,
    month: int = 3,
    base_salary: float = 30000,
    health: float = 458,
    labor: float = 738,
    net: float = 28804,
    version: int = 1,
) -> tuple[int, int]:
    """建立員工 + 一筆薪資記錄，回傳 (emp_id, record_id)。"""
    with sf() as session:
        emp = Employee(
            employee_id=f"E_{name}",
            name=name,
            base_salary=base_salary,
            employee_type="regular",
            is_active=True,
        )
        session.add(emp)
        session.flush()
        rec = SalaryRecord(
            employee_id=emp.id,
            salary_year=year,
            salary_month=month,
            base_salary=base_salary,
            health_insurance_employee=health,
            labor_insurance_employee=labor,
            gross_salary=base_salary,
            total_deduction=health + labor,
            net_salary=net,
            version=version,
            is_finalized=False,
        )
        session.add(rec)
        session.commit()
        return emp.id, rec.id


def _seed_admin_and_login(sf, client):
    with sf() as session:
        session.add(
            User(
                employee_id=None,
                username="snap_admin",
                password_hash=hash_password("SnapPass123"),
                role="admin",
                permissions=-1,
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login",
        json={"username": "snap_admin", "password": "SnapPass123"},
    )
    assert res.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Service unit tests
# ─────────────────────────────────────────────────────────────────────────────


class TestPayloadColumnsReflection:
    def test_covers_money_fields(self):
        cols = snap_svc._PAYLOAD_COLUMNS
        for expected in (
            "base_salary",
            "health_insurance_employee",
            "labor_insurance_employee",
            "net_salary",
            "gross_salary",
            "total_deduction",
        ):
            assert expected in cols

    def test_excludes_metadata_fields(self):
        cols = set(snap_svc._PAYLOAD_COLUMNS)
        for meta in (
            "id",
            "salary_record_id",
            "salary_year",
            "salary_month",
            "snapshot_type",
            "captured_at",
            "captured_by",
            "source_version",
            "snapshot_remark",
        ):
            assert meta not in cols


class TestCreateMonthEndSnapshots:
    def test_creates_for_all_records(self, snap_client):
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_employee_and_record(sf, name="B", month=3)
        with sf() as session:
            created = snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        assert created == 2
        with sf() as session:
            cnt = (
                session.query(SalarySnapshot)
                .filter_by(snapshot_type="month_end")
                .count()
            )
            assert cnt == 2

    def test_idempotent_same_month(self, snap_client):
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        # 第二次應 skip 全部
        with sf() as session:
            created = snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        assert created == 0
        with sf() as session:
            assert session.query(SalarySnapshot).count() == 1

    def test_empty_when_no_records(self, snap_client):
        _, sf = snap_client
        with sf() as session:
            created = snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        assert created == 0

    def test_different_months_independent(self, snap_client):
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_employee_and_record(sf, name="A2", month=4)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            snap_svc.create_month_end_snapshots(session, 2026, 4)
            session.commit()
        with sf() as session:
            assert session.query(SalarySnapshot).count() == 2


class TestCreateFinalizeAndManualSnapshot:
    def test_finalize_snapshot(self, snap_client):
        _, sf = snap_client
        _, rec_id = _seed_employee_and_record(sf, month=3)
        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            snap = snap_svc.create_finalize_snapshot(session, rec, "operator")
            session.commit()
            assert snap.snapshot_type == "finalize"
            assert snap.captured_by == "operator"

    def test_manual_snapshot_whole_month(self, snap_client):
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_employee_and_record(sf, name="B", month=3)
        with sf() as session:
            n = snap_svc.create_manual_snapshot(
                session, 2026, 3, "admin", remark="發薪前留底"
            )
            session.commit()
        assert n == 2
        with sf() as session:
            rows = session.query(SalarySnapshot).filter_by(snapshot_type="manual").all()
            assert len(rows) == 2
            assert all(r.snapshot_remark == "發薪前留底" for r in rows)

    def test_manual_snapshot_single_employee(self, snap_client):
        _, sf = snap_client
        emp_a, _ = _seed_employee_and_record(sf, name="A", month=3)
        _seed_employee_and_record(sf, name="B", month=3)
        with sf() as session:
            n = snap_svc.create_manual_snapshot(
                session, 2026, 3, "admin", employee_id=emp_a
            )
            session.commit()
        assert n == 1


# ─────────────────────────────────────────────────────────────────────────────
# 核心不變量：重算 SalaryRecord 後 snapshot 金額不變
# ─────────────────────────────────────────────────────────────────────────────


class TestSnapshotImmutabilityAgainstRecalculation:
    def test_snapshot_survives_record_update(self, snap_client):
        _, sf = snap_client
        _, rec_id = _seed_employee_and_record(sf, month=3, health=458)
        # 建立 month_end snapshot
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        # 模擬「重算」：把 SalaryRecord 的健保費改大（眷屬數增加）
        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            rec.health_insurance_employee = 916  # 原 458 × (1 + 1 眷屬)
            rec.total_deduction = 916 + 738
            rec.net_salary = 30000 - 916 - 738
            rec.version = 2
            session.commit()
        # Snapshot 金額應該維持原值
        with sf() as session:
            snap = (
                session.query(SalarySnapshot).filter_by(snapshot_type="month_end").one()
            )
            assert float(snap.health_insurance_employee) == 458
            assert snap.source_version == 1

    def test_diff_reports_changed_fields(self, snap_client):
        _, sf = snap_client
        _, rec_id = _seed_employee_and_record(sf, month=3, health=458)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            rec.health_insurance_employee = 916
            session.commit()
        with sf() as session:
            snap = session.query(SalarySnapshot).first()
            result = snap_svc.diff_with_current(session, snap.id)
        assert result is not None
        fields = {c["field"]: c for c in result["changes"]}
        assert "health_insurance_employee" in fields
        assert float(fields["health_insurance_employee"]["snapshot"]) == 458
        assert float(fields["health_insurance_employee"]["current"]) == 916


# ─────────────────────────────────────────────────────────────────────────────
# List / detail service
# ─────────────────────────────────────────────────────────────────────────────


class TestListAndDetail:
    def test_list_ordered_desc_by_captured_at(self, snap_client):
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        with sf() as session:
            snap_svc.create_manual_snapshot(session, 2026, 3, "admin", remark="r2")
            session.commit()
        with sf() as session:
            rows = snap_svc.list_snapshots(session, 2026, 3)
        assert len(rows) == 2
        # 最新（manual）排第一
        assert rows[0]["snapshot_type"] == "manual"
        assert rows[1]["snapshot_type"] == "month_end"

    def test_list_filter_by_employee(self, snap_client):
        _, sf = snap_client
        emp_a, _ = _seed_employee_and_record(sf, name="A", month=3)
        _seed_employee_and_record(sf, name="B", month=3)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        with sf() as session:
            rows = snap_svc.list_snapshots(session, 2026, 3, employee_id=emp_a)
        assert len(rows) == 1
        assert rows[0]["employee_id"] == emp_a

    def test_detail_includes_all_payload_fields(self, snap_client):
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3, base_salary=35000)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        with sf() as session:
            snap = session.query(SalarySnapshot).first()
            data = snap_svc.get_snapshot_detail(session, snap.id)
        assert data["base_salary"] == 35000
        assert "employee_name" in data
        assert data["snapshot_type"] == "month_end"


# ─────────────────────────────────────────────────────────────────────────────
# API 端點
# ─────────────────────────────────────────────────────────────────────────────


class TestSnapshotEndpoints:
    def test_list_endpoint(self, snap_client):
        client, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_admin_and_login(sf, client)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        res = client.get("/api/salaries/snapshots?year=2026&month=3")
        assert res.status_code == 200
        body = res.json()
        assert len(body["snapshots"]) == 1
        assert body["snapshots"][0]["snapshot_type"] == "month_end"

    def test_detail_endpoint(self, snap_client):
        client, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_admin_and_login(sf, client)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        with sf() as session:
            snap_id = session.query(SalarySnapshot).first().id
        res = client.get(f"/api/salaries/snapshots/{snap_id}")
        assert res.status_code == 200
        assert res.json()["snapshot_type"] == "month_end"

    def test_detail_404(self, snap_client):
        client, sf = snap_client
        _seed_admin_and_login(sf, client)
        res = client.get("/api/salaries/snapshots/99999")
        assert res.status_code == 404

    def test_manual_snapshot_endpoint(self, snap_client):
        client, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_admin_and_login(sf, client)
        res = client.post(
            "/api/salaries/snapshots?year=2026&month=3",
            json={"remark": "發薪前留底"},
        )
        assert res.status_code == 200
        assert res.json()["count"] == 1
        with sf() as session:
            snap = session.query(SalarySnapshot).one()
            assert snap.snapshot_type == "manual"
            assert snap.snapshot_remark == "發薪前留底"

    def test_manual_snapshot_404_when_no_records(self, snap_client):
        client, sf = snap_client
        _seed_admin_and_login(sf, client)
        res = client.post(
            "/api/salaries/snapshots?year=2026&month=3",
            json={},
        )
        assert res.status_code == 404

    def test_diff_endpoint(self, snap_client):
        client, sf = snap_client
        _, rec_id = _seed_employee_and_record(sf, name="A", month=3, health=458)
        _seed_admin_and_login(sf, client)
        with sf() as session:
            snap_svc.create_month_end_snapshots(session, 2026, 3)
            session.commit()
        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            rec.health_insurance_employee = 916
            session.commit()
        with sf() as session:
            snap_id = session.query(SalarySnapshot).first().id
        res = client.get(f"/api/salaries/snapshots/{snap_id}/diff")
        assert res.status_code == 200
        fields = {c["field"] for c in res.json()["changes"]}
        assert "health_insurance_employee" in fields


# ─────────────────────────────────────────────────────────────────────────────
# Finalize 整合
# ─────────────────────────────────────────────────────────────────────────────


class TestFinalizeIntegration:
    def test_finalize_month_writes_finalize_snapshots(self, snap_client):
        client, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_employee_and_record(sf, name="B", month=3)
        _seed_admin_and_login(sf, client)

        res = client.post(
            "/api/salaries/finalize-month",
            json={"year": 2026, "month": 3},
        )
        assert res.status_code == 200
        assert res.json()["count"] == 2

        with sf() as session:
            snaps = (
                session.query(SalarySnapshot).filter_by(snapshot_type="finalize").all()
            )
            assert len(snaps) == 2
            # 每筆 snapshot 的 captured_by 應為操作者 username
            for s in snaps:
                assert s.captured_by == "snap_admin"

    def test_unfinalize_does_not_delete_snapshot(self, snap_client):
        client, sf = snap_client
        _, rec_id = _seed_employee_and_record(sf, name="A", month=3)
        _seed_admin_and_login(sf, client)
        client.post("/api/salaries/finalize-month", json={"year": 2026, "month": 3})

        # 解封需帶 reason ≥10 字（2026-04-27 守衛）
        res = client.request(
            "DELETE",
            f"/api/salaries/{rec_id}/finalize",
            json={"reason": "回補上游遲到資料後重新封存"},
        )
        assert res.status_code == 200
        # 解封後 finalize snapshot 仍保留
        with sf() as session:
            cnt = (
                session.query(SalarySnapshot)
                .filter_by(snapshot_type="finalize")
                .count()
            )
            assert cnt == 1


# ─────────────────────────────────────────────────────────────────────────────
# Lazy trigger / scheduler
# ─────────────────────────────────────────────────────────────────────────────


class TestLazyTrigger:
    def test_records_endpoint_queues_past_month_snapshot(
        self, snap_client, monkeypatch
    ):
        client, sf = snap_client
        # 用 2026/3 作為「上個月」，系統日期模擬為 2026/4/5
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_admin_and_login(sf, client)

        class FakeDate(date):
            @classmethod
            def today(cls):
                return date(2026, 4, 5)

        # 2026/4 還沒有任何 SalaryRecord → 觸發 /records 時會檢查 2026/3 快照
        salary_module._snapshot_lazy_guard.clear()
        monkeypatch.setattr(salary_module, "date", FakeDate)

        res = client.get("/api/salaries/records?year=2026&month=4")
        assert res.status_code == 200
        # BackgroundTasks 執行完後，3 月的 month_end snapshot 應已建立
        with sf() as session:
            cnt = (
                session.query(SalarySnapshot)
                .filter_by(snapshot_type="month_end", salary_year=2026, salary_month=3)
                .count()
            )
            assert cnt == 1

    def test_lazy_guard_prevents_double_queue_same_day(self, snap_client, monkeypatch):
        client, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        _seed_admin_and_login(sf, client)

        class FakeDate(date):
            @classmethod
            def today(cls):
                return date(2026, 4, 5)

        salary_module._snapshot_lazy_guard.clear()
        monkeypatch.setattr(salary_module, "date", FakeDate)

        client.get("/api/salaries/records?year=2026&month=4")
        client.get("/api/salaries/records?year=2026&month=4")
        with sf() as session:
            cnt = (
                session.query(SalarySnapshot)
                .filter_by(snapshot_type="month_end")
                .count()
            )
            assert cnt == 1  # idempotent + guard 同日不重觸發


class TestSchedulerHelper:
    def test_check_and_snapshot_once_creates_for_previous_month(self, snap_client):
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        created = snap_sched.check_and_snapshot_once(today=date(2026, 4, 15))
        assert created == 1
        with sf() as session:
            assert (
                session.query(SalarySnapshot)
                .filter_by(snapshot_type="month_end")
                .count()
                == 1
            )

    def test_check_and_snapshot_once_idempotent(self, snap_client):
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", month=3)
        snap_sched.check_and_snapshot_once(today=date(2026, 4, 15))
        assert snap_sched.check_and_snapshot_once(today=date(2026, 4, 15)) == 0

    def test_check_and_snapshot_once_handles_january(self, snap_client):
        """1 月時應處理上年 12 月。"""
        _, sf = snap_client
        _seed_employee_and_record(sf, name="A", year=2025, month=12)
        created = snap_sched.check_and_snapshot_once(today=date(2026, 1, 5))
        assert created == 1
        with sf() as session:
            snap = session.query(SalarySnapshot).one()
            assert snap.salary_year == 2025
            assert snap.salary_month == 12
