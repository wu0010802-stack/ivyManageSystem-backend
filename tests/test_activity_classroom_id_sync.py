"""後台新增/改班同步 classroom_id（code review #1，2026-06-22）。

問題：後台手動新增報名與「編輯基本資料」改班時只寫 class_name 快照，沒有寫
ActivityRegistration.classroom_id。教師端 portal 以 classroom_id FK 篩選班級報名
（避免字串比對在轉班後失準）→ 後台建立的報名 classroom_id=NULL 教師端永遠看不到；
改班後 classroom_id 不變 → 舊班教師續留可見、新班教師看不到。

修法：新增/改班時一併寫入 classroom_id = 解析出的 classroom.id。

DB 隔離：SQLite + monkeypatch base_module（不碰 dev PG）。
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
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    User,
)
from utils.auth import hash_password

PASSWORD = "Temp123456"


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "classroom_id_sync.sqlite"
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


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _login(c):
    r = c.post("/api/auth/login", json={"username": "clerk", "password": PASSWORD})
    assert r.status_code == 200, r.text


def _seed(sf):
    sy, sem = _term()
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
        c1 = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
        c2 = Classroom(name="企鵝班", is_active=True, school_year=sy, semester=sem)
        s.add_all([c1, c2])
        s.add(
            ActivityCourse(
                name="圍棋",
                price=1000,
                capacity=30,
                is_active=True,
                school_year=sy,
                semester=sem,
            )
        )
        s.commit()
        return {"c1": c1.id, "c2": c2.id}


def _create_payload(class_name="海豚班"):
    return {
        "name": "王小明",
        "birthday": "2020-05-10",
        "class": class_name,
        "courses": [{"name": "圍棋"}],
        "supplies": [],
    }


class TestClassroomIdSync:
    def test_admin_create_sets_classroom_id(self, admin_client):
        c, sf = admin_client
        ids = _seed(sf)
        _login(c)

        res = c.post("/api/activity/registrations", json=_create_payload("海豚班"))
        assert res.status_code == 201, res.text
        reg_id = res.json()["id"]
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).first()
            assert reg.classroom_id == ids["c1"]

    def test_admin_change_class_updates_classroom_id(self, admin_client):
        c, sf = admin_client
        ids = _seed(sf)
        _login(c)

        reg_id = c.post(
            "/api/activity/registrations", json=_create_payload("海豚班")
        ).json()["id"]

        # 改班：海豚班 → 企鵝班
        res = c.put(
            f"/api/activity/registrations/{reg_id}",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "class": "企鵝班",
                "email": None,
            },
        )
        assert res.status_code == 200, res.text
        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).first()
            assert reg.classroom_id == ids["c2"]
            assert reg.class_name == "企鵝班"
