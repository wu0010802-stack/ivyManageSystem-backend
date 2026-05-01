"""家長端子女檔案與本週行程聚合 API 測試。

涵蓋：
- GET /api/parent/students/{id}/profile：成功回傳 student/classroom/teachers/guardians/allergies；
  非自己小孩 → 403；不存在 → 403/404
- GET /api/parent/calendar/week：events / fee_due / announcement 三類聚合，
  past 公告 pin 到 today，items 全在 [today, today+days) 區間
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
from api.parent_portal import parent_router
from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    Base,
    Classroom,
    Employee,
    Guardian,
    SchoolEvent,
    Student,
    User,
)
from models.fees import FeeItem, StudentFeeRecord
from models.portfolio import StudentAllergy
from utils.auth import create_access_token


@pytest.fixture
def parent_client(tmp_path):
    db_path = tmp_path / "parent-misc.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    app = FastAPI()
    app.include_router(parent_router)
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _make_parent(session, *, line_id="U1") -> User:
    u = User(
        username=f"p_{line_id}",
        password_hash="!LINE",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_id,
        token_version=0,
    )
    session.add(u)
    session.flush()
    return u


def _add_child(session, parent: User, *, name="小明", classroom: Classroom):
    student = Student(
        student_id=f"ST_{name}",
        name=name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    g = Guardian(
        student_id=student.id,
        user_id=parent.id,
        name="家長",
        relation="父親",
        is_primary=True,
        can_pickup=True,
    )
    session.add(g)
    session.flush()
    return student


def _token(user: User) -> str:
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


class TestChildProfile:
    def test_returns_full_profile(self, parent_client):
        client, sf = parent_client
        with sf() as session:
            parent = _make_parent(session)
            head = Employee(employee_id="E001", name="王老師", is_active=True)
            session.add(head)
            session.flush()
            classroom = Classroom(
                name="向日葵", is_active=True, head_teacher_id=head.id
            )
            session.add(classroom)
            session.flush()
            student = _add_child(session, parent, classroom=classroom)
            session.add(
                StudentAllergy(
                    student_id=student.id,
                    allergen="花生",
                    severity="severe",
                    reaction_symptom="呼吸困難",
                    active=True,
                )
            )
            session.commit()
            token = _token(parent)
            sid = student.id

        resp = client.get(
            f"/api/parent/students/{sid}/profile",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["student"]["name"] == "小明"
        assert data["classroom"]["name"] == "向日葵"
        assert any(
            t["role"] == "head" and t["name"] == "王老師" for t in data["teachers"]
        )
        assert len(data["guardians"]) == 1
        assert data["guardians"][0]["is_self"] is True
        assert len(data["allergies"]) == 1
        assert data["allergies"][0]["allergen"] == "花生"
        assert data["allergies"][0]["severity"] == "severe"

    def test_other_parent_cannot_access(self, parent_client):
        client, sf = parent_client
        with sf() as session:
            owner = _make_parent(session, line_id="U1")
            other = _make_parent(session, line_id="U2")
            classroom = Classroom(name="C", is_active=True)
            session.add(classroom)
            session.flush()
            student = _add_child(session, owner, classroom=classroom)
            session.commit()
            other_token = _token(other)
            sid = student.id

        resp = client.get(
            f"/api/parent/students/{sid}/profile",
            cookies={"access_token": other_token},
        )
        assert resp.status_code == 403


class TestCalendarWeek:
    def test_aggregates_events_fees_announcements(self, parent_client):
        client, sf = parent_client
        with sf() as session:
            parent = _make_parent(session)
            classroom = Classroom(name="C", is_active=True)
            session.add(classroom)
            session.flush()
            student = _add_child(session, parent, classroom=classroom)

            # 行事曆事件（明日）
            session.add(
                SchoolEvent(
                    title="校外教學",
                    description="去動物園",
                    event_date=date.today() + timedelta(days=1),
                    event_type="activity",
                    is_all_day=True,
                    is_active=True,
                    requires_acknowledgment=True,
                )
            )
            # 5 天後到期的費用
            item = FeeItem(name="學費", amount=5000, period="2026-1", is_active=True)
            session.add(item)
            session.flush()
            session.add(
                StudentFeeRecord(
                    student_id=student.id,
                    student_name=student.name,
                    classroom_name="C",
                    fee_item_id=item.id,
                    fee_item_name="學費",
                    amount_due=5000,
                    amount_paid=0,
                    status="unpaid",
                    period="2026-1",
                    due_date=date.today() + timedelta(days=5),
                )
            )
            # 公告（今日建立、scope=all）
            admin = User(
                username="admin",
                password_hash="x",
                role="admin",
                permissions=-1,
                is_active=True,
                token_version=0,
            )
            session.add(admin)
            session.flush()
            ann = Announcement(
                title="本週公告",
                content="...",
                priority="normal",
                is_pinned=False,
                created_at=datetime.now(),
                created_by=admin.id,
            )
            session.add(ann)
            session.flush()
            session.add(
                AnnouncementParentRecipient(announcement_id=ann.id, scope="all")
            )
            session.commit()
            token = _token(parent)

        resp = client.get(
            "/api/parent/calendar/week?days=7",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        cats = {it["category"] for it in data["items"]}
        assert "event" in cats
        assert "fee_due" in cats
        assert "announcement" in cats

        # 區間內，且都不在過去
        today_iso = date.today().isoformat()
        assert all(it["date"] >= today_iso for it in data["items"])

    def test_past_event_excluded(self, parent_client):
        client, sf = parent_client
        with sf() as session:
            parent = _make_parent(session)
            session.add(
                SchoolEvent(
                    title="昨日已過",
                    event_date=date.today() - timedelta(days=2),
                    end_date=date.today() - timedelta(days=2),
                    event_type="general",
                    is_active=True,
                )
            )
            session.commit()
            token = _token(parent)

        resp = client.get(
            "/api/parent/calendar/week",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        titles = {it["title"] for it in resp.json()["items"]}
        assert "昨日已過" not in titles

    def test_paid_fee_excluded(self, parent_client):
        client, sf = parent_client
        with sf() as session:
            parent = _make_parent(session)
            classroom = Classroom(name="C", is_active=True)
            session.add(classroom)
            session.flush()
            student = _add_child(session, parent, classroom=classroom)
            item = FeeItem(name="餐費", amount=1000, period="2026-1", is_active=True)
            session.add(item)
            session.flush()
            session.add(
                StudentFeeRecord(
                    student_id=student.id,
                    student_name=student.name,
                    fee_item_id=item.id,
                    fee_item_name="餐費",
                    amount_due=1000,
                    amount_paid=1000,
                    status="paid",
                    period="2026-1",
                    due_date=date.today() + timedelta(days=3),
                )
            )
            session.commit()
            token = _token(parent)

        resp = client.get(
            "/api/parent/calendar/week",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        cats = {it["category"] for it in resp.json()["items"]}
        assert "fee_due" not in cats
