"""家長首頁彙總端點 /api/parent/home/summary 測試。

涵蓋：
- 基本路徑：me / children / summary 三段都正確
- 跨子女彙總：fees outstanding_count、events pending 跨多個學生
- 邊界：無子女、無未繳費、無未讀公告、無待簽事件
- 既有 helper 不破壞：announcements unread-count、fees summary、events list 仍應正常
- 角色隔離：role != parent → 403
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
from models.database import (
    Announcement,
    AnnouncementParentRead,
    AnnouncementParentRecipient,
    Base,
    Classroom,
    EventAcknowledgment,
    Guardian,
    SchoolEvent,
    Student,
    User,
)
from models.fees import FeeItem, StudentFeeRecord
from utils.auth import create_access_token


@pytest.fixture
def home_client(tmp_path):
    db_path = tmp_path / "home-summary.sqlite"
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


def _make_parent(session, *, line_user_id="U1", username=None) -> User:
    user = User(
        username=username or f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _make_classroom(session, name="向日葵") -> Classroom:
    classroom = session.query(Classroom).filter(Classroom.name == name).first()
    if not classroom:
        classroom = Classroom(name=name, is_active=True)
        session.add(classroom)
        session.flush()
    return classroom


def _add_child(session, user: User, *, name: str, classroom: Classroom) -> Student:
    student = Student(
        student_id=f"S_{name}",
        name=name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="父親",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return student


def _add_unpaid_fee(session, student: Student, *, amount=5000, due_offset_days=None):
    """新增 1 筆未繳費 record。due_offset_days 為相對今天的天數（None=無到期日）。"""
    item = FeeItem(name="學費", amount=amount, period="2026-1", is_active=True)
    session.add(item)
    session.flush()
    rec = StudentFeeRecord(
        student_id=student.id,
        student_name=student.name,
        classroom_name="向日葵",
        fee_item_id=item.id,
        fee_item_name="學費",
        amount_due=amount,
        amount_paid=0,
        status="unpaid",
        period="2026-1",
        due_date=(
            date.today() + timedelta(days=due_offset_days)
            if due_offset_days is not None
            else None
        ),
    )
    session.add(rec)
    session.flush()
    return rec


def _add_announcement_for_all(session, *, title: str, author_id: int) -> Announcement:
    ann = Announcement(
        title=title,
        content="...",
        priority="normal",
        is_pinned=False,
        created_at=datetime.now(),
        created_by=author_id,
    )
    session.add(ann)
    session.flush()
    session.add(AnnouncementParentRecipient(announcement_id=ann.id, scope="all"))
    session.flush()
    return ann


def _make_admin(session, *, username="admin") -> User:
    admin = User(
        username=username,
        password_hash="x",
        role="admin",
        permissions=0,
        is_active=True,
        token_version=0,
    )
    session.add(admin)
    session.flush()
    return admin


def _add_event_requiring_ack(session, *, title="校外教學") -> SchoolEvent:
    ev = SchoolEvent(
        title=title,
        description="",
        event_date=date.today() + timedelta(days=7),
        event_type="general",
        is_all_day=True,
        is_active=True,
        requires_acknowledgment=True,
    )
    session.add(ev)
    session.flush()
    return ev


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


class TestHomeSummaryBasics:
    def test_returns_me_children_and_summary_keys(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            _add_child(session, user, name="小明", classroom=classroom)
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"me", "children", "summary"}
        assert data["me"]["role"] == "parent"
        assert data["me"]["name"].startswith("parent_line_")
        assert len(data["children"]) == 1
        assert data["children"][0]["name"] == "小明"
        s = data["summary"]
        assert "unread_announcements" in s
        assert "fees" in s
        assert "pending_event_acks" in s
        assert "unread_messages" in s
        assert "pending_activity_promotions" in s
        assert "recent_leave_reviews" in s

    def test_no_children_returns_empty_lists(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        data = resp.json()
        assert data["children"] == []
        assert data["summary"]["fees"]["outstanding_count"] == 0
        assert data["summary"]["fees"]["outstanding"] == 0
        assert data["summary"]["unread_announcements"] == 0
        assert data["summary"]["pending_event_acks"] == 0
        assert data["summary"]["pending_activity_promotions"] == 0
        assert data["summary"]["recent_leave_reviews"] == 0


class TestFeesAggregation:
    def test_outstanding_count_aggregates_across_children(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            child_a = _add_child(session, user, name="A", classroom=classroom)
            child_b = _add_child(session, user, name="B", classroom=classroom)
            _add_unpaid_fee(
                session, child_a, amount=10000, due_offset_days=-2
            )  # overdue
            _add_unpaid_fee(
                session, child_a, amount=2000, due_offset_days=3
            )  # due_soon
            _add_unpaid_fee(session, child_b, amount=8000, due_offset_days=30)  # future
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        fees = resp.json()["summary"]["fees"]
        assert fees["outstanding_count"] == 3
        assert fees["outstanding"] == 20000
        assert fees["overdue"] == 10000
        assert fees["due_soon"] == 2000

    def test_paid_records_excluded_from_outstanding_count(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            child = _add_child(session, user, name="A", classroom=classroom)
            rec = _add_unpaid_fee(session, child, amount=5000)
            rec.amount_paid = 5000
            rec.status = "paid"
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        fees = resp.json()["summary"]["fees"]
        assert fees["outstanding_count"] == 0
        assert fees["outstanding"] == 0


class TestUnreadAnnouncements:
    def test_unread_count_counts_visible_minus_read(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            admin = _make_admin(session)
            classroom = _make_classroom(session)
            _add_child(session, user, name="小明", classroom=classroom)
            ann1 = _add_announcement_for_all(session, title="A", author_id=admin.id)
            _add_announcement_for_all(session, title="B", author_id=admin.id)
            session.add(
                AnnouncementParentRead(
                    announcement_id=ann1.id,
                    user_id=user.id,
                    read_at=datetime.now(),
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        assert resp.json()["summary"]["unread_announcements"] == 1


class TestPendingEventAcks:
    def test_pending_counts_per_student_event_pair(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            child_a = _add_child(session, user, name="A", classroom=classroom)
            child_b = _add_child(session, user, name="B", classroom=classroom)
            ev = _add_event_requiring_ack(session)
            session.add(
                EventAcknowledgment(
                    event_id=ev.id,
                    user_id=user.id,
                    student_id=child_a.id,
                    acknowledged_at=datetime.now(),
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        # 1 個事件 × 2 個學生 = 2 個 pair；child_a 已簽 → 還剩 1 個 pending
        assert resp.json()["summary"]["pending_event_acks"] == 1

    def test_event_not_requiring_ack_excluded(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            _add_child(session, user, name="A", classroom=classroom)
            ev = SchoolEvent(
                title="一般通知",
                description="",
                event_date=date.today() + timedelta(days=3),
                event_type="general",
                is_all_day=True,
                is_active=True,
                requires_acknowledgment=False,
            )
            session.add(ev)
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        assert resp.json()["summary"]["pending_event_acks"] == 0


class TestActivityPromotionAndLeaveReviewCounts:
    def test_promoted_pending_counted_as_pending_promotion(self, home_client):
        """RegistrationCourse status='promoted_pending' 計入 pending_activity_promotions。"""
        from models.activity import (
            ActivityCourse,
            ActivityRegistration,
            RegistrationCourse,
        )

        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            student = _add_child(session, user, name="小明", classroom=classroom)

            course = ActivityCourse(
                name="繪畫A",
                school_year=115,
                semester=1,
                price=1000,
                is_active=True,
                allow_waitlist=True,
            )
            session.add(course)
            session.flush()

            reg = ActivityRegistration(
                student_name=student.name,
                student_id=student.id,
                school_year=115,
                semester=1,
                paid_amount=0,
                is_active=True,
            )
            session.add(reg)
            session.flush()

            # 一筆 enrolled（不該被計）+ 一筆 promoted_pending（要被計）
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="enrolled",
                )
            )
            course2 = ActivityCourse(
                name="繪畫B",
                school_year=115,
                semester=1,
                price=1000,
                is_active=True,
                allow_waitlist=True,
            )
            session.add(course2)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course2.id,
                    status="promoted_pending",
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        assert resp.json()["summary"]["pending_activity_promotions"] == 1

    def test_recent_leave_review_counted(self, home_client):
        """最近 7 天內 reviewed 的請假計入 recent_leave_reviews。"""
        from models.student_leave import StudentLeaveRequest

        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            student = _add_child(session, user, name="小明", classroom=classroom)

            # 已批准（最近）— 計入
            session.add(
                StudentLeaveRequest(
                    student_id=student.id,
                    applicant_user_id=user.id,
                    leave_type="病假",
                    start_date=date.today(),
                    end_date=date.today(),
                    status="approved",
                    reviewed_at=datetime.now() - timedelta(days=1),
                )
            )
            # 還在 pending — 不計入
            session.add(
                StudentLeaveRequest(
                    student_id=student.id,
                    applicant_user_id=user.id,
                    leave_type="事假",
                    start_date=date.today() + timedelta(days=2),
                    end_date=date.today() + timedelta(days=2),
                    status="pending",
                )
            )
            # 8 天前 reviewed — 過期不計入
            session.add(
                StudentLeaveRequest(
                    student_id=student.id,
                    applicant_user_id=user.id,
                    leave_type="病假",
                    start_date=date.today() - timedelta(days=10),
                    end_date=date.today() - timedelta(days=10),
                    status="approved",
                    reviewed_at=datetime.now() - timedelta(days=8),
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        assert resp.status_code == 200
        assert resp.json()["summary"]["recent_leave_reviews"] == 1


class TestTodayStatus:
    def test_today_status_no_children_returns_empty(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/home/today-status", cookies={"access_token": token}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["children"] == []
        assert "date" in data

    def test_today_status_aggregates_attendance_leave_medication_dismissal(
        self, home_client
    ):
        from models.classroom import StudentAttendance
        from models.dismissal import StudentDismissalCall
        from models.portfolio import StudentMedicationOrder
        from models.student_leave import StudentLeaveRequest

        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            student = _add_child(session, user, name="小明", classroom=classroom)

            # 今日出席
            session.add(
                StudentAttendance(
                    student_id=student.id, date=date.today(), status="出席"
                )
            )
            # 今日是 approved 請假範圍內
            session.add(
                StudentLeaveRequest(
                    student_id=student.id,
                    applicant_user_id=user.id,
                    leave_type="病假",
                    start_date=date.today(),
                    end_date=date.today(),
                    status="approved",
                    reviewed_at=datetime.now(),
                )
            )
            # 今日有用藥單
            session.add(
                StudentMedicationOrder(
                    student_id=student.id,
                    order_date=date.today(),
                    medication_name="退燒藥",
                    dose="1 顆",
                    time_slots=["08:30"],
                    source="parent",
                )
            )
            # 今日 pending 接送
            session.add(
                StudentDismissalCall(
                    student_id=student.id,
                    classroom_id=classroom.id,
                    requested_by_user_id=user.id,
                    status="pending",
                    requested_at=datetime.now(),
                )
            )
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/home/today-status", cookies={"access_token": token}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["children"]) == 1
        c = data["children"][0]
        assert c["name"] == "小明"
        assert c["attendance"]["status"] == "出席"
        assert c["leave"]["type"] == "病假"
        assert c["leave"]["status"] == "approved"
        assert c["medication"]["has_order"] is True
        assert c["medication"]["order_count"] == 1
        assert c["dismissal"]["status"] == "pending"


class TestRoleIsolation:
    def test_non_parent_returns_403(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            employee_user = User(
                username="staff",
                password_hash="x",
                role="staff",
                permissions=0,
                is_active=True,
                token_version=0,
            )
            session.add(employee_user)
            session.commit()
            token = create_access_token(
                {
                    "user_id": employee_user.id,
                    "employee_id": None,
                    "role": "staff",
                    "name": "staff",
                    "permissions": 0,
                    "token_version": 0,
                }
            )

        resp = client.get("/api/parent/home/summary", cookies={"access_token": token})
        assert resp.status_code == 403


class TestExistingEndpointsStillWork:
    """確保抽 helper 後既有端點行為不變。"""

    def test_announcements_unread_count_endpoint(self, home_client):
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            admin = _make_admin(session)
            classroom = _make_classroom(session)
            _add_child(session, user, name="小明", classroom=classroom)
            _add_announcement_for_all(session, title="A", author_id=admin.id)
            _add_announcement_for_all(session, title="B", author_id=admin.id)
            session.commit()
            token = _parent_token(user)

        resp = client.get(
            "/api/parent/announcements/unread-count",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        assert resp.json() == {"unread_count": 2}

    def test_fees_summary_endpoint_no_outstanding_count_leak_to_by_student(
        self, home_client
    ):
        """by_student entry 不應有 outstanding_count（那是整體 totals 用的）。"""
        client, session_factory = home_client
        with session_factory() as session:
            user = _make_parent(session)
            classroom = _make_classroom(session)
            child = _add_child(session, user, name="A", classroom=classroom)
            _add_unpaid_fee(session, child, amount=5000)
            session.commit()
            token = _parent_token(user)

        resp = client.get("/api/parent/fees/summary", cookies={"access_token": token})
        data = resp.json()
        assert data["totals"]["outstanding_count"] == 1
        for entry in data["by_student"]:
            assert "outstanding_count" not in entry
