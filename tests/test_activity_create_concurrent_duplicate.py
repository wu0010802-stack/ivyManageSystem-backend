"""並發新增同名課程/用品的乾淨 400（Finding P2，2026-06-23）。

問題：create_course / create_supply 先 SELECT 查 active 同名 → 無則 add+commit。
並發兩請求 SELECT 都查不到 → 都 commit → 後到者撞 partial unique index
（`uq_activity_course_name_term` / 用品同構索引）→ IntegrityError 落入 generic
except → raise_safe_500 → 500。應為乾淨 400「名稱已存在」。

重現法（最穩、無 PG-only 盲區）：用 ORM 繞過 API 先寫一筆 active 同名同學期
（模擬 race 中先到者已 commit、跳過 API 層 SELECT 查重），再呼叫 create API
同 name/sy/sem。partial unique index 有 sqlite_where=text("is_active = 1")，
SQLite 同樣 enforce，故能在 SQLite 重現。

DB 隔離：SQLite + monkeypatch base_module（不碰 dev PG），與其他 activity 測試一致。
"""

import os
import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Query, sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivitySupply,
    Base,
    User,
)
from utils.auth import hash_password

PASSWORD = "Temp123456"
SY = 115
SEM = 1


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "concurrent_dup.sqlite"
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


def _preinsert_course(sf, *, name="水彩課"):
    """模擬 race 中先到者已 commit 的 active 同名課程。"""
    with sf() as s:
        s.add(
            ActivityCourse(
                name=name,
                price=300,
                capacity=30,
                school_year=SY,
                semester=SEM,
                is_active=True,
            )
        )
        s.commit()


def _preinsert_supply(sf, *, name="畫具組"):
    """模擬 race 中先到者已 commit 的 active 同名用品。"""
    with sf() as s:
        s.add(
            ActivitySupply(
                name=name,
                price=300,
                school_year=SY,
                semester=SEM,
                is_active=True,
            )
        )
        s.commit()


def _force_dup_check_miss(entity):
    """模擬並發 race 窗口：create 端對 `entity`（ActivityCourse / ActivitySupply）
    的查重 SELECT `.first()` 回 None（另一請求剛寫入但本請求 SELECT 沒看到），
    讓流程走到 add+commit 撞 unique index。

    只攔截針對 entity 的查詢；auth 的 User 查詢等其他 `.first()` 照常委派真實實作，
    避免把使用者載入也打成 None（否則 401）。
    """
    real_first = Query.first

    def fake_first(self):
        if entity in {
            ent["entity"] for ent in self.column_descriptions if ent.get("entity")
        }:
            return None
        return real_first(self)

    return patch.object(Query, "first", autospec=True, side_effect=fake_first)


class TestConcurrentDuplicateName:
    def test_create_course_concurrent_duplicate_returns_clean_400(self, admin_client):
        c, sf = admin_client
        _seed_admin(sf)
        _login(c)
        # 先以 ORM 寫一筆 active 同名（模擬並發先到者已 commit）
        _preinsert_course(sf, name="水彩課")

        # create 端查重 SELECT 在 race 窗口看不到先到者 → 走到 commit 撞 index
        with _force_dup_check_miss(ActivityCourse):
            res = c.post(
                "/api/activity/courses",
                json={
                    "name": "水彩課",
                    "price": 300,
                    "school_year": SY,
                    "semester": SEM,
                },
            )
        assert res.status_code == 400, res.text
        assert "名稱已存在" in res.json()["detail"]

    def test_create_supply_concurrent_duplicate_returns_clean_400(self, admin_client):
        c, sf = admin_client
        _seed_admin(sf)
        _login(c)
        _preinsert_supply(sf, name="畫具組")

        with _force_dup_check_miss(ActivitySupply):
            res = c.post(
                "/api/activity/supplies",
                json={
                    "name": "畫具組",
                    "price": 300,
                    "school_year": SY,
                    "semester": SEM,
                },
            )
        assert res.status_code == 400, res.text
        assert "名稱已存在" in res.json()["detail"]


def _force_dup_check_miss_after_load(entity, *, keep_first=1):
    """update 版 race 模擬：update 端對 `entity` 先 `.first()` 載入目標（必須回真實
    物件，否則 404），再 `.first()` 查重。讓前 `keep_first` 次該 entity 的 `.first()`
    走真實實作（載入），之後回 None（查重在 race 窗口看不到衝突者）→ 走到 commit
    撞 unique index。其他 entity（如 auth User）一律委派真實實作。
    """
    real_first = Query.first
    state = {"seen": 0}

    def fake_first(self):
        is_entity = entity in {
            ent["entity"] for ent in self.column_descriptions if ent.get("entity")
        }
        if is_entity:
            state["seen"] += 1
            if state["seen"] > keep_first:
                return None
        return real_first(self)

    return patch.object(Query, "first", autospec=True, side_effect=fake_first)


def _seed_course(sf, *, name, sy=SY, sem=SEM):
    with sf() as s:
        course = ActivityCourse(
            name=name,
            price=300,
            capacity=30,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        s.add(course)
        s.commit()
        return course.id


def _seed_supply(sf, *, name, sy=SY, sem=SEM):
    with sf() as s:
        supply = ActivitySupply(
            name=name, price=300, school_year=sy, semester=sem, is_active=True
        )
        s.add(supply)
        s.commit()
        return supply.id


class TestUpdateConcurrentDuplicateName:
    """update 改名同名競態：查重 SELECT 在 race 窗口 miss → commit 撞 partial unique
    index。修前 update 端 commit 無 except IntegrityError → raise_safe_500（500）；
    修後比照 create 轉乾淨 400「名稱已存在」。"""

    def test_update_course_concurrent_duplicate_returns_clean_400(self, admin_client):
        c, sf = admin_client
        _seed_admin(sf)
        _login(c)
        # 既有衝突者「新名」（active、同學期）+ 待更新課程「舊名」
        _seed_course(sf, name="新名")
        target_id = _seed_course(sf, name="舊名")

        # 查重在 race 窗口看不到「新名」→ 走到 commit；load 仍取真實 target
        with _force_dup_check_miss_after_load(ActivityCourse, keep_first=1):
            res = c.put(
                f"/api/activity/courses/{target_id}",
                json={"name": "新名"},
            )
        assert res.status_code == 400, res.text
        assert "名稱已存在" in res.json()["detail"]

    def test_update_supply_concurrent_duplicate_returns_clean_400(self, admin_client):
        c, sf = admin_client
        _seed_admin(sf)
        _login(c)
        _seed_supply(sf, name="新名")
        target_id = _seed_supply(sf, name="舊名")

        with _force_dup_check_miss_after_load(ActivitySupply, keep_first=1):
            res = c.put(
                f"/api/activity/supplies/{target_id}",
                json={"name": "新名"},
            )
        assert res.status_code == 400, res.text
        assert "名稱已存在" in res.json()["detail"]
