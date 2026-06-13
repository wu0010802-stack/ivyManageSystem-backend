"""tests/test_activity_crud_fixes.py — 才藝 CRUD 修補批次（2026-06-13）

涵蓋：
- K1 copy_courses_from_previous 漏複製 Phase 3 五欄位
- K2 軟刪課程/用品後同學期同名重建（partial unique index）
- K3 delete_session 稽核 log
- K4 list_sessions skip/limit 驗證
- K6 inquiry phone 寬鬆格式驗證
- K7 inquiry list 全量 unread_count
"""

import os
import sys
from datetime import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import (
    _public_inquiry_limiter_instance,
    _public_register_limiter_instance,
)
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import ActivityCourse, ActivitySupply, Base, User
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password


@pytest.fixture
def client_factory(tmp_path):
    db_path = tmp_path / "crud_fixes.sqlite"
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
    _public_inquiry_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    _public_inquiry_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _setup_admin(sf, client):
    with sf() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
                is_active=True,
            )
        )
        s.commit()
    res = client.post(
        "/api/auth/login", json={"username": "admin", "password": "TempPass123"}
    )
    assert res.status_code == 200


# ────────────────────────────────────────────────────────────────── #
# K1 — copy_courses_from_previous 漏複製 Phase 3 五欄位
# ────────────────────────────────────────────────────────────────── #


class TestCopyCoursesCarriesPhase3Fields:
    def test_copy_preserves_age_and_schedule_fields(self, client_factory):
        """複製上學期課程必須帶上 min/max_age_months + meeting_* 五欄位。"""
        client, sf = client_factory
        _setup_admin(sf, client)

        sy, sem = resolve_current_academic_term()
        src_sy, src_sem = (sy - 1, 2) if sem == 1 else (sy, 1)
        with sf() as s:
            s.add(
                ActivityCourse(
                    name="陶藝創作",
                    price=2400,
                    sessions=12,
                    capacity=8,
                    school_year=src_sy,
                    semester=src_sem,
                    min_age_months=36,
                    max_age_months=72,
                    meeting_weekday=2,
                    meeting_start_time=time(15, 30),
                    meeting_end_time=time(16, 30),
                )
            )
            s.commit()

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": src_sy,
                "source_semester": src_sem,
                "target_school_year": sy,
                "target_semester": sem,
            },
        )
        assert res.status_code == 201
        assert res.json()["created"] == 1

        with sf() as s:
            copied = (
                s.query(ActivityCourse)
                .filter(
                    ActivityCourse.school_year == sy,
                    ActivityCourse.semester == sem,
                    ActivityCourse.name == "陶藝創作",
                )
                .one()
            )
            assert copied.min_age_months == 36
            assert copied.max_age_months == 72
            assert copied.meeting_weekday == 2
            assert copied.meeting_start_time == time(15, 30)
            assert copied.meeting_end_time == time(16, 30)


# ────────────────────────────────────────────────────────────────── #
# K2 — 軟刪後同學期同名重建（partial unique index WHERE is_active）
# ────────────────────────────────────────────────────────────────── #


class TestSoftDeletedNameCanBeReused:
    def test_course_recreate_after_soft_delete(self, client_factory):
        """軟刪課程後，同學期同名重建必須成功（不可撞全列 unique → 500）。"""
        client, sf = client_factory
        _setup_admin(sf, client)

        res = client.post("/api/activity/courses", json={"name": "圍棋", "price": 1200})
        assert res.status_code == 201
        course_id = res.json()["id"]

        res = client.delete(f"/api/activity/courses/{course_id}")
        assert res.status_code == 200

        res = client.post("/api/activity/courses", json={"name": "圍棋", "price": 1500})
        assert res.status_code == 201, res.text

        with sf() as s:
            rows = s.query(ActivityCourse).filter(ActivityCourse.name == "圍棋").all()
            assert len(rows) == 2
            assert sorted(r.is_active for r in rows) == [False, True]

    def test_supply_recreate_after_soft_delete(self, client_factory):
        """軟刪用品後，同學期同名重建必須成功。"""
        client, sf = client_factory
        _setup_admin(sf, client)

        res = client.post(
            "/api/activity/supplies", json={"name": "畫具組", "price": 300}
        )
        assert res.status_code == 201
        supply_id = res.json()["id"]

        res = client.delete(f"/api/activity/supplies/{supply_id}")
        assert res.status_code == 200

        res = client.post(
            "/api/activity/supplies", json={"name": "畫具組", "price": 350}
        )
        assert res.status_code == 201, res.text

        with sf() as s:
            rows = s.query(ActivitySupply).filter(ActivitySupply.name == "畫具組").all()
            assert len(rows) == 2

    def test_active_duplicate_still_rejected(self, client_factory):
        """應用層 active 重名檢查保留：仍回 400 友善訊息。"""
        client, sf = client_factory
        _setup_admin(sf, client)

        assert (
            client.post(
                "/api/activity/courses", json={"name": "直排輪", "price": 1000}
            ).status_code
            == 201
        )
        res = client.post(
            "/api/activity/courses", json={"name": "直排輪", "price": 1000}
        )
        assert res.status_code == 400
        assert "已存在" in res.json()["detail"]

    def test_db_level_partial_unique_blocks_active_dup(self, client_factory):
        """DB 層 partial unique：兩筆 is_active=True 同名同學期直插必須被擋。"""
        from sqlalchemy.exc import IntegrityError

        client, sf = client_factory
        sy, sem = resolve_current_academic_term()
        with sf() as s:
            s.add(
                ActivityCourse(name="跆拳道", price=1000, school_year=sy, semester=sem)
            )
            s.commit()
        with sf() as s:
            s.add(
                ActivityCourse(name="跆拳道", price=1000, school_year=sy, semester=sem)
            )
            with pytest.raises(IntegrityError):
                s.commit()
