"""Priority 4 integration test：金流 audit 改成同交易 outbox 後的端對端驗證。

這層測試確認透過完整 FastAPI stack（AuditMiddleware + session_scope）打出去的
PUT /api/fees/records/{id}/pay 與 POST /api/fees/records/{id}/refund：
  1. 真的寫入一筆 AuditLog（不再是 fire-and-forget 漏掉）
  2. middleware 沒有額外寫第二筆（audit_skip 旗標生效）
  3. AuditLog 的 changes 含結構化 metadata（action / payment_id / refund_id）

只有單元測試 write_audit_in_session 不夠：金流 endpoint 走完整 ASGI stack 才能
證明 middleware 互動、session_scope commit 順序、audit_skip 旗標讀取都正確。
"""

import json
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.fees import router as fees_router
from models.base import Base
from models.classroom import Classroom, Student
from models.database import AuditLog, User
from models.fees import FeeItem, StudentFeeRecord
from utils.audit import AuditMiddleware
from utils.auth import hash_password


@pytest.fixture
def fee_audit_client(tmp_path):
    """完整 stack：包含 AuditMiddleware 才能證明同交易 outbox 沒被 middleware 重寫。"""
    db_path = tmp_path / "fees-audit.sqlite"
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
    app.add_middleware(AuditMiddleware)
    app.include_router(auth_router)
    app.include_router(fees_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed(session):
    user = User(
        username="audit_admin",
        password_hash=hash_password("Temp123456"),
        role="admin",
        permissions=-1,
        is_active=True,
    )
    cls = Classroom(name="大班", school_year=2025, semester=1)
    session.add_all([user, cls])
    session.flush()
    st = Student(
        student_id="S00099", name="陳大明", is_active=True, classroom_id=cls.id
    )
    item = FeeItem(name="學費", amount=2000, period="2026-1", is_active=True)
    session.add_all([st, item])
    session.flush()
    rec = StudentFeeRecord(
        student_id=st.id,
        student_name=st.name,
        classroom_name=cls.name,
        fee_item_id=item.id,
        fee_item_name=item.name,
        amount_due=2000,
        amount_paid=0,
        status="unpaid",
        period=item.period,
    )
    session.add(rec)
    session.flush()
    return rec.id


def _login(client):
    return client.post(
        "/api/auth/login",
        json={"username": "audit_admin", "password": "Temp123456"},
    )


def _wait_for_background_audits():
    """AuditMiddleware 採 fire-and-forget 背景寫入；測試結束前等所有背景 task 完成,
    避免「outbox 已寫但 middleware 還沒跑完」造成假陰性。"""
    import asyncio
    from utils.audit import _background_tasks

    async def drain():
        if _background_tasks:
            await asyncio.gather(*list(_background_tasks), return_exceptions=True)

    try:
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            loop.run_until_complete(drain())
    except RuntimeError:
        # 沒 event loop 也代表沒背景 task 在跑
        pass


class TestPayFeeAuditOutbox:
    def test_pay_writes_exactly_one_audit_row(self, fee_audit_client):
        """同交易 outbox：成功繳費應寫入恰好 1 筆 AuditLog（不是 0、不是 2）。"""
        client, sf = fee_audit_client
        with sf() as s:
            rec_id = _seed(s)
            s.commit()

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": "2026-04-15",
                "amount_paid": 2000,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 200, res.text

        _wait_for_background_audits()

        with sf() as s:
            logs = s.query(AuditLog).filter(AuditLog.entity_type == "fee").all()

        assert len(logs) == 1, (
            f"應該恰好有 1 筆 fee audit；實際 {len(logs)}（"
            "outbox 缺寫或 middleware 寫了第二筆）"
        )
        log = logs[0]
        assert log.entity_id == str(rec_id)
        assert "繳費登記" in log.summary
        assert log.changes is not None

        # 結構化 changes 必須含 action 與 payment_id（從 fee_pay outbox payload）
        changes = json.loads(log.changes)
        assert changes["action"] == "fee_pay"
        assert changes["delta"] == 2000
        assert changes["new_paid"] == 2000
        assert changes["payment_id"] is not None

    def test_failed_pay_does_not_leave_orphan_audit(self, fee_audit_client):
        """金流操作失敗（400 業務錯誤）時不應留下 AuditLog。"""
        client, sf = fee_audit_client
        with sf() as s:
            rec_id = _seed(s)
            s.commit()

        assert _login(client).status_code == 200

        # 故意觸發業務錯誤：amount_paid > amount_due
        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": "2026-04-15",
                "amount_paid": 99999,  # 超過 amount_due 2000
                "payment_method": "現金",
            },
        )
        assert res.status_code == 400, res.text

        _wait_for_background_audits()

        with sf() as s:
            logs = s.query(AuditLog).filter(AuditLog.entity_type == "fee").all()
        # 業務驗證在金流寫入前 raise，整個交易被攔截，audit 也不應落地
        assert (
            len(logs) == 0
        ), "失敗交易不應留下 audit row（同交易 outbox 失敗 = audit 跟著消失）"


class TestRefundFeeAuditOutbox:
    def test_refund_writes_exactly_one_audit_row(self, fee_audit_client):
        """同交易 outbox：成功退款應寫入 1 筆 AuditLog 含 refund_id。"""
        client, sf = fee_audit_client
        with sf() as s:
            rec_id = _seed(s)
            s.commit()

        assert _login(client).status_code == 200

        # 先繳費 → 1 筆 audit
        client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": "2026-04-15",
                "amount_paid": 2000,
                "payment_method": "現金",
            },
        )

        # 再退款 → 應該再多 1 筆 audit
        res = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 500, "reason": "家長申請部分退費"},
        )
        assert res.status_code == 201, res.text

        _wait_for_background_audits()

        with sf() as s:
            logs = (
                s.query(AuditLog)
                .filter(AuditLog.entity_type == "fee")
                .order_by(AuditLog.id.asc())
                .all()
            )

        # 1 筆 pay + 1 筆 refund = 2 筆，沒有重複
        assert len(logs) == 2, f"預期 2 筆 audit，實得 {len(logs)}"

        refund_log = logs[1]
        assert "學費退款" in refund_log.summary
        changes = json.loads(refund_log.changes)
        assert changes["action"] == "fee_refund"
        assert changes["refund_amount"] == 500
        assert changes["paid_after"] == 1500
        assert changes["refund_id"] is not None
