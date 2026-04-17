"""學生轉班 / 畢業時，同步更新 ActivityRegistration 的回歸測試。

涵蓋：
- sync_registrations_on_student_transfer：轉班後更新 classroom_id + class_name 快照
- sync_registrations_on_student_deactivate：離園時軟刪當學期才藝報名
- 整合：PUT /api/students/{id} / bulk-transfer / graduate 端點走通
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
from api.students import router as students_router
from models.database import (
    ActivityRegistration,
    Base,
    Classroom,
    Student,
    User,
)

# 確保 student_change_logs 表進入 metadata（graduate/bulk-transfer 端點會寫入此表）
import models.student_log  # noqa: F401
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def sync_client(tmp_path):
    db_path = tmp_path / "sync.sqlite"
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
    app.include_router(students_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _add_admin(session):
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permissions=Permission.STUDENTS_READ
            | Permission.STUDENTS_WRITE
            | Permission.ACTIVITY_READ
            | Permission.ACTIVITY_WRITE,
            is_active=True,
        )
    )
    session.flush()


def _login(client):
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": "TempPass123"}
    )
    assert r.status_code == 200
    return r


def _seed(session):
    sy, sem = resolve_current_academic_term()
    _add_admin(session)
    old = Classroom(name="大象班", is_active=True, school_year=sy, semester=sem)
    new = Classroom(name="長頸鹿班", is_active=True, school_year=sy, semester=sem)
    session.add_all([old, new])
    session.flush()
    stu = Student(
        student_id="S001",
        name="王小明",
        birthday=date(2020, 5, 10),
        classroom_id=old.id,
        parent_phone="0912345678",
        is_active=True,
    )
    session.add(stu)
    session.flush()
    # 當學期才藝報名，模擬靜默比對成功的狀態
    reg = ActivityRegistration(
        student_name="王小明",
        birthday="2020-05-10",
        class_name="大象班",
        classroom_id=old.id,
        school_year=sy,
        semester=sem,
        student_id=stu.id,
        parent_phone="0912345678",
        is_active=True,
        match_status="matched",
        pending_review=False,
    )
    session.add(reg)
    session.commit()
    return {
        "student_id": stu.id,
        "old_classroom_id": old.id,
        "new_classroom_id": new.id,
        "registration_id": reg.id,
    }


class TestTransferSync:
    def test_put_student_classroom_change_syncs_registration(self, sync_client):
        client, sf = sync_client
        with sf() as s:
            ids = _seed(s)
        _login(client)

        res = client.put(
            f"/api/students/{ids['student_id']}",
            json={"classroom_id": ids["new_classroom_id"]},
        )
        assert res.status_code == 200

        with sf() as s:
            reg = (
                s.query(ActivityRegistration).filter_by(id=ids["registration_id"]).one()
            )
            assert reg.classroom_id == ids["new_classroom_id"]
            assert reg.class_name == "長頸鹿班"

    def test_bulk_transfer_syncs_registrations(self, sync_client):
        client, sf = sync_client
        with sf() as s:
            ids = _seed(s)
        _login(client)

        res = client.post(
            "/api/students/bulk-transfer",
            json={
                "student_ids": [ids["student_id"]],
                "target_classroom_id": ids["new_classroom_id"],
            },
        )
        assert res.status_code == 200

        with sf() as s:
            reg = (
                s.query(ActivityRegistration).filter_by(id=ids["registration_id"]).one()
            )
            assert reg.classroom_id == ids["new_classroom_id"]
            assert reg.class_name == "長頸鹿班"

    def test_same_classroom_put_no_change_skipped(self, sync_client):
        """PUT 未變更 classroom_id 時不應誤觸同步（避免不必要寫入）。"""
        client, sf = sync_client
        with sf() as s:
            ids = _seed(s)
        _login(client)

        # 改別的欄位，classroom_id 未送
        res = client.put(
            f"/api/students/{ids['student_id']}",
            json={"parent_name": "爸爸"},
        )
        assert res.status_code == 200
        with sf() as s:
            reg = (
                s.query(ActivityRegistration).filter_by(id=ids["registration_id"]).one()
            )
            assert reg.classroom_id == ids["old_classroom_id"]  # 不變
            assert reg.class_name == "大象班"


class TestDeactivateSync:
    def test_graduate_student_soft_deletes_activity_registration(self, sync_client):
        client, sf = sync_client
        with sf() as s:
            ids = _seed(s)
        _login(client)

        res = client.post(
            f"/api/students/{ids['student_id']}/graduate",
            json={
                "graduation_date": "2026-06-30",
                "status": "已畢業",
            },
        )
        assert res.status_code == 200

        with sf() as s:
            reg = (
                s.query(ActivityRegistration).filter_by(id=ids["registration_id"]).one()
            )
            assert reg.is_active is False

    def test_delete_student_soft_deletes_activity_registration(self, sync_client):
        client, sf = sync_client
        with sf() as s:
            ids = _seed(s)
        _login(client)

        res = client.delete(f"/api/students/{ids['student_id']}")
        assert res.status_code == 200

        with sf() as s:
            reg = (
                s.query(ActivityRegistration).filter_by(id=ids["registration_id"]).one()
            )
            assert reg.is_active is False
