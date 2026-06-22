"""編輯基本資料的異動紀錄不得寫入完整家長 Email（code review #4，2026-06-22）。

問題：編輯基本資料改 Email 時，把完整 email 明文寫進 RegistrationChange.description；
而 `GET /activity/changes` 只需 ACTIVITY_READ 即可原樣取得 description，繞過 GUARDIANS_READ
的 Email 遮罩。

修法：異動紀錄只記「Email 已變更」，不寫實際 email 值（其餘姓名/生日/班級為學生身分、
後台列表本就顯示；email 是家長聯絡 PII）。仍計入 changed 數量。

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
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationChange,
    User,
)
from utils.auth import hash_password

PASSWORD = "Temp123456"
OLD_EMAIL = "old.parent@example.com"
NEW_EMAIL = "new.parent@example.com"


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "email_mask.sqlite"
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
        s.add(c1)
        s.flush()
        reg = ActivityRegistration(
            student_name="王小明",
            birthday="2020-05-10",
            class_name="海豚班",
            classroom_id=c1.id,
            email=OLD_EMAIL,
            school_year=sy,
            semester=sem,
            is_active=True,
            paid_amount=0,
        )
        s.add(reg)
        s.commit()
        return reg.id


class TestChangeEmailMask:
    def test_email_change_record_excludes_full_email(self, admin_client):
        c, sf = admin_client
        reg_id = _seed(sf)
        _login(c)

        res = c.put(
            f"/api/activity/registrations/{reg_id}",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "class": "海豚班",
                "email": NEW_EMAIL,
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["changed"] >= 1  # email 變更仍計入

        with sf() as s:
            rows = (
                s.query(RegistrationChange)
                .filter(RegistrationChange.registration_id == reg_id)
                .all()
            )
            descs = " | ".join(r.description or "" for r in rows)
            # 不得洩漏新舊完整 email
            assert NEW_EMAIL not in descs, descs
            assert OLD_EMAIL not in descs, descs
            # 仍須標示 email 有變更（審計可見「改過」但不見值）
            assert "Email" in descs or "email" in descs, descs

    def test_email_value_not_leaked_via_changes_endpoint(self, admin_client):
        c, sf = admin_client
        reg_id = _seed(sf)
        _login(c)

        c.put(
            f"/api/activity/registrations/{reg_id}",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "class": "海豚班",
                "email": NEW_EMAIL,
            },
        )
        res = c.get("/api/activity/changes")
        assert res.status_code == 200, res.text
        body = res.text
        assert NEW_EMAIL not in body, body
        assert OLD_EMAIL not in body, body
