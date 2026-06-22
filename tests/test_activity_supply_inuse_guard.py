"""才藝用品停用守衛（code review #6，2026-06-22）。

問題：`DELETE /supplies/{id}` 直接 is_active=False，不檢查是否仍被在籍報名引用。
公開查詢仍回傳該用品、前端原樣送回，但公開更新只接受 active 用品 → 家長任何
存檔都 400，被卡住無法修改。

業主裁示：直接擋停用——仍被 active 報名引用時回 409 並提示使用中筆數，
須先處理（移除/改選）才能停用。inactive 報名（軟刪）不算引用，仍可停用。

DB 隔離：SQLite + monkeypatch base_module（不碰 dev PG），與其他 activity 測試一致。
"""

import os
import sys

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
    ActivitySupply,
    Base,
    RegistrationSupply,
    User,
)
from utils.auth import hash_password

PASSWORD = "Temp123456"


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "supply_guard.sqlite"
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


def _login(c):
    r = c.post("/api/auth/login", json={"username": "clerk", "password": PASSWORD})
    assert r.status_code == 200, r.text


def _seed_admin(sf):
    with sf() as s:
        s.add(
            User(
                username="clerk",
                password_hash=hash_password(PASSWORD),
                role="hr",
                permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
                is_active=True,
            )
        )
        s.commit()


def _seed_supply(sf, *, name="畫具", school_year=115, semester=1):
    with sf() as s:
        sup = ActivitySupply(
            name=name,
            price=300,
            school_year=school_year,
            semester=semester,
            is_active=True,
        )
        s.add(sup)
        s.commit()
        return sup.id


def _attach_supply_to_reg(sf, supply_id, *, reg_active=True):
    with sf() as s:
        reg = ActivityRegistration(
            student_name="王小明",
            birthday="2020-05-10",
            class_name="海豚班",
            school_year=115,
            semester=1,
            is_active=reg_active,
            paid_amount=0,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationSupply(
                registration_id=reg.id, supply_id=supply_id, price_snapshot=300
            )
        )
        s.commit()
        return reg.id


class TestSupplyInUseGuard:
    def test_deactivate_supply_in_use_by_active_registration_returns_409(
        self, admin_client
    ):
        c, sf = admin_client
        _seed_admin(sf)
        supply_id = _seed_supply(sf)
        _attach_supply_to_reg(sf, supply_id, reg_active=True)
        _login(c)

        res = c.delete(f"/api/activity/supplies/{supply_id}")
        assert res.status_code == 409, res.text
        # 用品仍為 active（停用被擋下）
        with sf() as s:
            sup = s.query(ActivitySupply).filter_by(id=supply_id).first()
            assert sup.is_active is True

    def test_deactivate_unused_supply_succeeds(self, admin_client):
        c, sf = admin_client
        _seed_admin(sf)
        supply_id = _seed_supply(sf)
        _login(c)

        res = c.delete(f"/api/activity/supplies/{supply_id}")
        assert res.status_code == 200, res.text
        with sf() as s:
            sup = s.query(ActivitySupply).filter_by(id=supply_id).first()
            assert sup.is_active is False

    def test_deactivate_supply_used_only_by_inactive_registration_succeeds(
        self, admin_client
    ):
        c, sf = admin_client
        _seed_admin(sf)
        supply_id = _seed_supply(sf)
        _attach_supply_to_reg(sf, supply_id, reg_active=False)
        _login(c)

        res = c.delete(f"/api/activity/supplies/{supply_id}")
        assert res.status_code == 200, res.text
