"""Finding 2（2026-06-23 audit）：POS 收據補印明細須為「開立當下」凍結快照，
不可隨付款後的增退課/移用品漂移成現況。

修法：checkout 開立收據時把整張收據 items 明細序列化存進 anchor（第一筆）紀錄的
receipt_items_snapshot；補印（_parse_receipt_response_from_record / print.pdf）優先
讀此 immutable snapshot。舊收據（snapshot=NULL）退回即時重建並標 items_rebuilt_live。
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
from api.activity.pos import _parse_receipt_response_from_record
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.base import Base
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    Classroom,
    RegistrationCourse,
    User,
)
from utils.auth import hash_password

# ── 單元：_parse_receipt_response_from_record 的 snapshot 優先 / legacy 重建 ──


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _reg(session, name="王小明", class_name="海豚班"):
    r = ActivityRegistration(
        student_name=name,
        birthday=date(2020, 1, 1),
        class_name=class_name,
        paid_amount=1000,
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


def _course(session, reg_id, course_name, price):
    course = ActivityCourse(name=course_name, price=price, is_active=True)
    session.add(course)
    session.flush()
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course.id,
        status="enrolled",
        price_snapshot=price,
    )
    session.add(rc)
    session.flush()
    return course, rc


def test_snapshot_is_honored_over_live_registration(session):
    """anchor 有 snapshot → 補印讀凍結明細，即使現行報名已改名/換課也不漂移。"""
    reg = _reg(session, "王小明", "海豚班")
    # 現行報名掛的是「西洋棋」課
    _course(session, reg.id, "西洋棋", 1000)
    receipt_no = "POS-20260301-AAAAAAAAAAAA"
    rec = ActivityPaymentRecord(
        registration_id=reg.id,
        type="payment",
        amount=1000,
        payment_date=date(2026, 3, 1),
        payment_method="現金",
        receipt_no=receipt_no,
        # 開立當下凍結的明細：課程是「圍棋」、學生原名
        receipt_items_snapshot=[
            {
                "registration_id": reg.id,
                "student_name": "王小明",
                "class_name": "海豚班",
                "amount_applied": 1000,
                "courses": [{"name": "圍棋", "price": 1000, "status": "enrolled"}],
                "supplies": [],
            }
        ],
    )
    session.add(rec)
    session.flush()

    out = _parse_receipt_response_from_record(session, rec)
    assert out is not None
    assert out["items_rebuilt_live"] is False
    # 讀 snapshot：課程仍是開立當下的「圍棋」，不是現行的「西洋棋」
    assert out["items"][0]["courses"][0]["name"] == "圍棋"


def test_legacy_record_without_snapshot_rebuilds_live(session):
    """snapshot=NULL（舊收據）→ 即時重建現況明細並標 items_rebuilt_live=True。"""
    reg = _reg(session, "陳小美", "企鵝班")
    _course(session, reg.id, "畫畫", 800)
    receipt_no = "POS-20260301-BBBBBBBBBBBB"
    rec = ActivityPaymentRecord(
        registration_id=reg.id,
        type="payment",
        amount=800,
        payment_date=date(2026, 3, 1),
        payment_method="現金",
        receipt_no=receipt_no,
        receipt_items_snapshot=None,  # 舊收據無快照
    )
    session.add(rec)
    session.flush()

    out = _parse_receipt_response_from_record(session, rec)
    assert out is not None
    assert out["items_rebuilt_live"] is True
    assert out["items"][0]["courses"][0]["name"] == "畫畫"


# ── 整合：checkout 寫入 snapshot；事後改課，補印仍印開立當下明細 ──


@pytest.fixture
def pos_client(tmp_path):
    db_path = tmp_path / "pos_snapshot.sqlite"
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


def _login(client, username):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Passw0rd!"}
    )


def test_checkout_snapshot_then_course_rename_reprint_stable(pos_client, monkeypatch):
    client, sf = pos_client
    with sf() as s:
        u = User(
            username="cashier",
            password_hash=hash_password("Passw0rd!"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
            must_change_password=False,
        )
        s.add(u)
        s.add(Classroom(name="海豚班", is_active=True))
        reg = ActivityRegistration(
            student_name="王小明",
            birthday=date(2020, 5, 1),
            class_name="海豚班",
            is_active=True,
            match_status="matched",
            paid_amount=0,
        )
        s.add(reg)
        s.flush()
        course = ActivityCourse(name="圍棋", price=1000, is_active=True)
        s.add(course)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )
        s.commit()
        reg_id, course_id = reg.id, course.id

    assert _login(client, "cashier").status_code == 200
    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 1000}],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "type": "payment",
            "idempotency_key": "RECEIPTSNAP-0001",
        },
    )
    assert res.status_code == 201, res.text
    receipt_no = res.json()["receipt_no"]

    # 事後把課程改名（模擬付款後異動）
    with sf() as s:
        c = s.get(ActivityCourse, course_id)
        c.name = "改名後圍棋"
        s.commit()

    # 補印：明細應仍為開立當下的「圍棋」，且不顯示 items_rebuilt_live 警語
    captured: dict = {}
    import api.activity.pos as pos_mod

    def _spy(*, receipt):
        captured["receipt"] = receipt
        return b"%PDF-1.4 stub"

    monkeypatch.setattr(pos_mod, "generate_pos_receipt_pdf", _spy)
    res2 = client.get(f"/api/activity/pos/receipts/{receipt_no}/print.pdf")
    assert res2.status_code == 200, res2.text
    items = captured["receipt"]["items"]
    assert items[0]["courses"][0]["name"] == "圍棋"
    assert captured["receipt"]["items_rebuilt_live"] is False
