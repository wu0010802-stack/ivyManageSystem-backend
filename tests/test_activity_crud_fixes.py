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


# ────────────────────────────────────────────────────────────────── #
# K3 — delete_session 硬刪須留稽核 log
# ────────────────────────────────────────────────────────────────── #


class TestDeleteSessionAudit:
    def test_delete_session_writes_explicit_audit(self, client_factory, monkeypatch):
        """硬刪場次 CASCADE 抹點名紀錄，必須顯式落稽核（誰/哪課/哪日/幾筆）。"""
        from datetime import date
        from unittest.mock import MagicMock

        from models.database import (
            ActivityAttendance,
            ActivityRegistration,
            ActivitySession,
        )

        client, sf = client_factory
        _setup_admin(sf, client)

        sy, sem = resolve_current_academic_term()
        with sf() as s:
            course = ActivityCourse(
                name="直排輪", price=1000, school_year=sy, semester=sem
            )
            s.add(course)
            s.flush()
            sess = ActivitySession(course_id=course.id, session_date=date(2026, 6, 10))
            s.add(sess)
            s.flush()
            for i in range(2):
                reg = ActivityRegistration(
                    student_name=f"學生{i}",
                    birthday="2020-01-01",
                    is_active=True,
                    school_year=sy,
                    semester=sem,
                )
                s.add(reg)
                s.flush()
                s.add(
                    ActivityAttendance(
                        session_id=sess.id,
                        registration_id=reg.id,
                        is_present=True,
                    )
                )
            s.commit()
            session_id = sess.id

        mock_audit = MagicMock()
        import api.activity.attendance as attendance_module

        monkeypatch.setattr(attendance_module, "write_explicit_audit", mock_audit)

        res = client.delete(f"/api/activity/attendance/sessions/{session_id}")
        assert res.status_code == 200

        assert mock_audit.call_count == 1
        kwargs = mock_audit.call_args.kwargs
        assert kwargs["action"] == "DELETE"
        assert kwargs["entity_type"] == "activity_session"
        assert kwargs["entity_id"] == str(session_id)
        assert "直排輪" in kwargs["summary"]
        assert "2026-06-10" in kwargs["summary"]
        assert "2" in kwargs["summary"]  # 抹掉 2 筆點名紀錄
        assert kwargs["changes"]["removed_attendance"] == 2
        assert kwargs["changes"]["operator"] == "admin"

    def test_delete_session_not_found_no_audit(self, client_factory, monkeypatch):
        """404 路徑不落稽核。"""
        from unittest.mock import MagicMock

        client, sf = client_factory
        _setup_admin(sf, client)

        mock_audit = MagicMock()
        import api.activity.attendance as attendance_module

        monkeypatch.setattr(attendance_module, "write_explicit_audit", mock_audit)
        res = client.delete("/api/activity/attendance/sessions/99999")
        assert res.status_code == 404
        assert mock_audit.call_count == 0


# ────────────────────────────────────────────────────────────────── #
# K4 — list_sessions skip/limit 驗證
# ────────────────────────────────────────────────────────────────── #


class TestListSessionsPaginationValidation:
    def test_negative_skip_rejected(self, client_factory):
        """skip=-1 在 PG OFFSET 會炸 500，必須 422 擋下。"""
        client, sf = client_factory
        _setup_admin(sf, client)
        res = client.get("/api/activity/attendance/sessions", params={"skip": -1})
        assert res.status_code == 422

    def test_oversized_limit_rejected(self, client_factory):
        """limit=999999 全表 dump，必須 422 擋下（上限 500）。"""
        client, sf = client_factory
        _setup_admin(sf, client)
        res = client.get("/api/activity/attendance/sessions", params={"limit": 999999})
        assert res.status_code == 422

    def test_zero_limit_rejected(self, client_factory):
        client, sf = client_factory
        _setup_admin(sf, client)
        res = client.get("/api/activity/attendance/sessions", params={"limit": 0})
        assert res.status_code == 422

    def test_valid_pagination_ok(self, client_factory):
        client, sf = client_factory
        _setup_admin(sf, client)
        res = client.get(
            "/api/activity/attendance/sessions", params={"skip": 0, "limit": 500}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["skip"] == 0
        assert body["limit"] == 500


# ────────────────────────────────────────────────────────────────── #
# K5 — supplies 4 端點補 response_model（契約如實描述既有輸出）
# ────────────────────────────────────────────────────────────────── #


class TestSuppliesResponseModel:
    def test_all_supply_endpoints_declare_response_model(self):
        """4 個 supplies 端點都必須掛 response_model（OpenAPI 契約防 unknown）。"""
        from api.activity.supplies import router

        routes = {
            (next(iter(r.methods)), r.path): r
            for r in router.routes
            if hasattr(r, "methods")
        }
        for key in [
            ("GET", "/supplies"),
            ("POST", "/supplies"),
            ("PUT", "/supplies/{supply_id}"),
            ("DELETE", "/supplies/{supply_id}"),
        ]:
            assert key in routes, key
            assert routes[key].response_model is not None, key

    def test_supply_crud_response_shape_unchanged(self, client_factory):
        """掛 response_model 後既有 response shape 不可改變。"""
        client, sf = client_factory
        _setup_admin(sf, client)
        sy, sem = resolve_current_academic_term()

        res = client.post("/api/activity/supplies", json={"name": "圍裙", "price": 250})
        assert res.status_code == 201
        body = res.json()
        assert set(body) == {"message", "id", "school_year", "semester"}
        assert body["school_year"] == sy
        assert body["semester"] == sem
        supply_id = body["id"]

        res = client.get("/api/activity/supplies")
        assert res.status_code == 200
        body = res.json()
        assert set(body) == {
            "supplies",
            "total",
            "skip",
            "limit",
            "school_year",
            "semester",
        }
        assert body["total"] == 1
        item = body["supplies"][0]
        assert set(item) == {"id", "name", "price", "school_year", "semester"}
        assert item["name"] == "圍裙"
        assert item["price"] == 250

        res = client.put(f"/api/activity/supplies/{supply_id}", json={"price": 300})
        assert res.status_code == 200
        assert res.json() == {"message": "用品更新成功"}

        res = client.delete(f"/api/activity/supplies/{supply_id}")
        assert res.status_code == 200
        assert res.json() == {"message": "用品已停用"}


# ────────────────────────────────────────────────────────────────── #
# K6 — inquiry phone 寬鬆格式驗證（允許市話；非 _validate_tw_mobile）
# ────────────────────────────────────────────────────────────────── #


def _post_inquiry(client, phone):
    return client.post(
        "/api/activity/public/inquiries",
        json={"name": "王媽媽", "phone": phone, "question": "請問課程何時開始？"},
    )


class TestInquiryPhoneValidation:
    def test_mobile_accepted(self, client_factory):
        client, sf = client_factory
        assert _post_inquiry(client, "0912345678").status_code == 201

    def test_landline_with_dashes_accepted(self, client_factory):
        """市話格式（含區碼、-、括號、空白、+886）都要放行。"""
        client, sf = client_factory
        assert _post_inquiry(client, "(02) 2345-6789").status_code == 201

    def test_international_prefix_accepted(self, client_factory):
        client, sf = client_factory
        assert _post_inquiry(client, "+886 2 2345 6789").status_code == 201

    def test_alpha_rejected(self, client_factory):
        client, sf = client_factory
        res = _post_inquiry(client, "abc12345678")
        assert res.status_code == 422

    def test_too_few_digits_rejected(self, client_factory):
        """數字字元少於 7 → 422。"""
        client, sf = client_factory
        res = _post_inquiry(client, "02-123")
        assert res.status_code == 422

    def test_error_message_in_chinese(self, client_factory):
        client, sf = client_factory
        res = _post_inquiry(client, "電話：0912")
        assert res.status_code == 422
        assert "電話" in str(res.json())


# ────────────────────────────────────────────────────────────────── #
# K7 — inquiry list 回傳全量 unread_count（前端 badge 跨頁/跨篩選正確）
# ────────────────────────────────────────────────────────────────── #


class TestInquiriesUnreadCount:
    def _seed(self, sf, unread=2, read=1):
        from models.database import ParentInquiry

        with sf() as s:
            for i in range(unread):
                s.add(
                    ParentInquiry(
                        name=f"家長{i}", phone="0912345678", question="未讀提問"
                    )
                )
            for i in range(read):
                s.add(
                    ParentInquiry(
                        name=f"已讀家長{i}",
                        phone="0912345678",
                        question="已讀提問",
                        is_read=True,
                    )
                )
            s.commit()

    def test_unread_count_is_global_regardless_of_filter(self, client_factory):
        """is_read=true 篩選下 unread_count 仍須回全量未讀數。"""
        client, sf = client_factory
        _setup_admin(sf, client)
        self._seed(sf, unread=2, read=1)

        res = client.get("/api/activity/inquiries", params={"is_read": True})
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        assert body["unread_count"] == 2

    def test_unread_count_ignores_pagination(self, client_factory):
        client, sf = client_factory
        _setup_admin(sf, client)
        self._seed(sf, unread=3, read=0)

        res = client.get("/api/activity/inquiries", params={"limit": 1})
        assert res.status_code == 200
        body = res.json()
        assert len(body["items"]) == 1
        assert body["unread_count"] == 3

    def test_unread_count_zero_when_all_read(self, client_factory):
        client, sf = client_factory
        _setup_admin(sf, client)
        self._seed(sf, unread=0, read=2)

        res = client.get("/api/activity/inquiries")
        assert res.status_code == 200
        assert res.json()["unread_count"] == 0
