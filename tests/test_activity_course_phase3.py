"""Phase 3 — 才藝課程適齡 + 結構化時段欄位（前台不適齡/衝堂警告檢核基礎建設）

覆蓋：
1. /public/courses 回傳新欄位（time 序列化為 "HH:MM"）
2. 既有課程不帶新欄位仍 work（向後相容；新欄位為 null）
3. admin POST /courses 接受新欄位並寫入
4. admin PUT /courses 更新新欄位
5. admin GET /courses 回新欄位
6. CourseCreate validator: min_age_months > max_age_months → 422
7. CourseCreate validator: meeting_start_time >= meeting_end_time → 422
8. CourseCreate validator: meeting_weekday 7 → 422

策略：警告但不阻擋；後端只負責「正確儲存與曝露欄位」，前端做 advisory UI。
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
from api.activity.public import _public_register_limiter_instance
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import ActivityCourse, Base, User
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_factory(tmp_path):
    db_path = tmp_path / "phase3.sqlite"
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


def _admin(session):
    user = User(
        username="admin",
        password_hash=hash_password("TempPass123"),
        role="admin",
        permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
        is_active=True,
    )
    session.add(user)
    session.flush()


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "admin", "password": "TempPass123"}
    )


class TestPublicCoursesNewFields:
    def test_public_courses_returns_age_and_schedule(self, client_factory):
        """/public/courses 必須回傳 Phase 3 新欄位給前端 advisory 使用。"""
        from datetime import time

        client, sf = client_factory
        sy, sem = resolve_current_academic_term()
        with sf() as s:
            s.add(
                ActivityCourse(
                    name="陶藝創作",
                    price=2400,
                    sessions=12,
                    capacity=8,
                    school_year=sy,
                    semester=sem,
                    min_age_months=36,
                    max_age_months=72,
                    meeting_weekday=2,  # Wed
                    meeting_start_time=time(15, 30),
                    meeting_end_time=time(16, 30),
                )
            )
            s.commit()

        res = client.get("/api/activity/public/courses")
        assert res.status_code == 200
        body = res.json()
        assert isinstance(body, list)
        c = body[0]
        assert c["name"] == "陶藝創作"
        assert c["min_age_months"] == 36
        assert c["max_age_months"] == 72
        assert c["meeting_weekday"] == 2
        # time 必須序列化為 "HH:MM"（前端 parse 友善；不要回 ISO datetime）
        assert c["meeting_start_time"] == "15:30"
        assert c["meeting_end_time"] == "16:30"

    def test_public_courses_backward_compat_when_fields_null(self, client_factory):
        """既有課程沒填新欄位時，端點仍 200 且新欄位為 null。"""
        client, sf = client_factory
        sy, sem = resolve_current_academic_term()
        with sf() as s:
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    sessions=10,
                    capacity=20,
                    school_year=sy,
                    semester=sem,
                    # 不設新欄位
                )
            )
            s.commit()

        res = client.get("/api/activity/public/courses")
        assert res.status_code == 200
        c = res.json()[0]
        assert c["min_age_months"] is None
        assert c["max_age_months"] is None
        assert c["meeting_weekday"] is None
        assert c["meeting_start_time"] is None
        assert c["meeting_end_time"] is None
        # 既有欄位不變
        assert c["name"] == "圍棋"
        assert c["price"] == 1200


class TestAdminCoursesNewFields:
    def _setup_admin(self, sf, client):
        with sf() as s:
            _admin(s)
            s.commit()
        assert _login(client).status_code == 200

    def test_admin_create_with_new_fields(self, client_factory):
        client, sf = client_factory
        self._setup_admin(sf, client)

        res = client.post(
            "/api/activity/courses",
            json={
                "name": "音樂律動",
                "price": 1500,
                "sessions": 8,
                "capacity": 15,
                "min_age_months": 24,
                "max_age_months": 60,
                "meeting_weekday": 1,
                "meeting_start_time": "14:00",
                "meeting_end_time": "15:00",
            },
        )
        assert res.status_code == 201

        # 透過 GET 驗證寫入
        list_res = client.get("/api/activity/courses")
        assert list_res.status_code == 200
        c = list_res.json()["courses"][0]
        assert c["min_age_months"] == 24
        assert c["max_age_months"] == 60
        assert c["meeting_weekday"] == 1
        assert c["meeting_start_time"] == "14:00"
        assert c["meeting_end_time"] == "15:00"

    def test_admin_update_with_new_fields(self, client_factory):
        client, sf = client_factory
        self._setup_admin(sf, client)

        # 先建立一筆無 schedule 的課程
        create = client.post(
            "/api/activity/courses",
            json={"name": "舞蹈", "price": 1000, "sessions": 6, "capacity": 12},
        )
        assert create.status_code == 201
        course_id = create.json()["id"]

        # PUT 補上 schedule
        upd = client.put(
            f"/api/activity/courses/{course_id}",
            json={
                "meeting_weekday": 4,
                "meeting_start_time": "16:00",
                "meeting_end_time": "17:00",
                "max_age_months": 84,
            },
        )
        assert upd.status_code == 200

        list_res = client.get("/api/activity/courses")
        c = next(c for c in list_res.json()["courses"] if c["id"] == course_id)
        assert c["meeting_weekday"] == 4
        assert c["meeting_start_time"] == "16:00"
        assert c["meeting_end_time"] == "17:00"
        assert c["max_age_months"] == 84

    def test_min_age_greater_than_max_rejected(self, client_factory):
        client, sf = client_factory
        self._setup_admin(sf, client)

        res = client.post(
            "/api/activity/courses",
            json={
                "name": "繪畫",
                "price": 800,
                "min_age_months": 80,
                "max_age_months": 60,  # < min → 422
            },
        )
        assert res.status_code == 422

    def test_meeting_start_after_end_rejected(self, client_factory):
        client, sf = client_factory
        self._setup_admin(sf, client)

        res = client.post(
            "/api/activity/courses",
            json={
                "name": "樂高",
                "price": 1100,
                "meeting_start_time": "16:00",
                "meeting_end_time": "15:00",  # 早於 start → 422
            },
        )
        assert res.status_code == 422

    def test_meeting_weekday_out_of_range_rejected(self, client_factory):
        client, sf = client_factory
        self._setup_admin(sf, client)

        res = client.post(
            "/api/activity/courses",
            json={
                "name": "美術",
                "price": 900,
                "meeting_weekday": 7,  # 週日 = 6；7 不合法
            },
        )
        assert res.status_code == 422

    def test_meeting_start_equal_end_rejected(self, client_factory):
        """start == end 視為 0 分鐘上課，不合理；ge 不允許。"""
        client, sf = client_factory
        self._setup_admin(sf, client)

        res = client.post(
            "/api/activity/courses",
            json={
                "name": "speed-class",
                "price": 100,
                "meeting_start_time": "14:00",
                "meeting_end_time": "14:00",
            },
        )
        assert res.status_code == 422


class TestPartialUpdateRangeValidation:
    """Finding 6：部分更新需合併 DB 現值後驗證，避免寫出矛盾的年齡／時間範圍。

    schema validator 只在成對欄位同時出現於 payload 時才比較，但 endpoint 把 patch
    直接覆寫既有資料 → 單獨更新一邊可造成 min_age>max_age 或 start>=end。
    """

    def _setup_admin(self, sf, client):
        with sf() as s:
            _admin(s)
            s.commit()
        assert _login(client).status_code == 200

    def _create_course(self, client, **fields):
        body = {"name": "課程", "price": 1000, "sessions": 8, "capacity": 12}
        body.update(fields)
        res = client.post("/api/activity/courses", json=body)
        assert res.status_code == 201, res.text
        return res.json()["id"]

    def test_update_min_age_above_existing_max_rejected(self, client_factory):
        """既有 max_age=36，單獨 PUT min_age=60（>既有 max）→ 應被拒（400）。"""
        client, sf = client_factory
        self._setup_admin(sf, client)
        cid = self._create_course(client, min_age_months=24, max_age_months=36)

        res = client.put(f"/api/activity/courses/{cid}", json={"min_age_months": 60})
        assert res.status_code == 400, res.text
        # 確認未寫入矛盾值
        c = next(
            x
            for x in client.get("/api/activity/courses").json()["courses"]
            if x["id"] == cid
        )
        assert c["min_age_months"] == 24

    def test_update_start_after_existing_end_rejected(self, client_factory):
        """既有 end=15:00，單獨 PUT start=16:00（晚於既有 end）→ 應被拒（400）。"""
        client, sf = client_factory
        self._setup_admin(sf, client)
        cid = self._create_course(
            client, meeting_start_time="14:00", meeting_end_time="15:00"
        )

        res = client.put(
            f"/api/activity/courses/{cid}", json={"meeting_start_time": "16:00"}
        )
        assert res.status_code == 400, res.text

    def test_valid_partial_update_still_allowed(self, client_factory):
        """合法的部分更新（min_age=30 ≤ 既有 max=36）→ 200。"""
        client, sf = client_factory
        self._setup_admin(sf, client)
        cid = self._create_course(client, min_age_months=24, max_age_months=36)

        res = client.put(f"/api/activity/courses/{cid}", json={"min_age_months": 30})
        assert res.status_code == 200, res.text
        c = next(
            x
            for x in client.get("/api/activity/courses").json()["courses"]
            if x["id"] == cid
        )
        assert c["min_age_months"] == 30


# ============================================================
# [C48/C49] video_url scheme 驗證（防儲存型 XSS）
# 純 schema 單元測試，不經 DB。
# ============================================================


class TestCourseVideoUrlScheme:
    """CourseCreate/CourseUpdate.video_url 須限制 scheme ∈ {http, https}。

    前端 :href 直出 video_url，若允許 javascript: 等 scheme，API 直寫
    可繞過前端造成儲存型 XSS。
    """

    @pytest.fixture(autouse=True)
    def _import_schema(self):
        from schemas.activity_admin import CourseCreate, CourseUpdate

        self.Create = CourseCreate
        self.Update = CourseUpdate

    def test_create_javascript_scheme_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self.Create(name="繪畫", price=800, video_url="javascript:alert(1)")

    def test_update_javascript_scheme_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self.Update(video_url="javascript:alert(1)")

    def test_create_data_scheme_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self.Create(
                name="繪畫",
                price=800,
                video_url="data:text/html;base64,PHNjcmlwdD4=",
            )

    def test_create_https_youtu_allowed(self):
        obj = self.Create(name="繪畫", price=800, video_url="https://youtu.be/x")
        assert obj.video_url == "https://youtu.be/x"

    def test_create_http_allowed(self):
        obj = self.Create(name="繪畫", price=800, video_url="http://example.com/v")
        assert obj.video_url == "http://example.com/v"

    def test_create_empty_and_none_allowed(self):
        assert self.Create(name="繪畫", price=800, video_url=None).video_url is None
        assert self.Create(name="繪畫", price=800, video_url="").video_url == ""

    def test_update_none_allowed(self):
        assert self.Update(video_url=None).video_url is None
