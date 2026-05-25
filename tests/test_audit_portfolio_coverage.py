"""Integration test：portfolio / contact_book audit 覆蓋（audit 2026-05-25）.

驗證：
- Class A 寫操作走細粒度 entity_type（portfolio_milestone 等），不是 student
- Class B 高敏感下載寫 audit（READ，不 dedup）
- Class C list/read 寫 audit（READ，dedup=True 第 2 次同 key 不寫）
"""

import os
import sys
import time
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    AuditLog,
    Base,
    Classroom,
    Employee,
    Student,
    StudentMeasurement,
    User,
)
from utils.auth import create_access_token, hash_password


@pytest.fixture
def client_with_portfolio(tmp_path):
    """fixture 含 admin + classroom + student + 1 筆 measurement，方便測讀寫 audit。"""
    from api.portfolio.measurements import router as measurements_router
    from api.portfolio.milestones import router as milestones_router

    db_path = tmp_path / "portfolio-audit.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    try:
        emp = Employee(employee_id="A1", name="管理員", is_active=True)
        session.add(emp)
        session.commit()
        emp_id = emp.id

        classroom = Classroom(name="小班")
        session.add(classroom)
        session.commit()

        student = Student(
            student_id="S001",
            name="小明",
            classroom_id=classroom.id,
            is_active=True,
        )
        session.add(student)
        session.commit()
        student_id = student.id

        # Seed 一筆 measurement，list 與 dedup 用得到
        session.add(
            StudentMeasurement(
                student_id=student_id,
                measured_on=date.today(),
                height_cm=100,
                weight_kg=15,
            )
        )
        session.commit()

        admin = User(
            username="admin",
            password_hash=hash_password("Admin1234"),
            role="admin",
            is_active=True,
            permission_names=["*"],
            employee_id=emp_id,
        )
        session.add(admin)
        session.commit()
        admin_id = admin.id
    finally:
        session.close()

    token = create_access_token(
        {
            "user_id": admin_id,
            "employee_id": emp_id,
            "role": "admin",
            "name": "管理員",
            "permission_names": ["*"],
            "token_version": 0,
        }
    )

    from utils.audit import AuditMiddleware

    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.include_router(measurements_router)
    app.include_router(milestones_router)

    client = TestClient(app)
    client.cookies.set("access_token", token)

    # Reset dedup cache so test 不被前一個 test 殘留影響
    from utils import audit as audit_module

    audit_module._audit_read_cache.clear()

    yield client, session_factory, student_id

    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


def _wait_audits(session_factory, **filters):
    """audit 是 fire-and-forget thread；poll 直到看到符合 row 或超時。"""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        session = session_factory()
        try:
            q = session.query(AuditLog)
            for k, v in filters.items():
                q = q.filter(getattr(AuditLog, k) == v)
            rows = q.all()
            if rows:
                return rows
        finally:
            session.close()
        time.sleep(0.05)
    return []


class TestClassA_GranularEntityType:
    """寫操作走細粒度 entity_type，不混進 student。"""

    def test_measurement_create_writes_student_measurement_not_student(
        self, client_with_portfolio
    ):
        client, sf, sid = client_with_portfolio
        res = client.post(
            f"/api/students/{sid}/measurements",
            json={
                "measured_on": date.today().isoformat(),
                "height_cm": 110,
                "weight_kg": 18,
            },
        )
        assert res.status_code == 201, res.text

        # CREATE 應落 entity_type=student_measurement（middleware 攔截）
        rows = _wait_audits(sf, action="CREATE", entity_type="student_measurement")
        assert rows, "CREATE measurement 應落 entity_type=student_measurement audit"

        # 反向：不應誤落 entity_type=student（除非端點主動寫）
        session = sf()
        try:
            wrong = (
                session.query(AuditLog)
                .filter(
                    AuditLog.action == "CREATE",
                    AuditLog.entity_type == "student",
                )
                .all()
            )
            assert (
                not wrong
            ), f"create measurement 不應誤落 student；找到 {len(wrong)} 筆"
        finally:
            session.close()


class TestClassC_DedupBehavior:
    """list/read endpoint 走 dedup=True：同 key 60s 內第二次不寫。"""

    def test_measurement_list_dedups_second_call(self, client_with_portfolio):
        client, sf, sid = client_with_portfolio

        res1 = client.get(f"/api/students/{sid}/measurements")
        assert res1.status_code == 200
        rows1 = _wait_audits(
            sf, action="READ", entity_type="student_measurement", entity_id=str(sid)
        )
        assert len(rows1) == 1, f"第一次 list 應落 1 筆 READ；得 {len(rows1)}"

        res2 = client.get(f"/api/students/{sid}/measurements")
        assert res2.status_code == 200
        # 第二次同 (user, entity_type, entity_id) 60s 內 dedup → 仍為 1 筆
        time.sleep(0.2)  # 給 audit thread 一點時間嘗試寫入
        session = sf()
        try:
            rows2 = (
                session.query(AuditLog)
                .filter(
                    AuditLog.action == "READ",
                    AuditLog.entity_type == "student_measurement",
                    AuditLog.entity_id == str(sid),
                )
                .all()
            )
        finally:
            session.close()
        assert (
            len(rows2) == 1
        ), f"dedup 失敗：第 2 次 list 不應再寫 audit；得 {len(rows2)} 筆"

    def test_milestone_list_records_portfolio_milestone(self, client_with_portfolio):
        """list_milestones 應落 entity_type=portfolio_milestone（不是 student）。"""
        client, sf, sid = client_with_portfolio
        res = client.get(f"/api/students/{sid}/milestones")
        assert res.status_code == 200
        rows = _wait_audits(
            sf, action="READ", entity_type="portfolio_milestone", entity_id=str(sid)
        )
        assert rows, "milestone list 應落 entity_type=portfolio_milestone READ audit"

        # 反證：不應出現 entity_type=student 的 READ
        session = sf()
        try:
            wrong = (
                session.query(AuditLog)
                .filter(AuditLog.action == "READ", AuditLog.entity_type == "student")
                .all()
            )
            assert not wrong, "milestone list 不應誤落 entity_type=student READ"
        finally:
            session.close()
