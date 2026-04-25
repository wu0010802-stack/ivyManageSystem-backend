"""家長端費用查詢測試（Batch 6）。

涵蓋：
- summary 跨子女彙總（outstanding / overdue / due_soon）
- records 列表（IDOR + 隱藏 student 欄位）
- payments 收據（不揭露 operator / refunded_by）
- 跨家長 IDOR
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router as parent_portal_router
from models.database import Base, Classroom, Guardian, Student, User
from models.fees import (
    FeeItem,
    StudentFeePayment,
    StudentFeeRecord,
    StudentFeeRefund,
)
from utils.auth import create_access_token


@pytest.fixture
def fees_client(tmp_path):
    db_path = tmp_path / "fees.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    app.include_router(parent_portal_router)
    with TestClient(app) as client:
        yield client, session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _setup_family(session, *, line_user_id="UF", student_name="小明", classroom_name="向日葵"):
    user = User(
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = (
        session.query(Classroom).filter(Classroom.name == classroom_name).first()
    )
    if not classroom:
        classroom = Classroom(name=classroom_name, is_active=True)
        session.add(classroom)
        session.flush()
    student = Student(
        student_id=f"S_{student_name}",
        name=student_name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="父親",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, guardian, student, classroom


def _create_fee_record(
    session,
    student: Student,
    *,
    fee_item_name="學費",
    amount_due=10000,
    amount_paid=0,
    period="2026-1",
    due_date=None,
    status="unpaid",
):
    item = FeeItem(name=fee_item_name, amount=amount_due, period=period, is_active=True)
    session.add(item)
    session.flush()
    record = StudentFeeRecord(
        student_id=student.id,
        student_name=student.name,
        classroom_name="向日葵",
        fee_item_id=item.id,
        fee_item_name=fee_item_name,
        amount_due=amount_due,
        amount_paid=amount_paid,
        status=status,
        period=period,
        due_date=due_date,
    )
    session.add(record)
    session.flush()
    return record


def _parent_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permissions": 0,
            "token_version": user.token_version or 0,
        }
    )


class TestSummary:
    def test_summary_aggregates_overdue_and_due_soon(self, fees_client):
        client, session_factory = fees_client
        today = date.today()
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            _create_fee_record(
                session, student, fee_item_name="學費", amount_due=10000,
                amount_paid=0, due_date=today - timedelta(days=2),
            )
            _create_fee_record(
                session, student, fee_item_name="材料費", amount_due=2000,
                amount_paid=0, due_date=today + timedelta(days=3),
            )
            _create_fee_record(
                session, student, fee_item_name="制服費", amount_due=1500,
                amount_paid=1500, status="paid",
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/fees/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        data = resp.json()
        totals = data["totals"]
        assert totals["amount_due"] == 13500
        assert totals["amount_paid"] == 1500
        assert totals["outstanding"] == 12000
        assert totals["overdue"] == 10000
        assert totals["due_soon"] == 2000


class TestRecords:
    def test_records_returns_owned_student(self, fees_client):
        client, session_factory = fees_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            _create_fee_record(session, student, fee_item_name="學費")
            session.commit()
            token = _parent_token(user)
            student_id = student.id

        resp = client.get(
            "/api/parent/fees/records",
            params={"student_id": student_id, "period": "2026-1"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["fee_item_name"] == "學費"

    def test_records_other_child_returns_403(self, fees_client):
        client, session_factory = fees_client
        with session_factory() as session:
            user_a, _, _, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A班"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B班"
            )
            session.commit()
            token_a = _parent_token(user_a)
            student_b_id = student_b.id

        resp = client.get(
            "/api/parent/fees/records",
            params={"student_id": student_b_id},
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403


class TestPayments:
    def test_payments_returns_receipt_no_not_operator(self, fees_client):
        client, session_factory = fees_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            record = _create_fee_record(session, student, amount_paid=5000)
            session.add(
                StudentFeePayment(
                    record_id=record.id,
                    amount=5000,
                    payment_date=date(2026, 4, 1),
                    payment_method="現金",
                    operator="財務人員",
                    idempotency_key="RCP-001",
                )
            )
            session.add(
                StudentFeeRefund(
                    record_id=record.id,
                    amount=200,
                    reason="重複繳費",
                    refunded_by="財務人員",
                    refunded_at=datetime.now(),
                )
            )
            session.commit()
            token = _parent_token(user)
            record_id = record.id

        resp = client.get(
            f"/api/parent/fees/records/{record_id}/payments",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["payments"]) == 1
        # 隱私：不洩漏 operator
        assert "operator" not in data["payments"][0]
        assert data["payments"][0]["receipt_no"] == "RCP-001"
        assert len(data["refunds"]) == 1
        # 隱私：不洩漏 refunded_by
        assert "refunded_by" not in data["refunds"][0]

    def test_payments_other_child_returns_403(self, fees_client):
        client, session_factory = fees_client
        with session_factory() as session:
            user_a, _, _, _ = _setup_family(
                session, line_user_id="UA2", student_name="A2", classroom_name="A2班"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB2", student_name="B2", classroom_name="B2班"
            )
            record_b = _create_fee_record(session, student_b)
            session.commit()
            token_a = _parent_token(user_a)
            record_b_id = record_b.id

        resp = client.get(
            f"/api/parent/fees/records/{record_b_id}/payments",
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403
