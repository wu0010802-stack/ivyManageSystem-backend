"""課後才藝整合回歸測試。"""

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
from api.activity.public import _public_register_limiter_instance
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    Base,
    ActivityCourse,
    ActivityRegistration,
    ActivitySupply,
    Classroom,
    Employee,
    ParentInquiry,
    RegistrationCourse,
    RegistrationSupply,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def activity_client(tmp_path):
    """建立隔離 sqlite 測試 app。"""
    db_path = tmp_path / "activity-regressions.sqlite"
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


def _create_admin(
    session, username: str = "activity_admin", password: str = "TempPass123"
) -> User:
    admin = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
        is_active=True,
    )
    session.add(admin)
    session.flush()
    return admin


def _create_employee(session, employee_id: str, name: str) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        is_active=True,
    )
    session.add(employee)
    session.flush()
    return employee


def _login(
    client: TestClient, username: str = "activity_admin", password: str = "TempPass123"
):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _create_classroom(session, name: str, is_active: bool = True) -> Classroom:
    classroom = Classroom(name=name, is_active=is_active)
    session.add(classroom)
    session.flush()
    return classroom


def _current_term():
    """測試用：回傳當前學期 (school_year, semester)。"""
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _create_course(
    session, name: str, price: int, capacity: int = 30, allow_waitlist: bool = True
) -> ActivityCourse:
    sy, sem = _current_term()
    course = ActivityCourse(
        name=name,
        price=price,
        capacity=capacity,
        allow_waitlist=allow_waitlist,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()
    return course


def _create_supply(session, name: str, price: int) -> ActivitySupply:
    sy, sem = _current_term()
    supply = ActivitySupply(
        name=name, price=price, is_active=True, school_year=sy, semester=sem
    )
    session.add(supply)
    session.flush()
    return supply


def _create_registration(
    session,
    *,
    student_name: str,
    class_name: str,
    is_active: bool = True,
    is_paid: bool = False,
    parent_phone: str = "0912345678",
) -> ActivityRegistration:
    sy, sem = _current_term()
    registration = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name=class_name,
        is_active=is_active,
        is_paid=is_paid,
        school_year=sy,
        semester=sem,
        parent_phone=parent_phone,
    )
    session.add(registration)
    session.flush()
    return registration


class TestPublicRegisterValidation:
    def test_public_register_unknown_class_goes_to_pending_review(
        self, activity_client
    ):
        """班級不存在不再擋 400（避免洩漏系統狀態），而是進入待審核佇列。"""
        client, session_factory = activity_client

        with session_factory() as session:
            _create_classroom(session, "向日葵班", is_active=False)
            _create_course(session, "圍棋", 1200)
            session.commit()

        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
                "class": "不存在班級",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [],
            },
        )

        assert res.status_code == 201
        with session_factory() as session:
            reg = session.query(ActivityRegistration).one()
            assert reg.pending_review is True
            assert reg.match_status == "pending"
            assert reg.student_id is None

    def test_public_register_uses_db_prices_for_snapshots(self, activity_client):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_classroom(session, "海豚班")
            _create_course(session, "圍棋", 1200)
            _create_supply(session, "教材包", 350)
            session.commit()

        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "王小明",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [{"name": "教材包", "price": "2"}],
            },
        )

        assert res.status_code == 201

        with session_factory() as session:
            registration = session.query(ActivityRegistration).one()
            course_row = (
                session.query(RegistrationCourse)
                .filter(RegistrationCourse.registration_id == registration.id)
                .one()
            )
            supply_row = (
                session.query(RegistrationSupply)
                .filter(RegistrationSupply.registration_id == registration.id)
                .one()
            )

            assert course_row.price_snapshot == 1200
            assert supply_row.price_snapshot == 350


class TestActivityCacheInvalidation:
    def test_mark_inquiry_read_invalidates_stats_summary_cache(self, activity_client):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            inquiry = ParentInquiry(
                name="王家長",
                phone="0912345678",
                question="請問上課時間？",
                is_read=False,
            )
            session.add(inquiry)
            session.commit()
            inquiry_id = inquiry.id

        login_res = _login(client)
        assert login_res.status_code == 200

        first_summary = client.get("/api/activity/stats-summary")
        assert first_summary.status_code == 200
        assert first_summary.json()["unreadInquiries"] == 1

        mark_read = client.put(f"/api/activity/inquiries/{inquiry_id}/read")
        assert mark_read.status_code == 200

        second_summary = client.get("/api/activity/stats-summary")
        assert second_summary.status_code == 200
        assert second_summary.json()["unreadInquiries"] == 0


class TestRegistrationListAggregation:
    def test_admin_registration_list_preserves_counts_and_course_names(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            _create_classroom(session, "海豚班")
            course_a = _create_course(session, "圍棋", 1200)
            course_b = _create_course(session, "珠心算", 1500)
            supply = _create_supply(session, "教材包", 300)
            reg = _create_registration(
                session,
                student_name="王小明",
                class_name="海豚班",
                is_paid=True,
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course_a.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course_b.id,
                    status="waitlist",
                    price_snapshot=1500,
                )
            )
            session.add(
                RegistrationSupply(
                    registration_id=reg.id, supply_id=supply.id, price_snapshot=300
                )
            )
            session.commit()

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.get("/api/activity/registrations")

        assert res.status_code == 200
        payload = res.json()
        assert payload["total"] == 1
        assert payload["items"][0]["course_count"] == 2
        assert payload["items"][0]["supply_count"] == 1
        assert payload["items"][0]["course_names"] == "圍棋、珠心算（候補）"


class TestSoftDeleteCapacityConsistency:
    def test_soft_deleted_registration_releases_public_availability(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_classroom(session, "海豚班")
            course = _create_course(
                session, "圍棋", 1200, capacity=1, allow_waitlist=False
            )
            deleted_registration = _create_registration(
                session,
                student_name="乙生",
                class_name="海豚班",
                is_active=False,
            )
            session.add(
                RegistrationCourse(
                    registration_id=deleted_registration.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.commit()

        res = client.get("/api/activity/public/courses/availability")

        assert res.status_code == 200
        assert res.json()["圍棋"] == 1

    def test_soft_deleted_registration_excluded_from_admin_course_counts(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            _create_classroom(session, "海豚班")
            course = _create_course(session, "圍棋", 1200, capacity=3)
            active_registration = _create_registration(
                session, student_name="甲生", class_name="海豚班"
            )
            deleted_registration = _create_registration(
                session,
                student_name="乙生",
                class_name="海豚班",
                is_active=False,
            )
            session.add(
                RegistrationCourse(
                    registration_id=active_registration.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.add(
                RegistrationCourse(
                    registration_id=deleted_registration.id,
                    course_id=course.id,
                    status="waitlist",
                    price_snapshot=1200,
                )
            )
            session.commit()

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.get("/api/activity/courses")

        assert res.status_code == 200
        course_row = res.json()["courses"][0]
        assert course_row["enrolled"] == 1
        assert course_row["waitlist_count"] == 0
        assert course_row["remaining"] == 2

    def test_promote_waitlist_ignores_soft_deleted_registration_capacity(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            _create_classroom(session, "海豚班")
            course = _create_course(session, "圍棋", 1200, capacity=1)
            deleted_registration = _create_registration(
                session,
                student_name="舊報名",
                class_name="海豚班",
                is_active=False,
            )
            waiting_registration = _create_registration(
                session, student_name="新候補", class_name="海豚班"
            )
            session.add(
                RegistrationCourse(
                    registration_id=deleted_registration.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.add(
                RegistrationCourse(
                    registration_id=waiting_registration.id,
                    course_id=course.id,
                    status="waitlist",
                    price_snapshot=1200,
                )
            )
            session.commit()
            course_id = course.id
            waiting_registration_id = waiting_registration.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.put(
            f"/api/activity/registrations/{waiting_registration_id}/waitlist",
            params={"course_id": course_id},
        )

        assert res.status_code == 200

        with session_factory() as session:
            promoted = (
                session.query(RegistrationCourse)
                .filter(
                    RegistrationCourse.registration_id == waiting_registration_id,
                    RegistrationCourse.course_id == course_id,
                )
                .one()
            )
            assert promoted.status == "enrolled"

    def test_delete_course_ignores_soft_deleted_historical_registration(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            _create_classroom(session, "海豚班")
            course = _create_course(session, "圍棋", 1200)
            deleted_registration = _create_registration(
                session,
                student_name="舊報名",
                class_name="海豚班",
                is_active=False,
            )
            session.add(
                RegistrationCourse(
                    registration_id=deleted_registration.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.commit()
            course_id = course.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.delete(f"/api/activity/courses/{course_id}")

        assert res.status_code == 200

        with session_factory() as session:
            course = (
                session.query(ActivityCourse)
                .filter(ActivityCourse.id == course_id)
                .one()
            )
            assert course.is_active is False


class TestPublicUpdateRegressions:
    def test_public_update_uses_db_prices_for_snapshots(self, activity_client):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_classroom(session, "海豚班")
            course = _create_course(session, "圍棋", 1200)
            supply = _create_supply(session, "教材包", 350)
            registration = _create_registration(
                session, student_name="王小明", class_name="海豚班"
            )
            session.add(
                RegistrationCourse(
                    registration_id=registration.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.add(
                RegistrationSupply(
                    registration_id=registration.id,
                    supply_id=supply.id,
                    price_snapshot=350,
                )
            )
            session.commit()
            registration_id = registration.id

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": registration_id,
                "name": "王小明",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [{"name": "教材包", "price": "2"}],
            },
        )

        assert res.status_code == 200

        with session_factory() as session:
            course_row = (
                session.query(RegistrationCourse)
                .filter(RegistrationCourse.registration_id == registration_id)
                .one()
            )
            supply_row = (
                session.query(RegistrationSupply)
                .filter(RegistrationSupply.registration_id == registration_id)
                .one()
            )

            assert course_row.price_snapshot == 1200
            assert supply_row.price_snapshot == 350

    def test_public_update_ignores_soft_deleted_registration_capacity(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_classroom(session, "海豚班")
            course = _create_course(session, "圍棋", 1200, capacity=1)
            deleted_registration = _create_registration(
                session,
                student_name="舊報名",
                class_name="海豚班",
                is_active=False,
            )
            target_registration = _create_registration(
                session,
                student_name="王小明",
                class_name="海豚班",
            )
            session.add(
                RegistrationCourse(
                    registration_id=deleted_registration.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.commit()
            course_id = course.id
            registration_id = target_registration.id

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": registration_id,
                "name": "王小明",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1200"}],
                "supplies": [],
            },
        )

        assert res.status_code == 200

        with session_factory() as session:
            course_row = (
                session.query(RegistrationCourse)
                .filter(
                    RegistrationCourse.registration_id == registration_id,
                    RegistrationCourse.course_id == course_id,
                )
                .one()
            )
            assert course_row.status == "enrolled"

    def test_public_query_waitlist_position_excludes_soft_deleted_rows(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_classroom(session, "海豚班")
            course = _create_course(session, "圍棋", 1200)
            deleted_registration = _create_registration(
                session,
                student_name="舊候補",
                class_name="海豚班",
                is_active=False,
            )
            target_registration = _create_registration(
                session,
                student_name="王小明",
                class_name="海豚班",
            )
            session.add(
                RegistrationCourse(
                    registration_id=deleted_registration.id,
                    course_id=course.id,
                    status="waitlist",
                    price_snapshot=1200,
                )
            )
            session.add(
                RegistrationCourse(
                    registration_id=target_registration.id,
                    course_id=course.id,
                    status="waitlist",
                    price_snapshot=1200,
                )
            )
            session.commit()

        res = client.get(
            "/api/activity/public/query",
            params={
                "name": "王小明",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
            },
        )

        assert res.status_code == 200
        waitlist_course = res.json()["courses"][0]
        assert waitlist_course["status"] == "waitlist"
        assert waitlist_course["waitlist_position"] == 1

    def test_public_update_unknown_class_is_soft_accepted_for_pending(
        self, activity_client
    ):
        """public/update 不再硬擋停用班級。若 registration 尚無 classroom_id（pending
        狀態），接受家長輸入的字串（後台可在審核時修正）。"""
        client, session_factory = activity_client

        with session_factory() as session:
            _create_classroom(session, "海豚班")
            _create_classroom(session, "向日葵班", is_active=False)
            registration = _create_registration(
                session, student_name="王小明", class_name="海豚班"
            )
            session.commit()
            registration_id = registration.id

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": registration_id,
                "name": "王小明",
                "birthday": "2020-01-01",
                "parent_phone": "0912345678",
                "class": "向日葵班",
                "courses": [],
                "supplies": [],
            },
        )

        assert res.status_code == 200
        with session_factory() as session:
            reg = (
                session.query(ActivityRegistration).filter_by(id=registration_id).one()
            )
            assert reg.class_name == "向日葵班"


class TestCourseEnrolledRoster:
    """GET /api/activity/courses/{id}/enrolled 課程報名名單端點。"""

    def test_returns_only_enrolled_not_waitlist(self, activity_client):
        """status=enrolled 的才會回，waitlist 不出現。"""
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            course = _create_course(session, "美術", 1000, capacity=1)
            reg_e = _create_registration(
                session,
                student_name="正式生",
                class_name="大班",
                parent_phone="0911111111",
            )
            reg_w = _create_registration(
                session,
                student_name="候補生",
                class_name="大班",
                parent_phone="0922222222",
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg_e.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1000,
                )
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg_w.id,
                    course_id=course.id,
                    status="waitlist",
                    price_snapshot=1000,
                )
            )
            session.commit()
            course_id = course.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.get(f"/api/activity/courses/{course_id}/enrolled")
        assert res.status_code == 200
        body = res.json()
        assert body["course_id"] == course_id
        assert body["course_name"] == "美術"
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["position"] == 1
        assert item["student_name"] == "正式生"
        assert item["class_name"] == "大班"
        assert "registration_id" in item
        assert "course_record_id" in item

    def test_returns_404_when_course_missing(self, activity_client):
        """課程不存在回 404。"""
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            session.commit()

        login_res = _login(client)
        assert login_res.status_code == 200
        res = client.get("/api/activity/courses/999999/enrolled")
        assert res.status_code == 404

    def test_excludes_inactive_registrations(self, activity_client):
        """is_active=False（軟刪）的 registration 不出現。"""
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            course = _create_course(session, "書法", 1200, capacity=2)
            reg_active = _create_registration(
                session,
                student_name="在籍生",
                class_name="大班",
                parent_phone="0911111111",
            )
            reg_deleted = _create_registration(
                session,
                student_name="已退",
                class_name="大班",
                is_active=False,
                parent_phone="0922222222",
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg_active.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg_deleted.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.commit()
            course_id = course.id

        login_res = _login(client)
        assert login_res.status_code == 200
        res = client.get(f"/api/activity/courses/{course_id}/enrolled")
        assert res.status_code == 200
        body = res.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["student_name"] == "在籍生"
