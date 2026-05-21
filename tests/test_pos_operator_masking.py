"""驗證 /pos/recent-transactions operator 欄位對非金流簽核者遮罩。

威脅：原本任何持 ACTIVITY_READ 的低權限員工都看得到「誰收的款」名單，
可被用於內部竊盜情報蒐集 / 同事監控 / 騷擾。

修法：has_finance_approve（== ACTIVITY_PAYMENT_APPROVE）才回傳真實 operator，
其他角色看到 "[已遮罩]"。

Refs: 資安掃描 2026-05-07 P2。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    Base,
    Classroom,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def pos_client(tmp_path):
    db_path = tmp_path / "pos_op_mask.sqlite"
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
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, *, username, perms, role="staff"):
    if isinstance(perms, str):
        perms = [perms]
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=perms,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Passw0rd!"}
    )


def _seed_payment(session):
    """建一筆已存在的 POS 交易紀錄供 list 端點讀取。"""
    classroom = Classroom(name="海豚班", is_active=True)
    session.add(classroom)
    session.flush()
    course = ActivityCourse(name="圍棋", price=1000, is_active=True)
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name="王小明",
        birthday=date(2020, 5, 1),
        class_name="海豚班",
        parent_phone="0900-111-222",
        is_active=True,
        match_status="matched",
        paid_amount=1000,
    )
    session.add(reg)
    session.flush()
    rec = ActivityPaymentRecord(
        registration_id=reg.id,
        amount=1000,
        type="payment",
        payment_method="現金",
        payment_date=date.today(),
        operator="張三",  # 內部敏感欄位
        receipt_no="POS-20260507-ABCDEF123456",
        notes="[POS-20260507-ABCDEF123456]",
    )
    session.add(rec)
    session.flush()


_READ_ONLY_PERMS = ["ACTIVITY_READ"]
_FINANCE_PERMS = ["ACTIVITY_READ", "ACTIVITY_PAYMENT_APPROVE"]


class TestOperatorMasking:
    def test_operator_masked_for_read_only_user(self, pos_client):
        """只持 ACTIVITY_READ 的員工 → operator 應被遮罩"""
        client, sf = pos_client
        with sf() as s:
            _create_user(s, username="reader", perms=_READ_ONLY_PERMS)
            _seed_payment(s)
            s.commit()

        assert _login(client, "reader").status_code == 200
        res = client.get("/api/activity/pos/recent-transactions")
        assert res.status_code == 200, res.text
        body = res.json()
        assert len(body["transactions"]) == 1
        # operator 不可洩漏 "張三"
        assert body["transactions"][0]["operator"] == "[已遮罩]"
        assert "張三" not in str(body)

    def test_operator_visible_for_finance_approver(self, pos_client):
        """有 ACTIVITY_PAYMENT_APPROVE → 可看真實 operator"""
        client, sf = pos_client
        with sf() as s:
            _create_user(s, username="finance", perms=_FINANCE_PERMS, role="admin")
            _seed_payment(s)
            s.commit()

        assert _login(client, "finance").status_code == 200
        res = client.get("/api/activity/pos/recent-transactions")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["transactions"][0]["operator"] == "張三"
