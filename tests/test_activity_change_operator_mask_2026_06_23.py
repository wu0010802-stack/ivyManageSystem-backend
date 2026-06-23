"""tests/test_activity_change_operator_mask_2026_06_23.py

Code review P1（2026-06-23）：金流操作員遮罩可被「修改紀錄」繞過。

繳費明細 / POS 收據對只持 ACTIVITY_READ（無 ACTIVITY_PAYMENT_APPROVE）的使用者
遮罩 operator（_desensitize_operator，首字+***），但 POS checkout / 單筆繳費退費 /
作廢會把 raw operator 寫進 RegistrationChange.changed_by，而
  - GET /activity/changes（settings.get_changes，只需 ACTIVITY_READ）
  - GET /activity/registrations/{id}（get_registration_detail，只需 ACTIVITY_READ）
都原樣回傳 changed_by → 低權限員工可從修改紀錄看到「誰收的款」，繞過遮罩。

修法：對「金流類」change_type 的 changed_by 套用同一把 _desensitize_operator
（has_payment_approve 才見真實經手人）；非金流類（報名/候補/編輯等 admin 操作軌跡）
維持原樣。

DB 隔離：SQLite + monkeypatch base_module（不碰 dev PG），對齊 test_pos_operator_masking。
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
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationChange,
    User,
)
from utils.auth import hash_password

CASHIER = "張三"  # 金流經手人（敏感）
ADMIN_EDITOR = "王五"  # 非金流 admin 操作者
MASKED_CASHIER = "張***"  # _desensitize_operator 首字+***

_READ_ONLY_PERMS = ["ACTIVITY_READ"]
_FINANCE_PERMS = ["ACTIVITY_READ", "ACTIVITY_PAYMENT_APPROVE"]


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "change_op_mask.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _create_user(session, *, username, perms, role="staff"):
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=list(perms),
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


def _seed_reg_with_changes(session) -> int:
    """建一筆報名 + 三筆修改紀錄（2 金流 1 非金流），回傳 registration_id。"""
    classroom = Classroom(name="海豚班", is_active=True)
    session.add(classroom)
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
    # 金流類：POS 繳費 / 單筆繳費（changed_by = 經手人，敏感）
    session.add(
        RegistrationChange(
            registration_id=reg.id,
            student_name=reg.student_name,
            change_type="POS繳費",
            description="POS-X NT$1000，方式：現金",
            changed_by=CASHIER,
        )
    )
    session.add(
        RegistrationChange(
            registration_id=reg.id,
            student_name=reg.student_name,
            change_type="新增繳費記錄",
            description="繳費 NT$500，繳費方式：現金",
            changed_by=CASHIER,
        )
    )
    # 非金流類：admin 編輯基本資料（操作軌跡，維持可見）
    session.add(
        RegistrationChange(
            registration_id=reg.id,
            student_name=reg.student_name,
            change_type="編輯基本資料",
            description="修改備註",
            changed_by=ADMIN_EDITOR,
        )
    )
    session.flush()
    return reg.id


def _changed_by_by_type(items):
    """items: list of change dict（含 change_type/changed_by）→ {change_type: changed_by}。"""
    return {it["change_type"]: it["changed_by"] for it in items}


# ── GET /activity/changes ─────────────────────────────────────────────────────


def test_changes_masks_money_operator_for_read_only(client):
    c, sf = client
    with sf() as s:
        _create_user(s, username="reader", perms=_READ_ONLY_PERMS)
        _seed_reg_with_changes(s)
        s.commit()
    assert _login(c, "reader").status_code == 200

    res = c.get("/api/activity/changes")
    assert res.status_code == 200, res.text
    body = res.json()
    assert CASHIER not in str(body), "金流經手人完整姓名不可洩漏給 ACTIVITY_READ"
    by_type = _changed_by_by_type(body["items"])
    assert by_type["POS繳費"] == MASKED_CASHIER
    assert by_type["新增繳費記錄"] == MASKED_CASHIER
    # 非金流類 admin 操作軌跡維持原樣（scope 僅金流）
    assert by_type["編輯基本資料"] == ADMIN_EDITOR


def test_changes_shows_full_operator_for_finance_approver(client):
    c, sf = client
    with sf() as s:
        _create_user(s, username="finance", perms=_FINANCE_PERMS, role="admin")
        _seed_reg_with_changes(s)
        s.commit()
    assert _login(c, "finance").status_code == 200

    res = c.get("/api/activity/changes")
    assert res.status_code == 200, res.text
    by_type = _changed_by_by_type(res.json()["items"])
    assert by_type["POS繳費"] == CASHIER
    assert by_type["新增繳費記錄"] == CASHIER


# ── GET /activity/registrations/{id} detail ──────────────────────────────────


def test_detail_masks_money_operator_for_read_only(client):
    c, sf = client
    with sf() as s:
        _create_user(s, username="reader2", perms=_READ_ONLY_PERMS)
        reg_id = _seed_reg_with_changes(s)
        s.commit()
    assert _login(c, "reader2").status_code == 200

    res = c.get(f"/api/activity/registrations/{reg_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert CASHIER not in str(
        body
    ), "detail 的金流經手人完整姓名不可洩漏給 ACTIVITY_READ"
    by_type = _changed_by_by_type(body["changes"])
    assert by_type["POS繳費"] == MASKED_CASHIER
    assert by_type["新增繳費記錄"] == MASKED_CASHIER
    assert by_type["編輯基本資料"] == ADMIN_EDITOR


def test_detail_shows_full_operator_for_finance_approver(client):
    c, sf = client
    with sf() as s:
        _create_user(s, username="finance2", perms=_FINANCE_PERMS, role="admin")
        reg_id = _seed_reg_with_changes(s)
        s.commit()
    assert _login(c, "finance2").status_code == 200

    res = c.get(f"/api/activity/registrations/{reg_id}")
    assert res.status_code == 200, res.text
    by_type = _changed_by_by_type(res.json()["changes"])
    assert by_type["POS繳費"] == CASHIER
