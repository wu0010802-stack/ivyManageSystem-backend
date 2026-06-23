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
    StudentFeeAdjustment,
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
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_portal_router)

    # Phase 1c+ (2026-05-18): fees.py now uses Depends(get_parent_db).
    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import make_sqlite_parent_db_override

    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _setup_family(
    session, *, line_user_id="UF", student_name="小明", classroom_name="向日葵"
):
    user = User(
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
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
    record = StudentFeeRecord(
        student_id=student.id,
        student_name=student.name,
        classroom_name="向日葵",
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
            "permission_names": [],
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
                session,
                student,
                fee_item_name="學費",
                amount_due=10000,
                amount_paid=0,
                due_date=today - timedelta(days=2),
            )
            _create_fee_record(
                session,
                student,
                fee_item_name="材料費",
                amount_due=2000,
                amount_paid=0,
                due_date=today + timedelta(days=3),
            )
            _create_fee_record(
                session,
                student,
                fee_item_name="制服費",
                amount_due=1500,
                amount_paid=1500,
                status="paid",
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

    def test_summary_adjustment_scoped_per_period(self, fees_client):
        """折抵須按 (student, period) 套用：114-2 的折抵不可先扣掉 114-1 的逾期欠款。

        後台 summary 已 scope（test_fees.TestFeeSummaryAdjustmentScope），
        家長端原本只按 student_id 加總折抵再 overdue-first 扣，會低估逾期金額。
        """
        client, session_factory = fees_client
        today = date.today()
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            # 114-1：已逾期 10000
            _create_fee_record(
                session,
                student,
                fee_item_name="上學期學費",
                amount_due=10000,
                amount_paid=0,
                period="114-1",
                due_date=today - timedelta(days=2),
            )
            # 114-2：未逾期 10000（due 落在 due_soon 視窗之外）
            _create_fee_record(
                session,
                student,
                fee_item_name="下學期學費",
                amount_due=10000,
                amount_paid=0,
                period="114-2",
                due_date=today + timedelta(days=60),
            )
            # 114-2 折抵 10000：只應抵 114-2，不可碰 114-1 逾期
            session.add(
                StudentFeeAdjustment(
                    student_id=student.id,
                    period="114-2",
                    adjustment_type="prepayment",
                    amount=10000,
                    reason="下學期預繳",
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/fees/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        totals = resp.json()["totals"]
        # 114-1 逾期不可被 114-2 折抵吃掉
        assert totals["overdue"] == 10000
        # 114-2 全額折抵後僅剩 114-1 未繳
        assert totals["outstanding"] == 10000
        assert totals["due_soon"] == 0
        assert totals["adjustment"] == 10000

    def test_summary_adjustment_reduces_outstanding_total_in_overdue_bucket(
        self, fees_client
    ):
        """同一期逾期桶被折抵時，總額桶 outstanding 須同步扣減（不得只扣 overdue 子分類）。

        qa-loop #2：outstanding 是「該期總欠款」總額桶，overdue/due_soon 是其重疊子分類。
        舊版用單一 remaining 依序 overdue→due_soon→outstanding 扣抵，overdue 先吃光 remaining
        後 outstanding（總額桶）扣不到 → totals.outstanding 高報、與 amount_due 自相矛盾。
        """
        client, session_factory = fees_client
        today = date.today()
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            # 114-1：逾期 10000
            _create_fee_record(
                session,
                student,
                fee_item_name="上學期學費",
                amount_due=10000,
                amount_paid=0,
                period="114-1",
                due_date=today - timedelta(days=2),
            )
            # 同期 114-1 折抵 3000
            session.add(
                StudentFeeAdjustment(
                    student_id=student.id,
                    period="114-1",
                    adjustment_type="prepayment",
                    amount=3000,
                    reason="減免",
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/fees/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        totals = resp.json()["totals"]
        assert totals["outstanding"] == 7000, (
            "總額桶 outstanding 須扣折抵 3000 → 7000，"
            f"實際 {totals['outstanding']}（10000=overdue 先吃光 remaining 後 outstanding 漏扣 → 高報）"
        )
        assert totals["overdue"] == 7000
        # amount_due 也應為 7000，與 outstanding 一致（不自相矛盾）
        assert totals["amount_due"] == 7000
        assert totals["adjustment"] == 3000

    def test_summary_full_adjustment_of_overdue_bucket_clears_outstanding_count(
        self, fees_client
    ):
        """逾期桶被同期折抵全額抵銷後，outstanding_count 不得仍計該桶為欠款。"""
        client, session_factory = fees_client
        today = date.today()
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            _create_fee_record(
                session,
                student,
                fee_item_name="上學期學費",
                amount_due=8000,
                amount_paid=0,
                period="114-1",
                due_date=today - timedelta(days=5),
            )
            session.add(
                StudentFeeAdjustment(
                    student_id=student.id,
                    period="114-1",
                    adjustment_type="prepayment",
                    amount=8000,
                    reason="全額減免",
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/fees/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        totals = resp.json()["totals"]
        assert totals["outstanding"] == 0
        assert totals["overdue"] == 0
        assert (
            totals["outstanding_count"] == 0
        ), "逾期桶全額折抵後不應計入 outstanding_count"


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
