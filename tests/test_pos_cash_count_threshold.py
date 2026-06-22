"""tests/test_pos_cash_count_threshold.py — 現金盤點門檻以毛流量判斷。

P2 Bug 說明：
原本門檻守衛用 by_method_net["現金"]（= payment - refund 淨額）判斷是否強制盤點。
場景：現金收款 10,000 + 現金退款 8,000 → 淨額 2,000 < 3,000 門檻 → 系統略過強制盤點。
但現金抽屜實際流動了 18,000（10,000 進 + 8,000 出），應視為高流量強制盤點。

修法：門檻改以現金毛流量（payment_gross + refund_gross）判斷，
淨額（cash_snapshot）保留用於對帳差異計算。
"""

import os
import sys
from datetime import date, datetime

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
    RegistrationCourse,
)

from tests.test_activity_pos import _create_admin, _login

APPROVE_PERMS = ["ACTIVITY_READ", "ACTIVITY_WRITE", "ACTIVITY_PAYMENT_APPROVE"]


@pytest.fixture
def threshold_client(tmp_path):
    """獨立的 SQLite DB fixture，避免與其他 test 污染。"""
    db_path = tmp_path / "cash_threshold.sqlite"
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


def _insert_payment(s, reg_id, amount, rec_type="payment"):
    """直接插入 ActivityPaymentRecord，繞過 checkout 端點（方便控制金額）。"""
    s.add(
        ActivityPaymentRecord(
            registration_id=reg_id,
            type=rec_type,
            amount=amount,
            payment_date=date.today(),
            payment_method="現金",
            operator="pos_admin",
            notes="",
            created_at=datetime.now(),
        )
    )


def _make_reg(s, name="測試生"):
    """建立最小 ActivityRegistration（不依賴 _setup_reg 的完整流程）。"""
    course = s.query(ActivityCourse).filter(ActivityCourse.name == "測試課").first()
    if not course:
        course = ActivityCourse(
            name="測試課",
            price=10000,
            capacity=30,
            allow_waitlist=True,
            school_year=114,
            semester=1,
        )
        s.add(course)
        s.flush()
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=0,
        is_paid=False,
        is_active=True,
        school_year=114,
        semester=1,
    )
    s.add(reg)
    s.flush()
    s.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=10000,
        )
    )
    s.flush()
    return reg


# ── Test (a): 毛流量高但淨額低 → 仍應強制盤點 ──────────────────────────────


def test_gross_flow_above_threshold_requires_cash_count(threshold_client):
    """現金收款 10,000 + 現金退款 8,000（淨額 2,000 < 門檻 3,000，毛流量 18,000 >= 門檻）
    → 不傳 actual_cash_count 應回 400。

    修前（用淨額）：2,000 < 3,000 → 跳過強制盤點 → 回 201（BUG）。
    修後（用毛流量）：18,000 >= 3,000 → 強制盤點 → 回 400（正確）。
    """
    client, sf = threshold_client
    target = date.today().isoformat()

    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _make_reg(s, "毛流量測試生")
        # 收款 10,000
        _insert_payment(s, reg.id, 10000, "payment")
        # 退款 8,000（淨額 = 2,000 < 3,000 門檻）
        _insert_payment(s, reg.id, 8000, "refund")
        s.commit()

    assert _login(client).status_code == 200

    # 不傳 actual_cash_count，應被擋（毛流量 18,000 >= 3,000）
    res = client.post(
        f"/api/activity/pos/daily-close/{target}",
        json={"note": "高毛流量但低淨額測試"},
    )
    assert res.status_code == 400, (
        f"預期 400（毛流量 18,000 >= 門檻應強制盤點），實際：{res.status_code}；"
        f"body={res.text}"
    )
    detail = res.json().get("detail", "")
    assert (
        "盤點" in detail or "actual_cash_count" in detail
    ), f"錯誤訊息應提及盤點/actual_cash_count，實際：{detail}"


# ── Test (b): 毛流量低 → 不應強制盤點 ──────────────────────────────────────


def test_low_gross_flow_does_not_require_cash_count(threshold_client):
    """現金收款 1,000 只（淨額 1,000、毛流量 1,000，皆 < 門檻 3,000）
    → 不傳 actual_cash_count 應回 201（不強制盤點）。

    這是保留正確行為的回歸測試：確保低流量日不受影響。
    """
    client, sf = threshold_client
    target = date.today().isoformat()

    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _make_reg(s, "低流量測試生")
        # 只有 1,000 收款
        _insert_payment(s, reg.id, 1000, "payment")
        s.commit()

    assert _login(client).status_code == 200

    # 不傳 actual_cash_count，毛流量 1,000 < 3,000 → 應允許
    res = client.post(
        f"/api/activity/pos/daily-close/{target}",
        json={"note": "低流量測試"},
    )
    assert res.status_code == 201, (
        f"預期 201（毛流量 1,000 < 門檻，不強制盤點），實際：{res.status_code}；"
        f"body={res.text}"
    )


# ── 額外：純淨額高（無退款）→ 仍觸發強制盤點 ──────────────────────────────


def test_high_net_with_no_refund_also_triggers_cash_count(threshold_client):
    """現金收款 5,000、無退款（淨額 = 毛流量 = 5,000 >= 3,000）
    → 不傳 actual_cash_count 應回 400（既有行為不回歸）。
    """
    client, sf = threshold_client
    target = date.today().isoformat()

    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _make_reg(s, "高淨額測試生")
        _insert_payment(s, reg.id, 5000, "payment")
        s.commit()

    assert _login(client).status_code == 200

    res = client.post(
        f"/api/activity/pos/daily-close/{target}",
        json={"note": "高淨額無退款"},
    )
    assert (
        res.status_code == 400
    ), f"預期 400（淨額 5,000 >= 門檻），實際：{res.status_code}；body={res.text}"


# ── 額外：毛流量 >= 門檻，但有填 actual_cash_count → 應通過 ─────────────────


def test_gross_flow_above_threshold_with_cash_count_succeeds(threshold_client):
    """現金收款 10,000 + 退款 8,000（毛流量 18,000 >= 3,000）+ 填 actual_cash_count
    → 應回 201（正確填寫盤點金額後可通過）。

    確保修改不會讓「有填盤點」的正常情況被擋。
    """
    client, sf = threshold_client
    target = date.today().isoformat()

    with sf() as s:
        _create_admin(s, permission_names=APPROVE_PERMS)
        reg = _make_reg(s, "毛流量填盤點測試生")
        _insert_payment(s, reg.id, 10000, "payment")
        _insert_payment(s, reg.id, 8000, "refund")
        s.commit()

    assert _login(client).status_code == 200

    # 淨額 2,000；填 2,000 作為盤點金額
    res = client.post(
        f"/api/activity/pos/daily-close/{target}",
        json={"note": "有填盤點", "actual_cash_count": 2000},
    )
    assert res.status_code == 201, (
        f"預期 201（毛流量 >= 門檻但有填 actual_cash_count），"
        f"實際：{res.status_code}；body={res.text}"
    )
    body = res.json()
    # cash_variance = actual_cash_count - cash_snapshot(net)
    # = 2000 - 2000 = 0
    assert body.get("cash_variance") == 0, f"cash_variance 應為 0，實際：{body}"
