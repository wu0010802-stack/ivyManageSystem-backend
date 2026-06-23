"""tests/test_activity_public_supply_price_snapshot_2026_06_23.py

code review P2：/public/query 與 /public/update 的 supplies 原本只回 list[str]（名稱），
前端「儲存前退費預警」（feePreview / wouldOverpay）對既有用品無從取得當初的 price_snapshot，
只能用「目前 option 價」估算。後台調降用品價後，家長即使保留原用品（後端 diff 更新保留
原 row 與 price_snapshot、不退費），前端也會誤判 newTotal < paid → wouldOverpay 擋下儲存。

修法：supplies 從 list[str] 擴成 list[{name, price}]，price 取 RegistrationSupply.price_snapshot，
讓前端能對既有用品用 snapshot、新增用品才用目前價，與後端 diff 行為對齊。
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
from api.activity.public import (
    _public_query_limiter_instance,
    _public_register_limiter_instance,
)
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivitySupply,
    Base,
    Classroom,
    Student,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def supply_client(tmp_path):
    db_path = tmp_path / "supply_snapshot.sqlite"
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
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed(session):
    sy, sem = _term()
    classroom = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
    session.add(classroom)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋", price=1000, school_year=sy, semester=sem, is_active=True
        )
    )
    session.add(
        ActivitySupply(
            name="彩色筆", price=300, school_year=sy, semester=sem, is_active=True
        )
    )
    session.add(
        Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 5, 10),
            classroom_id=classroom.id,
            parent_phone="0912345678",
            is_active=True,
        )
    )
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )
    session.commit()


def _register_with_supply(client):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "0912345678",
            "class": "海豚班",
            "courses": [{"name": "圍棋", "price": "1000"}],
            "supplies": [{"name": "彩色筆", "price": "300"}],
        },
    )


def _query(client):
    return client.post(
        "/api/activity/public/query",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "0912345678",
        },
    )


class TestPublicQuerySuppliesCarryPriceSnapshot:
    def test_supplies_returned_as_objects_with_snapshot_price(self, supply_client):
        """supplies 應回 [{name, price}]，price 為報名當下的 price_snapshot。"""
        client, sf = supply_client
        with sf() as s:
            _seed(s)
        reg = _register_with_supply(client)
        assert reg.status_code == 201, reg.text

        res = _query(client)
        assert res.status_code == 200, res.text
        supplies = res.json()["supplies"]
        assert supplies == [{"name": "彩色筆", "price": 300}], supplies

    def test_snapshot_price_not_affected_by_later_db_price_change(self, supply_client):
        """報名後後台調降用品價，query 仍回 snapshot 價（300），非目前 DB 價（100）。"""
        client, sf = supply_client
        with sf() as s:
            _seed(s)
        reg = _register_with_supply(client)
        assert reg.status_code == 201, reg.text

        # 後台調降用品價
        with sf() as s:
            sup = s.query(ActivitySupply).filter(ActivitySupply.name == "彩色筆").one()
            sup.price = 100
            s.commit()

        res = _query(client)
        assert res.status_code == 200, res.text
        supplies = res.json()["supplies"]
        assert len(supplies) == 1
        assert supplies[0]["name"] == "彩色筆"
        assert (
            supplies[0]["price"] == 300
        ), f"應回 snapshot 價 300，實得 {supplies[0]['price']}（誤用目前 DB 價）"
