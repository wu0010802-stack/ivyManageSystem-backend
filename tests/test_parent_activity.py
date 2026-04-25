"""家長端才藝課登入版測試（Batch 7）。

涵蓋：
- list courses 含 enrolled_count / is_full
- register happy path：parent_phone 從 Guardian 自動帶、match_status='manual'
- register 額滿 → waitlist
- register 額滿且不允候補 → 400
- 同學期重複報名 → 400
- 非自己小孩報名 → 403
- my-registrations 僅列自己小孩
- payments：不揭露 operator
- confirm-promotion happy path
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router as parent_portal_router
from models.activity import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    RegistrationCourse,
)
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "activity.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    app.include_router(parent_portal_router)
    with TestClient(app) as client:
        yield client, session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _setup_family(session, *, line_user_id="UA", student_name="阿活", classroom_name="活力班"):
    user = User(
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = (
        session.query(Classroom).filter(Classroom.name == classroom_name).first()
    )
    if not classroom:
        classroom = Classroom(name=classroom_name, is_active=True)
        session.add(classroom)
        session.flush()
    student = Student(
        student_id=f"S_{student_name}",
        name=student_name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="父親",
        phone="0911000111",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, guardian, student, classroom


def _create_course(
    session,
    *,
    name="繪畫",
    price=2000,
    capacity=2,
    school_year=115,
    semester=1,
    allow_waitlist=True,
):
    course = ActivityCourse(
        name=name,
        price=price,
        capacity=capacity,
        school_year=school_year,
        semester=semester,
        allow_waitlist=allow_waitlist,
        is_active=True,
    )
    session.add(course)
    session.flush()
    return course


def _parent_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permissions": 0,
            "token_version": user.token_version or 0,
        }
    )


class TestListCourses:
    def test_list_courses_with_enrolled_count(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, _, _ = _setup_family(session)
            _create_course(session, name="繪畫", capacity=2)
            _create_course(session, name="音樂", capacity=10)
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/activity/courses",
            params={"school_year": 115, "semester": 1},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert {i["name"] for i in items} == {"繪畫", "音樂"}
        for i in items:
            assert i["enrolled_count"] == 0
            assert i["is_full"] is False


class TestRegister:
    def test_register_happy_path(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫")
            session.commit()
            token = _parent_token(user)
            student_id = student.id
            course_id = course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [course_id],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["match_status"] == "manual"
        assert data["pending_review"] is False
        assert len(data["courses"]) == 1
        assert data["courses"][0]["status"] == "enrolled"
        with session_factory() as session:
            reg = session.query(ActivityRegistration).first()
            assert reg.parent_phone == "0911000111"  # 從 Guardian 自動帶入
            assert reg.classroom_id is not None

    def test_register_when_full_goes_waitlist(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, student_a, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A班"
            )
            user_b, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B班"
            )
            user_c, _, student_c, _ = _setup_family(
                session, line_user_id="UC", student_name="C", classroom_name="C班"
            )
            course = _create_course(session, name="熱門課", capacity=2, allow_waitlist=True)
            session.commit()
            tokens = [_parent_token(u) for u in (user_a, user_b, user_c)]
            student_ids = [student_a.id, student_b.id, student_c.id]
            course_id = course.id

        for token, sid in zip(tokens, student_ids):
            resp = client.post(
                "/api/parent/activity/register",
                json={
                    "student_id": sid,
                    "school_year": 115,
                    "semester": 1,
                    "course_ids": [course_id],
                    "supply_ids": [],
                },
                cookies={"access_token": token},
            )
            assert resp.status_code == 201

        with session_factory() as session:
            statuses = sorted(
                rc.status for rc in session.query(RegistrationCourse).all()
            )
            assert statuses == ["enrolled", "enrolled", "waitlist"]

    def test_register_when_full_and_no_waitlist_returns_400(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, student_a, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A班"
            )
            user_b, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B班"
            )
            course = _create_course(
                session, name="不可候補", capacity=1, allow_waitlist=False
            )
            session.commit()
            token_a = _parent_token(user_a)
            token_b = _parent_token(user_b)
            student_a_id = student_a.id
            student_b_id = student_b.id
            course_id = course.id

        client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_a_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [course_id],
                "supply_ids": [],
            },
            cookies={"access_token": token_a},
        )
        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_b_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [course_id],
                "supply_ids": [],
            },
            cookies={"access_token": token_b},
        )
        assert resp.status_code == 400

    def test_register_other_child_returns_403(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, _, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B"
            )
            course = _create_course(session, name="繪畫")
            session.commit()
            token_a = _parent_token(user_a)
            student_b_id = student_b.id
            course_id = course.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_b_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [course_id],
                "supply_ids": [],
            },
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403

    def test_register_duplicate_in_term_returns_400(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            c1 = _create_course(session, name="繪畫")
            c2 = _create_course(session, name="音樂")
            session.commit()
            token = _parent_token(user)
            student_id = student.id
            c1_id = c1.id
            c2_id = c2.id

        client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [c1_id],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": student_id,
                "school_year": 115,
                "semester": 1,
                "course_ids": [c2_id],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400

    def test_register_empty_courses_and_supplies_returns_400(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            session.commit()
            token = _parent_token(user)
            sid = student.id

        resp = client.post(
            "/api/parent/activity/register",
            json={
                "student_id": sid,
                "school_year": 115,
                "semester": 1,
                "course_ids": [],
                "supply_ids": [],
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400


class TestMyRegistrationsAndPayments:
    def test_my_registrations_only_owned(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, student_a, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B"
            )
            session.add(
                ActivityRegistration(
                    student_name="A",
                    is_active=True,
                    school_year=115,
                    semester=1,
                    student_id=student_a.id,
                    parent_phone="0911",
                    pending_review=False,
                    match_status="manual",
                )
            )
            session.add(
                ActivityRegistration(
                    student_name="B",
                    is_active=True,
                    school_year=115,
                    semester=1,
                    student_id=student_b.id,
                    parent_phone="0922",
                    pending_review=False,
                    match_status="manual",
                )
            )
            session.commit()
            token = _parent_token(user_a)

        resp = client.get(
            "/api/parent/activity/my-registrations",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["student_name"] == "A"

    def test_payments_does_not_leak_operator(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            reg = ActivityRegistration(
                student_name=student.name,
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student.id,
                parent_phone="0911",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="payment",
                    amount=2000,
                    payment_date=date(2026, 4, 1),
                    payment_method="現金",
                    operator="財務人員",
                    receipt_no="POS-20260401-XYZ",
                )
            )
            session.commit()
            token = _parent_token(user)
            reg_id = reg.id

        resp = client.get(
            f"/api/parent/activity/registrations/{reg_id}/payments",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["receipt_no"] == "POS-20260401-XYZ"
        assert "operator" not in items[0]


class TestConfirmPromotion:
    def test_confirm_promotion_happy_path(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user, _, student, _ = _setup_family(session)
            course = _create_course(session, name="繪畫")
            reg = ActivityRegistration(
                student_name=student.name,
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student.id,
                parent_phone="0911",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="promoted_pending",
                    price_snapshot=course.price,
                    promoted_at=datetime.now(),
                    confirm_deadline=datetime.now() + timedelta(hours=24),
                )
            )
            session.commit()
            token = _parent_token(user)
            reg_id = reg.id
            course_id = course.id

        resp = client.post(
            f"/api/parent/activity/registrations/{reg_id}/confirm-promotion",
            json={"course_id": course_id},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        with session_factory() as session:
            rc = session.query(RegistrationCourse).first()
            assert rc.status == "enrolled"

    def test_confirm_promotion_other_child_returns_403(self, activity_client):
        client, session_factory = activity_client
        with session_factory() as session:
            user_a, _, _, _ = _setup_family(
                session, line_user_id="UA", student_name="A", classroom_name="A"
            )
            _, _, student_b, _ = _setup_family(
                session, line_user_id="UB", student_name="B", classroom_name="B"
            )
            course = _create_course(session, name="X")
            reg_b = ActivityRegistration(
                student_name="B",
                is_active=True,
                school_year=115,
                semester=1,
                student_id=student_b.id,
                parent_phone="0922",
                pending_review=False,
                match_status="manual",
            )
            session.add(reg_b)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg_b.id,
                    course_id=course.id,
                    status="promoted_pending",
                )
            )
            session.commit()
            token_a = _parent_token(user_a)
            reg_b_id = reg_b.id
            course_id = course.id

        resp = client.post(
            f"/api/parent/activity/registrations/{reg_b_id}/confirm-promotion",
            json={"course_id": course_id},
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403
