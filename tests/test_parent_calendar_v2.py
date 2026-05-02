"""家長端行事曆 v3.1 擴充測試（Phase 3）。

涵蓋：
- contact_book kind 出現在 weekly agenda
- leave date-range 展開到區間內每一天
- medication appears on order_date
- student_id 篩選只回該學生的學生層級項目（公告/事件不過濾）
- /month 端點返回該月區間
"""

from __future__ import annotations

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
    Base,
    Classroom,
    Guardian,
    Student,
    StudentContactBookEntry,
    StudentLeaveRequest,
    StudentMedicationOrder,
    User,
)
from utils.auth import create_access_token


@pytest.fixture
def parent_client(tmp_path):
    db_path = tmp_path / "calendar-v2.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(db_engine)

    app = FastAPI()
    app.include_router(parent_router)
    with TestClient(app) as client:
        yield client, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    db_engine.dispose()


def _setup(s):
    classroom = Classroom(name="向日葵班", is_active=True)
    s.add(classroom)
    s.flush()
    parent = User(
        username="p1",
        password_hash="!",
        role="parent",
        permissions=0,
        is_active=True,
        token_version=0,
    )
    s.add(parent)
    s.flush()
    child_a = Student(
        student_id="ST_A", name="小明", classroom_id=classroom.id, is_active=True
    )
    child_b = Student(
        student_id="ST_B", name="小華", classroom_id=classroom.id, is_active=True
    )
    s.add_all([child_a, child_b])
    s.flush()
    s.add_all(
        [
            Guardian(
                student_id=child_a.id,
                user_id=parent.id,
                name="家長",
                relation="父親",
                is_primary=True,
                can_pickup=True,
            ),
            Guardian(
                student_id=child_b.id,
                user_id=parent.id,
                name="家長",
                relation="父親",
                is_primary=False,
                can_pickup=True,
            ),
        ]
    )
    s.flush()
    return classroom, parent, child_a, child_b


def _token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permissions": 0,
            "token_version": 0,
        }
    )


class TestContactBookKind:
    def test_contact_book_appears_in_week(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            classroom, parent, child_a, _ = _setup(s)
            entry = StudentContactBookEntry(
                student_id=child_a.id,
                classroom_id=classroom.id,
                log_date=date.today(),
                teacher_note="今天玩得很開心",
                published_at=datetime.now(),
            )
            s.add(entry)
            s.commit()
            tok = _token(parent)

        r = client.get(
            "/api/parent/calendar/week",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        kinds = {it["kind"] for it in r.json()["items"]}
        assert "contact_book" in kinds


class TestLeaveExpansion:
    def test_leave_date_range_expansion(self, parent_client):
        client, sf = parent_client
        today = date.today()
        with sf() as s:
            classroom, parent, child_a, _ = _setup(s)
            lv = StudentLeaveRequest(
                student_id=child_a.id,
                applicant_user_id=parent.id,
                leave_type="sick",
                start_date=today,
                end_date=today + timedelta(days=2),
                status="approved",
                reason="感冒",
            )
            s.add(lv)
            s.commit()
            tok = _token(parent)

        r = client.get(
            "/api/parent/calendar/week?days=7",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        leave_items = [it for it in r.json()["items"] if it["kind"] == "leave"]
        # 應展開為 3 天（today, +1, +2）
        assert len(leave_items) == 3
        dates = sorted({it["date"] for it in leave_items})
        assert dates == [
            today.isoformat(),
            (today + timedelta(days=1)).isoformat(),
            (today + timedelta(days=2)).isoformat(),
        ]


class TestMedicationKind:
    def test_medication_appears_on_order_date(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            classroom, parent, child_a, _ = _setup(s)
            order = StudentMedicationOrder(
                student_id=child_a.id,
                order_date=date.today(),
                medication_name="退燒藥",
                dose="5ml",
                time_slots=["08:00"],
                source="parent",
                created_by=parent.id,
            )
            s.add(order)
            s.commit()
            tok = _token(parent)

        r = client.get(
            "/api/parent/calendar/week",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        med = [it for it in r.json()["items"] if it["kind"] == "medication"]
        assert len(med) == 1
        assert med[0]["subtitle"] == "退燒藥"


class TestStudentIdFilter:
    def test_student_id_filter_isolates_student_level_items(self, parent_client):
        client, sf = parent_client
        today = date.today()
        with sf() as s:
            classroom, parent, child_a, child_b = _setup(s)
            # 兩位子女各一筆聯絡簿
            s.add_all(
                [
                    StudentContactBookEntry(
                        student_id=child_a.id,
                        classroom_id=classroom.id,
                        log_date=today,
                        teacher_note="A 紀錄",
                        published_at=datetime.now(),
                    ),
                    StudentContactBookEntry(
                        student_id=child_b.id,
                        classroom_id=classroom.id,
                        log_date=today,
                        teacher_note="B 紀錄",
                        published_at=datetime.now(),
                    ),
                ]
            )
            s.commit()
            tok = _token(parent)
            child_a_id = child_a.id

        r = client.get(
            f"/api/parent/calendar/week?student_id={child_a_id}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        cb_items = [it for it in r.json()["items"] if it["kind"] == "contact_book"]
        assert len(cb_items) == 1
        assert cb_items[0]["target_id"] is not None


class TestStudentIdIDOR:
    def test_student_id_not_owned_returns_403(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            _classroom, parent, _child_a, _child_b = _setup(s)
            other_student = Student(student_id="ST_X", name="其他人", is_active=True)
            s.add(other_student)
            s.commit()
            other_id = other_student.id
            tok = _token(parent)

        r = client.get(
            f"/api/parent/calendar/week?student_id={other_id}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 403


class TestMonthEndpoint:
    def test_month_returns_period(self, parent_client):
        client, sf = parent_client
        with sf() as s:
            _classroom, parent, _child_a, _child_b = _setup(s)
            s.commit()
            tok = _token(parent)

        today = date.today()
        r = client.get(
            f"/api/parent/calendar/month?year={today.year}&month={today.month}",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["year"] == today.year
        assert body["month"] == today.month
        assert body["from"].startswith(f"{today.year}-{today.month:02d}-01")
