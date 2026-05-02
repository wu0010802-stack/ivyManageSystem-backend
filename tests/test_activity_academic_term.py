"""tests/test_activity_academic_term.py — 才藝學期制整合測試。

涵蓋：
- 課程 / 用品 / 報名列表 支援 school_year/semester 過濾
- 公開 register 自動匹配 student_id
- /courses/copy-from-previous 複製上學期課程（含跳過已存在）
"""

import os
import sys
from datetime import date, timedelta

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
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySupply,
    Base,
    Classroom,
    Student,
    User,
)
from utils.academic import resolve_current_academic_term
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def term_client(tmp_path):
    db_path = tmp_path / "term.sqlite"
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
        permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
        is_active=True,
    )
    session.add(user)
    session.flush()


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "admin", "password": "TempPass123"}
    )


def _current_term():
    return resolve_current_academic_term()


class TestCourseTermFilter:
    def test_get_courses_filters_by_term(self, term_client):
        client, sf = term_client
        sy, sem = _current_term()
        other_sem = 2 if sem == 1 else 1
        with sf() as s:
            _admin(s)
            s.add(ActivityCourse(name="A", price=100, school_year=sy, semester=sem))
            s.add(
                ActivityCourse(name="B", price=200, school_year=sy, semester=other_sem)
            )
            s.commit()
        assert _login(client).status_code == 200

        # 預設學期只看得到 A
        res = client.get("/api/activity/courses")
        names = [c["name"] for c in res.json()["courses"]]
        assert "A" in names
        assert "B" not in names

        # 切到另一學期看得到 B
        res2 = client.get(
            f"/api/activity/courses?school_year={sy}&semester={other_sem}"
        )
        names2 = [c["name"] for c in res2.json()["courses"]]
        assert names2 == ["B"]

    def test_create_course_uses_current_term(self, term_client):
        client, sf = term_client
        sy, sem = _current_term()
        with sf() as s:
            _admin(s)
            s.commit()
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/courses",
            json={"name": "繪畫", "price": 800, "capacity": 20},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["school_year"] == sy
        assert data["semester"] == sem

    def test_course_name_unique_within_term_only(self, term_client):
        """跨學期可以有同名課程。"""
        client, sf = term_client
        sy, sem = _current_term()
        other_sem = 2 if sem == 1 else 1
        with sf() as s:
            _admin(s)
            s.commit()
        assert _login(client).status_code == 200

        r1 = client.post(
            "/api/activity/courses",
            json={"name": "圍棋", "price": 1200, "school_year": sy, "semester": sem},
        )
        assert r1.status_code == 201

        # 同學期同名 → 400
        r2 = client.post(
            "/api/activity/courses",
            json={"name": "圍棋", "price": 1300, "school_year": sy, "semester": sem},
        )
        assert r2.status_code == 400

        # 不同學期同名 → 201
        r3 = client.post(
            "/api/activity/courses",
            json={
                "name": "圍棋",
                "price": 1200,
                "school_year": sy,
                "semester": other_sem,
            },
        )
        assert r3.status_code == 201


class TestCopyFromPrevious:
    def test_copy_to_new_term(self, term_client):
        client, sf = term_client
        sy, sem = _current_term()
        other_sem = 2 if sem == 1 else 1
        with sf() as s:
            _admin(s)
            s.add(ActivityCourse(name="圍棋", price=1200, school_year=sy, semester=sem))
            s.add(ActivityCourse(name="美術", price=800, school_year=sy, semester=sem))
            s.commit()
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": sy,
                "source_semester": sem,
                "target_school_year": sy,
                "target_semester": other_sem,
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["created"] == 2
        assert data["skipped"] == 0

        # 確認目標學期有 2 筆
        with sf() as s:
            count = (
                s.query(ActivityCourse)
                .filter(
                    ActivityCourse.school_year == sy,
                    ActivityCourse.semester == other_sem,
                )
                .count()
            )
            assert count == 2

    def test_copy_skips_existing_names(self, term_client):
        client, sf = term_client
        sy, sem = _current_term()
        other_sem = 2 if sem == 1 else 1
        with sf() as s:
            _admin(s)
            s.add(ActivityCourse(name="圍棋", price=1200, school_year=sy, semester=sem))
            s.add(ActivityCourse(name="美術", price=800, school_year=sy, semester=sem))
            # 目標學期已有同名「圍棋」
            s.add(
                ActivityCourse(
                    name="圍棋", price=9999, school_year=sy, semester=other_sem
                )
            )
            s.commit()
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": sy,
                "source_semester": sem,
                "target_school_year": sy,
                "target_semester": other_sem,
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["created"] == 1
        assert data["skipped"] == 1

    def test_same_term_rejected(self, term_client):
        client, sf = term_client
        sy, sem = _current_term()
        with sf() as s:
            _admin(s)
            s.commit()
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/courses/copy-from-previous",
            json={
                "source_school_year": sy,
                "source_semester": sem,
                "target_school_year": sy,
                "target_semester": sem,
            },
        )
        assert res.status_code == 400


class TestPublicRegisterAutoLinksStudent:
    def _setup_base(self, sf, with_student=True):
        sy, sem = _current_term()
        with sf() as s:
            _admin(s)
            s.add(
                Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            if with_student:
                # classroom id 由 autoincrement 給
                classroom = s.query(Classroom).filter_by(name="海豚班").first()
                s.add(
                    Student(
                        student_id="S001",
                        name="王小明",
                        birthday=date(2020, 1, 1),
                        classroom_id=classroom.id if classroom else None,
                        parent_phone="0912345678",
                        is_active=True,
                    )
                )
            s.commit()

    def test_auto_links_when_match(self, term_client):
        client, sf = term_client
        self._setup_base(sf, with_student=True)

        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [],
            },
        )
        assert res.status_code == 201

        with sf() as s:
            reg = s.query(ActivityRegistration).one()
            assert reg.student_id is not None
            assert reg.classroom_id is not None
            assert reg.match_status == "matched"
            assert reg.pending_review is False
            assert reg.school_year == _current_term()[0]
            assert reg.semester == _current_term()[1]

    def test_no_link_when_no_student(self, term_client):
        client, sf = term_client
        self._setup_base(sf, with_student=False)

        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [],
            },
        )
        assert res.status_code == 201

        with sf() as s:
            reg = s.query(ActivityRegistration).one()
            assert reg.student_id is None
            assert reg.pending_review is True
            assert reg.match_status == "pending"

    def test_no_link_when_duplicate_students(self, term_client):
        """同名+生日+相同 phone 命中多位學生時不關聯（避免錯誤連結）。"""
        client, sf = term_client
        sy, sem = _current_term()
        with sf() as s:
            _admin(s)
            s.add(
                Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.add(
                Student(
                    student_id="A",
                    name="同名",
                    birthday=date(2020, 1, 1),
                    parent_phone="0912345678",
                    is_active=True,
                )
            )
            s.add(
                Student(
                    student_id="B",
                    name="同名",
                    birthday=date(2020, 1, 1),
                    parent_phone="0912345678",
                    is_active=True,
                )
            )
            s.commit()

        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "同名",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [],
            },
        )
        assert res.status_code == 201
        with sf() as s:
            reg = s.query(ActivityRegistration).one()
            assert reg.student_id is None
            assert reg.pending_review is True

    def test_admin_create_registration_bypasses_open_time(self, term_client):
        """後台手動新增報名不受報名開放時間限制。"""
        client, sf = term_client
        sy, sem = _current_term()
        with sf() as s:
            _admin(s)
            s.add(
                Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            # 明確關閉公開報名
            from models.database import ActivityRegistrationSettings

            s.add(ActivityRegistrationSettings(is_open=False))
            s.commit()
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "email": "parent@example.com",
                "remark": "後台手動建立",
                "courses": [{"name": "圍棋", "price": ""}],
                "supplies": [],
            },
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["id"]
        assert data["waitlisted"] is False

        with sf() as s:
            reg = s.query(ActivityRegistration).one()
            assert reg.student_name == "王小明"
            assert reg.class_name == "海豚班"
            assert reg.email == "parent@example.com"
            assert reg.remark == "後台手動建立"
            assert reg.school_year == sy
            assert reg.semester == sem

    def test_admin_create_duplicate_same_term_rejected(self, term_client):
        """同學期同學生重複建立應回 400。"""
        client, sf = term_client
        sy, sem = _current_term()
        with sf() as s:
            _admin(s)
            s.add(
                Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.commit()
        assert _login(client).status_code == 200

        payload = {
            "name": "王小明",
            "birthday": "2020-01-01",
            "class": "海豚班",
            "courses": [{"name": "圍棋", "price": ""}],
            "supplies": [],
        }
        r1 = client.post("/api/activity/registrations", json=payload)
        assert r1.status_code == 201, r1.text

        r2 = client.post("/api/activity/registrations", json=payload)
        assert r2.status_code == 400

    def test_admin_create_requires_write_permission(self, term_client):
        """只有 ACTIVITY_READ 權限者不能呼叫新增。"""
        client, sf = term_client
        sy, sem = _current_term()
        with sf() as s:
            user = User(
                username="viewer",
                password_hash=hash_password("TempPass123"),
                role="staff",
                permissions=Permission.ACTIVITY_READ,
                is_active=True,
            )
            s.add(user)
            s.add(
                Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
            )
            s.commit()

        login = client.post(
            "/api/auth/login",
            json={"username": "viewer", "password": "TempPass123"},
        )
        assert login.status_code == 200

        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "courses": [],
                "supplies": [],
            },
        )
        assert res.status_code == 403

    def test_same_student_can_register_in_different_terms(self, term_client):
        """同學生跨學期可重複報名。"""
        client, sf = term_client
        sy, sem = _current_term()
        other_sem = 2 if sem == 1 else 1
        with sf() as s:
            _admin(s)
            classroom = Classroom(
                name="海豚班", is_active=True, school_year=sy, semester=sem
            )
            s.add(classroom)
            s.flush()
            # F-030：未驗證身分（unmatched）的重複送件會走 silent-success（201、不寫 DB）。
            # 這個測試是要驗「同學期 dedup 觸發明確 400」，所以要 seed 對應 Student 讓
            # parent_phone 比對到，走「matched 家長」分流。
            s.add(
                Student(
                    student_id="S001",
                    name="王小明",
                    birthday=date(2020, 1, 1),
                    classroom_id=classroom.id,
                    parent_phone="0912345678",
                    is_active=True,
                )
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=other_sem,
                    is_active=True,
                )
            )
            # 開啟報名
            from models.database import ActivityRegistrationSettings

            s.add(ActivityRegistrationSettings(is_open=True))
            s.commit()

        payload = {
            "name": "王小明",
            "birthday": "2020-01-01",
            "parent_phone": "0912345678",
            "class": "海豚班",
            "courses": [{"name": "圍棋", "price": "1"}],
            "supplies": [],
        }
        # 當前學期報名
        r1 = client.post("/api/activity/public/register", json=payload)
        assert r1.status_code == 201

        # 同學期再報 → 400（matched 家長走明確 dedup 訊息；同名+生日同學期已存在）
        r2 = client.post("/api/activity/public/register", json=payload)
        assert r2.status_code == 400

        # 另一學期 → 201（新增第二筆不同學期）
        payload2 = {
            **payload,
            "parent_phone": "0912999999",
            "school_year": sy,
            "semester": other_sem,
        }
        r3 = client.post("/api/activity/public/register", json=payload2)
        assert r3.status_code == 201

        with sf() as s:
            assert s.query(ActivityRegistration).count() == 2


class TestAdminEditRegistration:
    """後台編輯既有報名（基本欄位 / 新增課程 / 用品增刪）。"""

    def _setup(self, sf, *, extra_course=False):
        sy, sem = _current_term()
        with sf() as s:
            _admin(s)
            s.add(
                Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
            )
            s.add(
                Classroom(name="鯨魚班", is_active=True, school_year=sy, semester=sem)
            )
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1200,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                    capacity=30,
                )
            )
            if extra_course:
                s.add(
                    ActivityCourse(
                        name="繪畫",
                        price=800,
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                        capacity=1,
                    )
                )
            s.add(
                ActivitySupply(
                    name="美術包",
                    price=500,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.commit()

    def _create_reg(self, client) -> int:
        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": ""}],
                "supplies": [],
            },
        )
        assert res.status_code == 201, res.text
        return res.json()["id"]

    def test_update_basic_fields(self, term_client):
        client, sf = term_client
        self._setup(sf)
        assert _login(client).status_code == 200
        reg_id = self._create_reg(client)

        res = client.put(
            f"/api/activity/registrations/{reg_id}",
            json={
                "name": "王大明",
                "birthday": "2020-01-02",
                "class": "鯨魚班",
                "email": "dad@example.com",
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["changed"] == 4

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.student_name == "王大明"
            assert reg.birthday == "2020-01-02"
            assert reg.class_name == "鯨魚班"
            assert reg.email == "dad@example.com"

    def test_update_basic_rejects_duplicate_same_term(self, term_client):
        client, sf = term_client
        self._setup(sf)
        assert _login(client).status_code == 200
        first = self._create_reg(client)

        # 另建一筆同學期、不同人
        res = client.post(
            "/api/activity/registrations",
            json={
                "name": "李小美",
                "birthday": "2020-05-05",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": ""}],
                "supplies": [],
            },
        )
        assert res.status_code == 201
        second = res.json()["id"]

        # 把 second 改成跟 first 同姓名+生日 → 400
        res2 = client.put(
            f"/api/activity/registrations/{second}",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "email": None,
            },
        )
        assert res2.status_code == 400
        assert first != second  # sanity

    def test_add_course_enrolled_and_waitlist(self, term_client):
        client, sf = term_client
        self._setup(sf, extra_course=True)
        sy, sem = _current_term()
        assert _login(client).status_code == 200
        reg_id = self._create_reg(client)

        with sf() as s:
            drawing = (
                s.query(ActivityCourse)
                .filter_by(name="繪畫", school_year=sy, semester=sem)
                .one()
            )
            drawing_id = drawing.id

        # 正常加入
        r1 = client.post(
            f"/api/activity/registrations/{reg_id}/courses",
            json={"course_id": drawing_id},
        )
        assert r1.status_code == 201, r1.text
        assert r1.json()["status"] == "enrolled"

        # 再加一次 → 400
        r2 = client.post(
            f"/api/activity/registrations/{reg_id}/courses",
            json={"course_id": drawing_id},
        )
        assert r2.status_code == 400

        # 建立第二筆報名，再加「繪畫」（capacity=1 已滿，會變候補）
        r3 = client.post(
            "/api/activity/registrations",
            json={
                "name": "李小美",
                "birthday": "2020-05-05",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": ""}],
                "supplies": [],
            },
        )
        assert r3.status_code == 201
        second_reg = r3.json()["id"]

        r4 = client.post(
            f"/api/activity/registrations/{second_reg}/courses",
            json={"course_id": drawing_id},
        )
        assert r4.status_code == 201
        assert r4.json()["status"] == "waitlist"

    def test_add_and_remove_supply(self, term_client):
        client, sf = term_client
        self._setup(sf)
        sy, sem = _current_term()
        assert _login(client).status_code == 200
        reg_id = self._create_reg(client)

        with sf() as s:
            supply_id = (
                s.query(ActivitySupply)
                .filter_by(name="美術包", school_year=sy, semester=sem)
                .one()
                .id
            )

        r1 = client.post(
            f"/api/activity/registrations/{reg_id}/supplies",
            json={"supply_id": supply_id},
        )
        assert r1.status_code == 201, r1.text
        rs_id = r1.json()["id"]
        assert r1.json()["total_amount"] == 1200 + 500

        r2 = client.delete(f"/api/activity/registrations/{reg_id}/supplies/{rs_id}")
        assert r2.status_code == 200
        assert r2.json()["total_amount"] == 1200
