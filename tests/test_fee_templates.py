"""費用範本 CRUD 測試"""

import os
import sys
from datetime import datetime

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
from models.classroom import ClassGrade
from models.database import User
from models.fees import FeeTemplate
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures: app + DB + admin/teacher clients
# ---------------------------------------------------------------------------


@pytest.fixture
def _backend(tmp_path):
    """建立檔案型 SQLite engine，swap global _engine/_SessionFactory，
    讓 api/fees 透過 session_scope() 看到同一份資料。"""
    db_path = tmp_path / "templates.sqlite"
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
    app.include_router(fees_router)

    yield {
        "engine": engine,
        "session_factory": session_factory,
        "app": app,
    }

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def session(_backend):
    """測試用 ORM session（與 API 共用同一 engine）。"""
    s = _backend["session_factory"]()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client_admin(_backend):
    """已登入的 admin 帳號 client（permissions=-1 表全開）。"""
    with _backend["session_factory"]() as s:
        u = User(
            username="tpl_admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permissions=-1,
            is_active=True,
        )
        s.add(u)
        s.commit()

    client = TestClient(_backend["app"])
    r = client.post(
        "/api/auth/login",
        json={"username": "tpl_admin", "password": "Temp123456"},
    )
    assert r.status_code == 200, f"admin login failed: {r.text}"
    yield client
    client.close()


@pytest.fixture
def client_teacher(_backend):
    """已登入的 teacher 帳號 client；用於驗證 require_staff_permission 擋線。"""
    with _backend["session_factory"]() as s:
        u = User(
            username="tpl_teacher",
            password_hash=hash_password("Temp123456"),
            role="teacher",
            permissions=-1,  # 權限位元全開，但 role=teacher 仍應被 403
            is_active=True,
        )
        s.add(u)
        s.commit()

    client = TestClient(_backend["app"])
    r = client.post(
        "/api/auth/login",
        json={"username": "tpl_teacher", "password": "Temp123456"},
    )
    assert r.status_code == 200, f"teacher login failed: {r.text}"
    yield client
    client.close()


@pytest.fixture
def grade_da(session):
    """大班 grade。"""
    g = ClassGrade(name="大班", sort_order=3, is_active=True)
    session.add(g)
    session.commit()
    return g


def _payload(grade_id, **overrides):
    base = {
        "grade_id": grade_id,
        "school_year": 114,
        "semester": 1,
        "fee_type": "registration",
        "name": "114-1 註冊費",
        "amount": 19000,
    }
    base.update(overrides)
    return base


def test_create_template_success(client_admin, grade_da):
    r = client_admin.post("/api/fees/templates", json=_payload(grade_da.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amount"] == 19000
    assert body["fee_type"] == "registration"
    assert body["due_date_offset_days"] == 14  # default


def test_create_template_duplicate_rejected(client_admin, grade_da):
    client_admin.post("/api/fees/templates", json=_payload(grade_da.id))
    r2 = client_admin.post("/api/fees/templates", json=_payload(grade_da.id))
    assert r2.status_code == 409
    assert "已存在" in r2.json()["detail"]


def test_create_template_invalid_fee_type(client_admin, grade_da):
    r = client_admin.post(
        "/api/fees/templates",
        json=_payload(grade_da.id, fee_type="invalid"),
    )
    assert r.status_code == 422


def test_create_monthly_template_breakdown_sum_validated(client_admin, grade_da):
    """月費 breakdown 總和必須等於 amount,否則拒絕。"""
    payload = _payload(
        grade_da.id,
        fee_type="monthly",
        name="月費",
        amount=13000,
        breakdown={"tuition": 8000, "meal": 3000, "transport": 1500},  # 12500 != 13000
    )
    r = client_admin.post("/api/fees/templates", json=payload)
    assert r.status_code == 400
    assert "breakdown" in r.json()["detail"]


def test_create_monthly_template_breakdown_ok(client_admin, grade_da):
    payload = _payload(
        grade_da.id,
        fee_type="monthly",
        name="月費",
        amount=13000,
        breakdown={"tuition": 8500, "meal": 3000, "transport": 1500},
    )
    r = client_admin.post("/api/fees/templates", json=payload)
    assert r.status_code == 200


def test_list_templates_filter_term(client_admin, grade_da, session):
    # 兩個學期各一筆
    session.add_all(
        [
            FeeTemplate(
                grade_id=grade_da.id,
                school_year=114,
                semester=1,
                fee_type="registration",
                name="上",
                amount=19000,
            ),
            FeeTemplate(
                grade_id=grade_da.id,
                school_year=114,
                semester=2,
                fee_type="registration",
                name="下",
                amount=19000,
            ),
        ]
    )
    session.commit()
    r = client_admin.get("/api/fees/templates?school_year=114&semester=1")
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["semester"] == 1


def test_update_template(client_admin, grade_da, session):
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="x",
        amount=19000,
    )
    session.add(t)
    session.commit()
    r = client_admin.put(
        f"/api/fees/templates/{t.id}",
        json={"amount": 20000, "name": "調漲"},
    )
    assert r.status_code == 200
    assert r.json()["amount"] == 20000


def test_delete_template_soft(client_admin, grade_da, session):
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="x",
        amount=19000,
    )
    session.add(t)
    session.commit()
    r = client_admin.delete(f"/api/fees/templates/{t.id}")
    assert r.status_code == 200
    session.refresh(t)
    assert t.is_active is False


def test_template_endpoint_requires_fees_write(client_teacher, grade_da):
    """teacher 沒有 FEES_WRITE,應 403。"""
    r = client_teacher.post("/api/fees/templates", json=_payload(grade_da.id))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 金流守衛：FEES_WRITE 但無 ACTIVITY_PAYMENT_APPROVE 的 user 受限於 50K 門檻
# ---------------------------------------------------------------------------


@pytest.fixture
def client_fees_writer(_backend):
    """只有 FEES_WRITE / 沒有 ACTIVITY_PAYMENT_APPROVE 的 admin。"""
    from utils.permissions import Permission

    perms = int(Permission.FEES_READ | Permission.FEES_WRITE)
    with _backend["session_factory"]() as s:
        u = User(
            username="tpl_fees_only",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permissions=perms,
            is_active=True,
        )
        s.add(u)
        s.commit()

    client = TestClient(_backend["app"])
    r = client.post(
        "/api/auth/login",
        json={"username": "tpl_fees_only", "password": "Temp123456"},
    )
    assert r.status_code == 200, f"fees-only login failed: {r.text}"
    yield client
    client.close()


def test_create_template_under_50k_no_finance_approve_required(
    client_fees_writer, grade_da
):
    """單筆範本金額 ≤ 50K 不需金流簽核（一般月費 NT$10K~30K 走得通）。"""
    r = client_fees_writer.post(
        "/api/fees/templates",
        json=_payload(grade_da.id, amount=50_000),
    )
    assert r.status_code == 200, r.text


def test_create_template_over_50k_requires_finance_approve(
    client_fees_writer, grade_da
):
    """單筆範本金額 > 50K 需 ACTIVITY_PAYMENT_APPROVE,否則 403。

    Regression: 早期錯用 finance_guards 預設閾值 1000,造成任何月費範本都要二簽。
    應沿用 fees 模組的 FEE_PAYMENT_APPROVAL_THRESHOLD (50K)。
    """
    r = client_fees_writer.post(
        "/api/fees/templates",
        json=_payload(grade_da.id, amount=50_001),
    )
    assert r.status_code == 403, r.text
    assert "50,000" in r.json()["detail"]


def test_update_template_small_raise_no_finance_approve(
    client_fees_writer, grade_da, session
):
    """漲幅 ≤ 50K 不需金流簽核。"""
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="x",
        amount=10_000,
    )
    session.add(t)
    session.commit()
    r = client_fees_writer.put(
        f"/api/fees/templates/{t.id}",
        json={"amount": 30_000},
    )
    assert r.status_code == 200, r.text


def test_update_template_big_raise_requires_finance_approve(
    client_fees_writer, grade_da, session
):
    """漲幅 > 50K 需 ACTIVITY_PAYMENT_APPROVE。"""
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="x",
        amount=10_000,
    )
    session.add(t)
    session.commit()
    r = client_fees_writer.put(
        f"/api/fees/templates/{t.id}",
        json={"amount": 100_000},
    )
    assert r.status_code == 403, r.text


def test_update_template_slow_walk_bypass_closed(client_fees_writer, grade_da, session):
    """『慢漲到一次到位』需被擋:既存 50K → 51K 雖 delta=1K,
    但 new=51K > 50K 仍應觸發守衛。

    Regression: 早期只看 delta,可分多次小幅漲到任意金額繞過守衛。
    """
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="x",
        amount=50_000,
    )
    session.add(t)
    session.commit()
    r = client_fees_writer.put(
        f"/api/fees/templates/{t.id}",
        json={"amount": 51_000},
    )
    assert r.status_code == 403, r.text


def test_update_template_big_drop_requires_finance_approve(
    client_fees_writer, grade_da, session
):
    """大降同樣需 ACTIVITY_PAYMENT_APPROVE:80K → 1 等同靜默免收,
    比漲價更難察覺,必須同等級守衛。

    Regression: 早期只擋 new > old,降價路徑完全無守衛。
    Note: 用 amount=1 而非 0(schema 已禁 0 元範本,改用 is_active=False 表達免收)。
    """
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="x",
        amount=80_000,
    )
    session.add(t)
    session.commit()
    r = client_fees_writer.put(
        f"/api/fees/templates/{t.id}",
        json={"amount": 1},
    )
    assert r.status_code == 403, r.text


def test_create_template_zero_amount_rejected(client_admin, grade_da):
    """schema 禁止 0 元範本:要表達『免收』請用 is_active=False 而非 amount=0。

    Regression: 早期 ge=0 允許 0 元範本,在 max-rule 守衛下仍留下
    『0 → 50K』單步可繞過守衛的灰色地帶。
    """
    r = client_admin.post(
        "/api/fees/templates",
        json=_payload(grade_da.id, amount=0),
    )
    assert r.status_code == 422


def test_update_template_zero_amount_rejected(client_admin, grade_da, session):
    """schema 禁止把範本 amount 更新為 0。"""
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="x",
        amount=10_000,
    )
    session.add(t)
    session.commit()
    r = client_admin.put(
        f"/api/fees/templates/{t.id}",
        json={"amount": 0},
    )
    assert r.status_code == 422


def test_update_big_template_no_amount_change_still_guarded(
    client_fees_writer, grade_da, session
):
    """既存大額範本（80K）即使只改 name/breakdown,仍應觸發守衛。

    Why: 既存大額範本的任何屬性修改都可能間接影響收費（例: breakdown 替換）。
    """
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="原名",
        amount=80_000,
    )
    session.add(t)
    session.commit()
    r = client_fees_writer.put(
        f"/api/fees/templates/{t.id}",
        json={"name": "改名"},  # 不動 amount
    )
    assert r.status_code == 403, r.text


def test_delete_small_template_no_finance_approve(
    client_fees_writer, grade_da, session
):
    """停用小額範本（amount ≤ 50K）不需金流簽核。"""
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="registration",
        name="x",
        amount=19_000,
    )
    session.add(t)
    session.commit()
    r = client_fees_writer.delete(f"/api/fees/templates/{t.id}")
    assert r.status_code == 200, r.text


def test_delete_big_template_requires_finance_approve(
    client_fees_writer, grade_da, session
):
    """停用大額範本（amount > 50K）需 ACTIVITY_PAYMENT_APPROVE。

    Regression: 早期 delete_fee_template 完全無守衛,可靜默停用月費範本,
    下次 /generate 該年級全班不出帳 → 收入流失。
    """
    t = FeeTemplate(
        grade_id=grade_da.id,
        school_year=114,
        semester=1,
        fee_type="monthly",
        name="高班月費",
        amount=80_000,
    )
    session.add(t)
    session.commit()
    r = client_fees_writer.delete(f"/api/fees/templates/{t.id}")
    assert r.status_code == 403, r.text
    assert "50,000" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Audit-on-deny: 守衛擋下時,AuditMiddleware 仍應收到 attempted payload
# ---------------------------------------------------------------------------


@pytest.fixture
def _backend_with_audit(tmp_path):
    """同 _backend 但接上 AuditMiddleware 並攔截 _schedule_audit_write。"""
    import json as _json
    from utils import audit as audit_module

    db_path = tmp_path / "tpl_audit.sqlite"
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

    captured: list = []
    original_schedule = audit_module._schedule_audit_write
    audit_module._schedule_audit_write = lambda p: captured.append(p)

    app = FastAPI()
    app.add_middleware(audit_module.AuditMiddleware)
    app.include_router(auth_router)
    app.include_router(fees_router)

    yield {
        "session_factory": session_factory,
        "app": app,
        "captured": captured,
        "json": _json,
    }

    audit_module._schedule_audit_write = original_schedule
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_blocked_create_template_captures_attempted_payload(_backend_with_audit):
    """403 BLOCKED_CREATE 應帶 audit_changes(action=fee_template_create + attempted)。

    Regression: 早期 require_finance_approve 在 audit_changes 設定前 raise,
    middleware 只看到 BLOCKED_CREATE 但沒有業務 context(哪個範本、想設多少金額)。
    """
    from utils.permissions import Permission

    sf = _backend_with_audit["session_factory"]
    with sf() as s:
        s.add(
            User(
                username="tpl_audit_writer",
                password_hash=hash_password("Temp123456"),
                role="admin",
                permissions=int(Permission.FEES_READ | Permission.FEES_WRITE),
                is_active=True,
            )
        )
        g = ClassGrade(name="大班", sort_order=3, is_active=True)
        s.add(g)
        s.commit()
        grade_id = g.id

    client = TestClient(_backend_with_audit["app"])
    r = client.post(
        "/api/auth/login",
        json={"username": "tpl_audit_writer", "password": "Temp123456"},
    )
    assert r.status_code == 200, r.text

    captured = _backend_with_audit["captured"]
    captured.clear()

    # 帶 amount=100K 觸發守衛(無 ACTIVITY_PAYMENT_APPROVE)→ 403
    payload = {
        "grade_id": grade_id,
        "school_year": 114,
        "semester": 1,
        "fee_type": "registration",
        "name": "想偷加的貴範本",
        "amount": 100_000,
    }
    r2 = client.post("/api/fees/templates", json=payload)
    assert r2.status_code == 403, r2.text
    client.close()

    # AuditMiddleware 應記錄 BLOCKED_CREATE 並帶 attempted 業務 context
    audit_writes = [p for p in captured if p["action"].startswith("BLOCKED_")]
    assert len(audit_writes) == 1, captured
    rec = audit_writes[0]
    assert rec["action"] == "BLOCKED_CREATE"
    assert rec["entity_type"] == "fee"
    # entity_id 應是 attempted 範本的複合鍵(grade/year-semester/fee_type)
    assert f"{grade_id}/114-1/registration" in str(rec["entity_id"])
    # changes 應為 JSON 字串,內含 attempted payload
    changes = _backend_with_audit["json"].loads(rec["changes"])
    assert changes["action"] == "fee_template_create"
    assert changes["attempted"]["amount"] == 100_000
    assert changes["attempted"]["name"] == "想偷加的貴範本"
