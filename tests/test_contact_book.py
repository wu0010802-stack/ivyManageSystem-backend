"""每日聯絡簿（家長入口 v3.1 Phase 1）整合測試。

涵蓋：
- 教師端：batch upsert 一學生一筆、樂觀鎖 409、publish 觸發 LINE push、未發布家長看不到
- 家長端：已讀 idempotent、回覆字數上限、IDOR（非自己子女不可見）
"""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router
from api.portal import router as portal_router
from api.portal.contact_book import init_contact_book_line_service
from models.database import (
    Base,
    Classroom,
    Employee,
    Guardian,
    Student,
    StudentContactBookAck,
    StudentContactBookEntry,
    StudentContactBookReply,
    User,
)
from utils.auth import create_access_token
from utils.permissions import Permission


@pytest.fixture
def app_clients(tmp_path):
    db_path = tmp_path / "contact-book.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    # mock LINE service for assertions
    line_service = MagicMock()
    line_service.should_push_to_parent.return_value = "U_LINE_USER"
    init_contact_book_line_service(line_service)

    app = FastAPI()
    app.include_router(parent_router)
    app.include_router(portal_router)
    with TestClient(app) as client:
        yield client, session_factory, line_service

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()
    init_contact_book_line_service(None)


# ── 工具：建立 fixture 資料 ─────────────────────────────────────────────────


def _make_teacher(session, classroom_id: int) -> tuple[Employee, User]:
    emp = Employee(employee_id="E001", name="王老師", is_active=True)
    session.add(emp)
    session.flush()
    user = User(
        username="teacher1",
        password_hash="!hash",
        role="teacher",
        employee_id=emp.id,
        permissions=int(
            Permission.PORTFOLIO_READ.value | Permission.PORTFOLIO_WRITE.value
        ),
        is_active=True,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return emp, user


def _make_parent(session, username: str = "parent1") -> User:
    u = User(
        username=username,
        password_hash="!LINE",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=f"U_{username}",
        token_version=0,
    )
    session.add(u)
    session.flush()
    return u


def _make_classroom(session, name="向日葵班") -> Classroom:
    c = Classroom(name=name, is_active=True)
    session.add(c)
    session.flush()
    return c


def _add_child(session, parent: User, classroom: Classroom, name: str) -> Student:
    s = Student(
        student_id=f"ST_{name}",
        name=name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(s)
    session.flush()
    session.add(
        Guardian(
            student_id=s.id,
            user_id=parent.id,
            name="家長",
            relation="父親",
            is_primary=True,
            can_pickup=True,
        )
    )
    session.flush()
    return s


def _set_classroom_teacher(session, classroom: Classroom, emp: Employee) -> None:
    classroom.head_teacher_id = emp.id
    session.flush()


def _teacher_token(user: User, emp: Employee) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": emp.id,
            "role": "teacher",
            "name": user.username,
            "permissions": user.permissions,
            "token_version": user.token_version or 0,
        }
    )


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


# ─────────────────────────────────────────────────────────────────────────
# 教師端測試
# ─────────────────────────────────────────────────────────────────────────


class TestTeacherBatch:
    def test_batch_upsert_creates_one_per_student(self, app_clients):
        client, sf, _ = app_clients
        with sf() as s:
            classroom = _make_classroom(s)
            emp, user = _make_teacher(s, classroom.id)
            _set_classroom_teacher(s, classroom, emp)
            parent = _make_parent(s)
            child_a = _add_child(s, parent, classroom, "小明")
            child_b = _add_child(s, parent, classroom, "小華")
            s.commit()
            classroom_id = classroom.id
            ids = [child_a.id, child_b.id]
            token = _teacher_token(user, emp)

        resp = client.post(
            "/api/portal/contact-book/batch",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "classroom_id": classroom_id,
                "log_date": "2026-05-02",
                "items": [
                    {
                        "student_id": ids[0],
                        "mood": "happy",
                        "teacher_note": "今天表現很好",
                    },
                    {"student_id": ids[1], "mood": "tired", "nap_minutes": 80},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["entry_ids"]) == 2

        # 每位學生只有一筆
        with sf() as s:
            rows = (
                s.query(StudentContactBookEntry)
                .filter(StudentContactBookEntry.classroom_id == classroom_id)
                .all()
            )
            assert len(rows) == 2
            student_ids = {r.student_id for r in rows}
            assert student_ids == set(ids)

        # 第二次 upsert 應更新而非建第二筆
        resp2 = client.post(
            "/api/portal/contact-book/batch",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "classroom_id": classroom_id,
                "log_date": "2026-05-02",
                "items": [
                    {
                        "student_id": ids[0],
                        "mood": "normal",
                        "teacher_note": "修正一下",
                    },
                ],
            },
        )
        assert resp2.status_code == 200
        with sf() as s:
            row = (
                s.query(StudentContactBookEntry)
                .filter(StudentContactBookEntry.student_id == ids[0])
                .one()
            )
            assert row.mood == "normal"
            assert row.teacher_note == "修正一下"


class TestOptimisticLock:
    def test_optimistic_lock_returns_409(self, app_clients):
        client, sf, _ = app_clients
        with sf() as s:
            classroom = _make_classroom(s)
            emp, user = _make_teacher(s, classroom.id)
            _set_classroom_teacher(s, classroom, emp)
            parent = _make_parent(s)
            child = _add_child(s, parent, classroom, "小明")
            entry = StudentContactBookEntry(
                student_id=child.id,
                classroom_id=classroom.id,
                log_date=date(2026, 5, 2),
                mood="happy",
                version=3,
                created_by_employee_id=emp.id,
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id
            token = _teacher_token(user, emp)

        # If-Match 帶錯版本應 409
        resp = client.put(
            f"/api/portal/contact-book/{entry_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "If-Match": '"1"',
            },
            json={"mood": "tired"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "VERSION_CONFLICT"

        # 帶對的版本應成功，且 version 累加
        resp_ok = client.put(
            f"/api/portal/contact-book/{entry_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "If-Match": '"3"',
            },
            json={"mood": "tired", "teacher_note": "update"},
        )
        assert resp_ok.status_code == 200
        body = resp_ok.json()
        assert body["mood"] == "tired"
        assert body["version"] == 4


class TestPublishLineNotification:
    def test_publish_triggers_line_push(self, app_clients):
        client, sf, line_service = app_clients
        with sf() as s:
            classroom = _make_classroom(s)
            emp, user = _make_teacher(s, classroom.id)
            _set_classroom_teacher(s, classroom, emp)
            parent = _make_parent(s)
            child = _add_child(s, parent, classroom, "小明")
            entry = StudentContactBookEntry(
                student_id=child.id,
                classroom_id=classroom.id,
                log_date=date(2026, 5, 2),
                mood="happy",
                teacher_note="今天玩得很開心",
                created_by_employee_id=emp.id,
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id
            token = _teacher_token(user, emp)

        resp = client.post(
            f"/api/portal/contact-book/{entry_id}/publish",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["published_at"] is not None
        # publish_entry 內 version+1
        assert body["version"] >= 2

        # LINE service should_push_to_parent + notify 都被呼叫
        line_service.should_push_to_parent.assert_called()
        line_service.notify_parent_contact_book_published.assert_called()


# ─────────────────────────────────────────────────────────────────────────
# 家長端測試
# ─────────────────────────────────────────────────────────────────────────


class TestParentVisibility:
    def test_unpublished_hidden_from_parent(self, app_clients):
        client, sf, _ = app_clients
        with sf() as s:
            classroom = _make_classroom(s)
            emp, _user = _make_teacher(s, classroom.id)
            _set_classroom_teacher(s, classroom, emp)
            parent = _make_parent(s)
            child = _add_child(s, parent, classroom, "小明")
            entry = StudentContactBookEntry(
                student_id=child.id,
                classroom_id=classroom.id,
                log_date=date.today(),
                teacher_note="草稿",
                published_at=None,
                created_by_employee_id=emp.id,
            )
            s.add(entry)
            s.commit()
            child_id = child.id
            entry_id = entry.id
            ptoken = _parent_token(parent)

        # today 端點：草稿不返回
        r = client.get(
            f"/api/parent/contact-book/today?student_id={child_id}",
            headers={"Authorization": f"Bearer {ptoken}"},
        )
        assert r.status_code == 200
        assert r.json()["entry"] is None

        # detail 端點：草稿一律 404（避免 enumeration）
        r2 = client.get(
            f"/api/parent/contact-book/{entry_id}",
            headers={"Authorization": f"Bearer {ptoken}"},
        )
        assert r2.status_code == 404


class TestParentAck:
    def test_ack_idempotent(self, app_clients):
        client, sf, _ = app_clients
        from datetime import datetime as _dt

        with sf() as s:
            classroom = _make_classroom(s)
            emp, _user = _make_teacher(s, classroom.id)
            _set_classroom_teacher(s, classroom, emp)
            parent = _make_parent(s)
            child = _add_child(s, parent, classroom, "小明")
            entry = StudentContactBookEntry(
                student_id=child.id,
                classroom_id=classroom.id,
                log_date=date.today(),
                teacher_note="今天好棒",
                published_at=_dt.now(),
                created_by_employee_id=emp.id,
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id
            ptoken = _parent_token(parent)
            parent_id = parent.id

        r1 = client.post(
            f"/api/parent/contact-book/{entry_id}/ack",
            headers={"Authorization": f"Bearer {ptoken}"},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["already_marked"] is False

        r2 = client.post(
            f"/api/parent/contact-book/{entry_id}/ack",
            headers={"Authorization": f"Bearer {ptoken}"},
        )
        assert r2.status_code == 200
        assert r2.json()["already_marked"] is True

        # DB 內只有一筆 ack
        with sf() as s:
            count = (
                s.query(StudentContactBookAck)
                .filter(
                    StudentContactBookAck.entry_id == entry_id,
                    StudentContactBookAck.guardian_user_id == parent_id,
                )
                .count()
            )
            assert count == 1


class TestParentReply:
    def test_reply_500_char_limit(self, app_clients):
        client, sf, _ = app_clients
        from datetime import datetime as _dt

        with sf() as s:
            classroom = _make_classroom(s)
            emp, _user = _make_teacher(s, classroom.id)
            _set_classroom_teacher(s, classroom, emp)
            parent = _make_parent(s)
            child = _add_child(s, parent, classroom, "小明")
            entry = StudentContactBookEntry(
                student_id=child.id,
                classroom_id=classroom.id,
                log_date=date.today(),
                published_at=_dt.now(),
                created_by_employee_id=emp.id,
            )
            s.add(entry)
            s.commit()
            entry_id = entry.id
            ptoken = _parent_token(parent)

        # 501 字 → 422
        too_long = "x" * 501
        r_bad = client.post(
            f"/api/parent/contact-book/{entry_id}/reply",
            headers={"Authorization": f"Bearer {ptoken}"},
            json={"body": too_long},
        )
        assert r_bad.status_code == 422

        # 500 字 → 201
        r_ok = client.post(
            f"/api/parent/contact-book/{entry_id}/reply",
            headers={"Authorization": f"Bearer {ptoken}"},
            json={"body": "感謝老師"},
        )
        assert r_ok.status_code == 201
        assert r_ok.json()["body"] == "感謝老師"

        # detail 應看得到 reply
        r_detail = client.get(
            f"/api/parent/contact-book/{entry_id}",
            headers={"Authorization": f"Bearer {ptoken}"},
        )
        assert r_detail.status_code == 200
        assert len(r_detail.json()["replies"]) == 1


class TestParentIDOR:
    def test_only_own_child_visible_idor(self, app_clients):
        client, sf, _ = app_clients
        from datetime import datetime as _dt

        with sf() as s:
            classroom = _make_classroom(s)
            emp, _user = _make_teacher(s, classroom.id)
            _set_classroom_teacher(s, classroom, emp)
            parent_a = _make_parent(s, username="pa")
            parent_b = _make_parent(s, username="pb")
            child_a = _add_child(s, parent_a, classroom, "小明")
            child_b = _add_child(s, parent_b, classroom, "小華")
            entry_b = StudentContactBookEntry(
                student_id=child_b.id,
                classroom_id=classroom.id,
                log_date=date.today(),
                published_at=_dt.now(),
                created_by_employee_id=emp.id,
            )
            s.add(entry_b)
            s.commit()
            entry_b_id = entry_b.id
            child_b_id = child_b.id
            token_a = _parent_token(parent_a)

        # parent_a 不可看 child_b 的 today
        r = client.get(
            f"/api/parent/contact-book/today?student_id={child_b_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert r.status_code == 403

        # parent_a 不可看 child_b 的 entry detail
        r2 = client.get(
            f"/api/parent/contact-book/{entry_b_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert r2.status_code == 403

        # parent_a 不可 ack child_b 的 entry
        r3 = client.post(
            f"/api/parent/contact-book/{entry_b_id}/ack",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert r3.status_code == 403
