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
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
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
        permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
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


class TestStatsTermParams:
    """stats 端點學期參數接線（與 dashboard-table 同名同語意，缺省=當前學期）。"""

    def test_stats_summary_requires_both_term_params(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            _create_admin(session)
            session.commit()
        assert _login(client).status_code == 200

        # 只給一個參數 → 400（resolve_academic_term_filters 既有慣例）
        res = client.get("/api/activity/stats-summary?school_year=114")
        assert res.status_code == 400

    def test_stats_endpoints_filter_by_term(self, activity_client):
        client, session_factory = activity_client
        sy, sem = _current_term()
        prev_sy, prev_sem = (sy, 1) if sem == 2 else (sy - 1, 2)

        with session_factory() as session:
            _create_admin(session)
            course = _create_course(session, "圍棋", 1200)
            reg = _create_registration(
                session, student_name="王小明", class_name="大班"
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            # 上學期報名：不應計入當前學期 summary
            old_reg = ActivityRegistration(
                student_name="舊學期生",
                birthday="2020-01-01",
                class_name="大班",
                is_active=True,
                school_year=prev_sy,
                semester=prev_sem,
            )
            session.add(old_reg)
            session.commit()

        assert _login(client).status_code == 200

        # 缺省 = 當前學期
        default_res = client.get("/api/activity/stats-summary")
        assert default_res.status_code == 200
        assert default_res.json()["totalRegistrations"] == 1

        # 顯式指定當前學期 = 同結果
        explicit_res = client.get(
            f"/api/activity/stats-summary?school_year={sy}&semester={sem}"
        )
        assert explicit_res.status_code == 200
        assert explicit_res.json()["totalRegistrations"] == 1

        # 指定上學期 → 只看到上學期那筆（無 enrolled 課程）
        prev_res = client.get(
            f"/api/activity/stats-summary?school_year={prev_sy}&semester={prev_sem}"
        )
        assert prev_res.status_code == 200
        assert prev_res.json()["totalRegistrations"] == 1
        assert prev_res.json()["totalEnrollments"] == 0

        # /stats 與 /stats-charts 同樣接受學期參數
        stats_res = client.get(f"/api/activity/stats?school_year={sy}&semester={sem}")
        assert stats_res.status_code == 200
        assert stats_res.json()["statistics"]["totalRegistrations"] == 1
        charts_res = client.get(
            f"/api/activity/stats-charts?school_year={prev_sy}&semester={prev_sem}"
        )
        assert charts_res.status_code == 200
        assert charts_res.json()["topCourses"] == []


class TestStatsResponseModels:
    """T3：stats.py 4 個 JSON 端點補 response_model（shape 不可變）。"""

    def test_stats_json_endpoints_declare_response_model(self):
        from api.activity import stats as stats_module

        wanted = {"/stats", "/stats-summary", "/stats-charts", "/dashboard-table"}
        models = {
            route.path: route.response_model
            for route in stats_module.router.routes
            if route.path in wanted
        }
        assert set(models) == wanted
        assert all(m is not None for m in models.values())

    def test_dashboard_table_shape_survives_response_model(self, activity_client):
        """response_model 不可 silent strip 既有 dashboard-table 欄位。"""
        from models.database import ClassGrade, Student

        client, session_factory = activity_client
        sy, sem = _current_term()

        with session_factory() as session:
            _create_admin(session)
            grade = ClassGrade(name="大班", sort_order=1, is_active=True)
            session.add(grade)
            session.flush()
            classroom = Classroom(
                name="海豚班",
                is_active=True,
                grade_id=grade.id,
                school_year=sy,
                semester=sem,
            )
            session.add(classroom)
            session.flush()
            session.add(
                Student(
                    student_id="S001",
                    name="王小明",
                    classroom_id=classroom.id,
                    is_active=True,
                )
            )
            course = _create_course(session, "圍棋", 1200)
            course_id = course.id
            reg = _create_registration(
                session, student_name="王小明", class_name="海豚班"
            )
            reg.classroom_id = classroom.id
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course_id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            session.commit()

        assert _login(client).status_code == 200
        res = client.get("/api/activity/dashboard-table")
        assert res.status_code == 200
        data = res.json()
        assert data["school_year"] == sy
        assert data["semester"] == sem
        assert data["grand_total"]["student_count"] == 1
        assert data["grand_total"]["courses"] == {str(course_id): 1}
        grade_row = data["grades"][0]
        assert grade_row["target_percent"] == 100
        assert grade_row["subtotal"]["total_enrollments"] == 1
        classroom_row = grade_row["classrooms"][0]
        assert classroom_row["classroom_name"] == "海豚班"
        assert classroom_row["courses"] == {str(course_id): 1}
        assert classroom_row["ratio"] == 100


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

        res = client.post(
            "/api/activity/public/query",
            json={
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


class TestAttendanceCourseEnrollmentGuard:
    """攻擊面收緊：batch_update_attendance 不可為「未報該課」的學生寫出席。

    Why: 點名端點原本只驗證 registration is_active + match_status，未驗證
    RegistrationCourse 是否真的關聯到 session.course_id；操作員（或前端傳錯）
    可為任意 reg 在任意 session 寫 attendance，污染出席統計與 student_id 冗餘欄位。
    """

    def test_batch_update_skips_reg_not_enrolled_in_session_course(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            course_a = _create_course(session, "圍棋", 1000)
            course_b = _create_course(session, "繪畫", 1200)

            reg_a = _create_registration(
                session,
                student_name="阿圍棋",
                class_name="大班",
                parent_phone="0911111111",
            )
            reg_b = _create_registration(
                session,
                student_name="阿繪畫",
                class_name="大班",
                parent_phone="0922222222",
            )
            # reg_a 只報圍棋；reg_b 只報繪畫
            session.add(
                RegistrationCourse(
                    registration_id=reg_a.id,
                    course_id=course_a.id,
                    status="enrolled",
                    price_snapshot=1000,
                )
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg_b.id,
                    course_id=course_b.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )

            sess = ActivitySession(
                course_id=course_a.id,
                session_date=date.today(),
                created_by="test",
            )
            session.add(sess)
            session.commit()
            session_id = sess.id
            reg_a_id = reg_a.id
            reg_b_id = reg_b.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.put(
            f"/api/activity/attendance/sessions/{session_id}/records",
            json={
                "records": [
                    {"registration_id": reg_a_id, "is_present": True, "notes": ""},
                    {"registration_id": reg_b_id, "is_present": True, "notes": ""},
                ]
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["updated"] == 1
        assert body["skipped"] == 1

        with session_factory() as session:
            atts = (
                session.query(ActivityAttendance)
                .filter(ActivityAttendance.session_id == session_id)
                .all()
            )
            assert len(atts) == 1
            assert atts[0].registration_id == reg_a_id

    def test_batch_update_accepts_promoted_pending_enrollment(self, activity_client):
        """promoted_pending（待家長確認的候補升正）也算佔位，應允許點名。"""
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            course = _create_course(session, "陶藝", 800)
            reg = _create_registration(
                session,
                student_name="阿陶",
                class_name="大班",
                parent_phone="0933333333",
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="promoted_pending",
                    price_snapshot=800,
                )
            )
            sess = ActivitySession(
                course_id=course.id,
                session_date=date.today(),
                created_by="test",
            )
            session.add(sess)
            session.commit()
            session_id = sess.id
            reg_id = reg.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.put(
            f"/api/activity/attendance/sessions/{session_id}/records",
            json={
                "records": [
                    {"registration_id": reg_id, "is_present": True, "notes": ""},
                ]
            },
        )
        assert res.status_code == 200
        assert res.json()["updated"] == 1

    def test_batch_update_dedups_duplicate_registration_id(self, activity_client):
        """P2-6：body 含重複 registration_id（且該 reg 本場次尚無紀錄）不可 500。

        原本兩個重複 item 都走 else 分支各 session.add 一筆相同
        (session_id, registration_id) → commit 撞 uq_activity_attendance_session_reg
        IntegrityError、整批點名靜默漏存。修法去重保留最後一筆。
        """
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            course = _create_course(session, "圍棋", 1000)
            reg = _create_registration(
                session,
                student_name="阿重複",
                class_name="大班",
                parent_phone="0944444444",
            )
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1000,
                )
            )
            sess = ActivitySession(
                course_id=course.id,
                session_date=date.today(),
                created_by="test",
            )
            session.add(sess)
            session.commit()
            session_id = sess.id
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.put(
            f"/api/activity/attendance/sessions/{session_id}/records",
            json={
                "records": [
                    {"registration_id": reg_id, "is_present": True, "notes": "first"},
                    {"registration_id": reg_id, "is_present": False, "notes": "last"},
                ]
            },
        )
        assert res.status_code == 200, res.text

        with session_factory() as session:
            atts = (
                session.query(ActivityAttendance)
                .filter(ActivityAttendance.session_id == session_id)
                .all()
            )
            assert len(atts) == 1
            # 去重保留最後一筆
            assert atts[0].is_present is False
            assert atts[0].notes == "last"


class TestTimestampsUseTaipeiTimezone:
    """寫入 naive datetime 欄位的端點必須走台灣時間 helper。

    Why: 同檔的「今日」判定多用 `datetime.now(TAIPEI_TZ).date()`，但部分時間戳寫入
    用裸 `datetime.now()`，server 部署於 UTC 時 8 小時偏移會造成稽核時序錯位
    （如 approved_at 比 close_date 早一天）。本測試比對寫入時刻與台灣 wall-clock
    在合理差內，並驗證與系統 UTC now 之間的差距明顯大於 0（部署於台灣時區的
    CI 無法捕獲 bug，故再透過 `now_taipei_naive` helper 的單元測試做雙保險）。
    """

    def test_now_taipei_naive_helper_returns_taipei_wall_clock(self):
        """now_taipei_naive() 必須等於 datetime.now(TAIPEI_TZ).replace(tzinfo=None)。"""
        from datetime import datetime, timedelta

        from api.activity._shared import TAIPEI_TZ, now_taipei_naive

        ts = now_taipei_naive()
        expected = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
        assert ts.tzinfo is None
        # 連續呼叫差距應在數秒內
        assert abs((ts - expected).total_seconds()) < 2

    def test_reject_registration_writes_taipei_reviewed_at(self, activity_client):
        """reject_registration 寫入的 reviewed_at 必須是台灣時間 (naive)。"""
        from datetime import datetime, timedelta

        from api.activity._shared import TAIPEI_TZ

        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            reg = _create_registration(
                session,
                student_name="待審",
                class_name="大班",
                parent_phone="0944444444",
            )
            session.commit()
            reg_id = reg.id

        login_res = _login(client)
        assert login_res.status_code == 200

        before = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
        res = client.post(
            f"/api/activity/registrations/{reg_id}/reject",
            json={"reason": "資料不符，視為校外生不收件"},
        )
        assert res.status_code == 200
        after = datetime.now(TAIPEI_TZ).replace(tzinfo=None)

        with session_factory() as session:
            reg_row = (
                session.query(ActivityRegistration)
                .filter(ActivityRegistration.id == reg_id)
                .one()
            )
            assert reg_row.reviewed_at is not None
            # reviewed_at 必須落在 [before - 1s, after + 1s] 視窗內
            # （server 若在 UTC 而寫入用 datetime.now()，會偏離 8 小時被抓出）
            assert (
                before - timedelta(seconds=1)
                <= reg_row.reviewed_at
                <= after + timedelta(seconds=1)
            )


class TestCourseSupplyRenameAcrossTerms:
    """update_course / update_supply 的改名查重必須限定學期，否則跨學期同名會誤報 409。

    Why: DB 層 UniqueConstraint 是 (name, school_year, semester)，跨學期同名是
    被允許的；router 的應用層查重若不帶 school_year/semester 過濾，會把跨學期
    存在的同名項目誤判為衝突，阻擋正當的改名/重新命名操作。
    """

    def test_course_rename_to_other_term_existing_name_is_allowed(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            sy, sem = _current_term()
            # 目前學期：圍棋
            course_current = _create_course(session, "圍棋", 1000)
            # 不同學期：書法（將被當作衝突候選的雜訊）
            other_sem = 1 if sem == 2 else 2
            other_sy = sy if sem == 2 else sy - 1
            session.add(
                ActivityCourse(
                    name="書法",
                    price=1500,
                    capacity=30,
                    is_active=True,
                    school_year=other_sy,
                    semester=other_sem,
                )
            )
            session.commit()
            course_id = course_current.id

        login_res = _login(client)
        assert login_res.status_code == 200

        # 把目前學期的圍棋改名為「書法」(目前學期不存在，僅其他學期存在)
        res = client.put(
            f"/api/activity/courses/{course_id}",
            json={"name": "書法"},
        )
        assert res.status_code == 200, res.text

    def test_course_rename_within_same_term_to_existing_name_still_blocked(
        self, activity_client
    ):
        """同學期內改成既有名稱仍要 409，避免破壞 UniqueConstraint。"""
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            _create_course(session, "圍棋", 1000)
            target = _create_course(session, "書法", 1500)
            session.commit()
            target_id = target.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.put(
            f"/api/activity/courses/{target_id}",
            json={"name": "圍棋"},
        )
        assert res.status_code == 400

    def test_supply_rename_to_other_term_existing_name_is_allowed(
        self, activity_client
    ):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            sy, sem = _current_term()
            supply_current = _create_supply(session, "教材包", 350)
            other_sem = 1 if sem == 2 else 2
            other_sy = sy if sem == 2 else sy - 1
            session.add(
                ActivitySupply(
                    name="畫筆組",
                    price=200,
                    is_active=True,
                    school_year=other_sy,
                    semester=other_sem,
                )
            )
            session.commit()
            supply_id = supply_current.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.put(
            f"/api/activity/supplies/{supply_id}",
            json={"name": "畫筆組"},
        )
        assert res.status_code == 200, res.text


class TestSessionCreateRejectsInactiveCourse:
    """create_session 必須拒絕為已停用課程建立場次。"""

    def test_create_session_on_inactive_course_404(self, activity_client):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            course = _create_course(session, "圍棋", 1000)
            course.is_active = False
            session.commit()
            course_id = course.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.post(
            "/api/activity/attendance/sessions",
            json={
                "course_id": course_id,
                "session_date": date.today().isoformat(),
                "notes": "",
            },
        )
        assert res.status_code == 404


class TestPortalBatchAttendanceRecordsCap:
    """PortalBatchAttendanceUpdate.records 必須有 max_length 上限（DoS 防護）。"""

    def test_portal_batch_records_exceeds_cap_rejected(self):
        from pydantic import ValidationError

        from api.portal.activity import PortalBatchAttendanceUpdate

        # 501 筆（超過 500 上限）必須觸發 ValidationError
        with pytest.raises(ValidationError):
            PortalBatchAttendanceUpdate(
                records=[
                    {"registration_id": i, "is_present": True, "notes": ""}
                    for i in range(1, 502)
                ]
            )


class TestOverpaidFilterIncludesSupplyOnlyRegs:
    """payment_status=overpaid 篩選必須支援『只有用品、無課程』的報名。

    Why: subquery 加法 (course_total_sq + supply_total_sq) 若任一為 NULL 會整體
    NULL，導致 `paid_amount > NULL` 永遠 False，超繳的報名查不到。目前
    `coalesce(sum(...), 0)` 已包住兩個子查詢防 NULL；本測試保護未來不被誤刪。
    """

    def test_overpaid_filter_returns_supply_only_overpaid_reg(self, activity_client):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_admin(session)
            supply = _create_supply(session, "教材包", 350)
            reg = _create_registration(
                session,
                student_name="超繳生",
                class_name="大班",
                parent_phone="0955555555",
            )
            # 只有用品、無 RegistrationCourse；paid_amount > 應繳
            session.add(
                RegistrationSupply(
                    registration_id=reg.id,
                    supply_id=supply.id,
                    price_snapshot=350,
                )
            )
            reg.paid_amount = 500  # 超繳
            session.commit()
            reg_id = reg.id

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.get("/api/activity/registrations?payment_status=overpaid")
        assert res.status_code == 200
        body = res.json()
        ids = [r["id"] for r in body.get("registrations", body.get("items", []))]
        assert reg_id in ids, f"overpaid filter 未返回 {reg_id}; got: {body}"
