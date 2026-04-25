"""家長端公告 + 事件簽閱（Batch 4）。"""

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
    AnnouncementParentRecipient,
    AnnouncementParentRead,
    Base,
    Classroom,
    Employee,
    EventAcknowledgment,
    Guardian,
    SchoolEvent,
    Student,
    User,
)
from utils.auth import create_access_token


@pytest.fixture
def parent_event_client(tmp_path):
    db_path = tmp_path / "parent-events.sqlite"
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


def _create_parent_with_child(
    session, *, line_user_id: str, student_name: str, classroom_name: str = "向日葵班"
):
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
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, guardian, student, classroom


def _create_employee_author(session) -> Employee:
    emp = Employee(
        employee_id="ANN001",
        name="公告發布者",
        base_salary=30000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _create_announcement(
    session, *, title: str, author_id: int, scope: str = "all", **scope_kwargs
) -> Announcement:
    ann = Announcement(title=title, content="內容", created_by=author_id)
    session.add(ann)
    session.flush()
    session.add(
        AnnouncementParentRecipient(
            announcement_id=ann.id, scope=scope, **scope_kwargs
        )
    )
    session.flush()
    return ann


def _make_token(user: User) -> str:
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


# ── Announcement scope ─────────────────────────────────────────────────


class TestAnnouncementScope:
    def test_all_scope_visible_to_every_parent(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user_a, _, _, _ = _create_parent_with_child(
                session, line_user_id="UA", student_name="A1"
            )
            _create_parent_with_child(
                session, line_user_id="UB", student_name="B1", classroom_name="B班"
            )
            author = _create_employee_author(session)
            _create_announcement(
                session, title="全校公告", author_id=author.id, scope="all"
            )
            session.commit()
            token = _make_token(user_a)

        resp = client.get("/api/parent/announcements", cookies={"access_token": token})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["title"] == "全校公告"
        assert items[0]["is_read"] is False

    def test_classroom_scope_only_visible_to_that_class(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user_a, _, _, classroom_a = _create_parent_with_child(
                session, line_user_id="UA2", student_name="A班生", classroom_name="A班"
            )
            user_b, _, _, _ = _create_parent_with_child(
                session, line_user_id="UB2", student_name="B班生", classroom_name="B班"
            )
            author = _create_employee_author(session)
            _create_announcement(
                session,
                title="A班公告",
                author_id=author.id,
                scope="classroom",
                classroom_id=classroom_a.id,
            )
            session.commit()
            token_a = _make_token(user_a)
            token_b = _make_token(user_b)

        resp_a = client.get(
            "/api/parent/announcements", cookies={"access_token": token_a}
        )
        resp_b = client.get(
            "/api/parent/announcements", cookies={"access_token": token_b}
        )
        assert resp_a.status_code == 200 and resp_b.status_code == 200
        assert len(resp_a.json()["items"]) == 1
        assert len(resp_b.json()["items"]) == 0

    def test_student_scope_only_visible_to_that_student_parent(
        self, parent_event_client
    ):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user_a, _, student_a, _ = _create_parent_with_child(
                session, line_user_id="UA3", student_name="目標"
            )
            user_b, _, _, _ = _create_parent_with_child(
                session, line_user_id="UB3", student_name="非目標", classroom_name="B"
            )
            author = _create_employee_author(session)
            _create_announcement(
                session,
                title="僅 A",
                author_id=author.id,
                scope="student",
                student_id=student_a.id,
            )
            session.commit()
            token_a = _make_token(user_a)
            token_b = _make_token(user_b)

        assert (
            len(
                client.get(
                    "/api/parent/announcements", cookies={"access_token": token_a}
                ).json()["items"]
            )
            == 1
        )
        assert (
            len(
                client.get(
                    "/api/parent/announcements", cookies={"access_token": token_b}
                ).json()["items"]
            )
            == 0
        )


class TestAnnouncementRead:
    def test_mark_read_idempotent(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user, _, _, _ = _create_parent_with_child(
                session, line_user_id="UR", student_name="R1"
            )
            author = _create_employee_author(session)
            ann = _create_announcement(
                session, title="X", author_id=author.id, scope="all"
            )
            session.commit()
            token = _make_token(user)
            ann_id = ann.id
            user_id = user.id

        resp1 = client.post(
            f"/api/parent/announcements/{ann_id}/read",
            cookies={"access_token": token},
        )
        resp2 = client.post(
            f"/api/parent/announcements/{ann_id}/read",
            cookies={"access_token": token},
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        with session_factory() as session:
            reads = session.query(AnnouncementParentRead).filter(
                AnnouncementParentRead.user_id == user_id
            ).all()
            assert len(reads) == 1  # 冪等

    def test_unread_count_decreases_after_read(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user, _, _, _ = _create_parent_with_child(
                session, line_user_id="UC", student_name="C1"
            )
            author = _create_employee_author(session)
            ann1 = _create_announcement(
                session, title="A", author_id=author.id, scope="all"
            )
            _create_announcement(
                session, title="B", author_id=author.id, scope="all"
            )
            session.commit()
            token = _make_token(user)
            ann1_id = ann1.id

        before = client.get(
            "/api/parent/announcements/unread-count", cookies={"access_token": token}
        ).json()["unread_count"]
        client.post(
            f"/api/parent/announcements/{ann1_id}/read",
            cookies={"access_token": token},
        )
        after = client.get(
            "/api/parent/announcements/unread-count", cookies={"access_token": token}
        ).json()["unread_count"]
        assert before == 2
        assert after == 1

    def test_mark_read_not_visible_returns_403(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user_a, _, _, _ = _create_parent_with_child(
                session, line_user_id="UA4", student_name="A4"
            )
            user_b, _, _, _ = _create_parent_with_child(
                session, line_user_id="UB4", student_name="B4", classroom_name="B"
            )
            author = _create_employee_author(session)
            ann = _create_announcement(
                session, title="只給 B 班", author_id=author.id, scope="guardian",
                guardian_id=session.query(Guardian).filter(Guardian.user_id == user_b.id).first().id,
            )
            session.commit()
            token_a = _make_token(user_a)
            ann_id = ann.id

        # A 家長對 B 的 guardian-scoped 公告應沒可見性
        resp = client.post(
            f"/api/parent/announcements/{ann_id}/read",
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403


# ── Event 簽閱 ──────────────────────────────────────────────────────


class TestEventAck:
    def test_list_events_includes_ack_status(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user, _, student, _ = _create_parent_with_child(
                session, line_user_id="UE", student_name="E1"
            )
            today = date.today()
            event = SchoolEvent(
                title="親師懇談",
                event_date=today + timedelta(days=7),
                event_type="meeting",
                requires_acknowledgment=True,
                ack_deadline=today + timedelta(days=10),
                is_active=True,
            )
            session.add(event)
            session.commit()
            token = _make_token(user)
            student_id = student.id

        resp = client.get("/api/parent/events", cookies={"access_token": token})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["requires_acknowledgment"] is True
        assert student_id in items[0]["need_ack_student_ids"]

    def test_ack_event_idempotent(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user, _, student, _ = _create_parent_with_child(
                session, line_user_id="UE2", student_name="E2"
            )
            event = SchoolEvent(
                title="疏散演習",
                event_date=date.today(),
                event_type="activity",
                requires_acknowledgment=True,
                is_active=True,
            )
            session.add(event)
            session.commit()
            token = _make_token(user)
            student_id = student.id
            event_id = event.id
            user_id = user.id

        resp1 = client.post(
            f"/api/parent/events/{event_id}/ack",
            json={"student_id": student_id, "signature_name": "王大明"},
            cookies={"access_token": token},
        )
        resp2 = client.post(
            f"/api/parent/events/{event_id}/ack",
            json={"student_id": student_id},
            cookies={"access_token": token},
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp2.json()["already_acknowledged"] is True
        with session_factory() as session:
            acks = session.query(EventAcknowledgment).filter(
                EventAcknowledgment.user_id == user_id
            ).all()
            assert len(acks) == 1

    def test_ack_other_child_returns_403(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user_a, _, _, _ = _create_parent_with_child(
                session, line_user_id="UE3A", student_name="EA"
            )
            _, _, student_b, _ = _create_parent_with_child(
                session, line_user_id="UE3B", student_name="EB", classroom_name="B"
            )
            event = SchoolEvent(
                title="活動",
                event_date=date.today(),
                event_type="activity",
                requires_acknowledgment=True,
                is_active=True,
            )
            session.add(event)
            session.commit()
            token_a = _make_token(user_a)
            event_id = event.id
            student_b_id = student_b.id

        resp = client.post(
            f"/api/parent/events/{event_id}/ack",
            json={"student_id": student_b_id},
            cookies={"access_token": token_a},
        )
        assert resp.status_code == 403

    def test_ack_event_without_requires_ack_returns_400(self, parent_event_client):
        client, session_factory = parent_event_client
        with session_factory() as session:
            user, _, student, _ = _create_parent_with_child(
                session, line_user_id="UE4", student_name="E4"
            )
            event = SchoolEvent(
                title="一般",
                event_date=date.today(),
                event_type="general",
                requires_acknowledgment=False,
                is_active=True,
            )
            session.add(event)
            session.commit()
            token = _make_token(user)
            event_id = event.id
            student_id = student.id

        resp = client.post(
            f"/api/parent/events/{event_id}/ack",
            json={"student_id": student_id},
            cookies={"access_token": token},
        )
        assert resp.status_code == 400
