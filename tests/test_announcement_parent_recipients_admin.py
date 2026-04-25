"""行政後台「對家長發公告」端點測試。

涵蓋：
- GET 讀取空清單
- PUT replace-all：含 all/classroom/student/guardian 四種 scope
- 多次 PUT 替換、空清單清除
- scope 與 id 不一致 → 422
- 不存在的 classroom/student/guardian → 400
- 公告不存在 → 404
- ANNOUNCEMENTS_READ/WRITE 權限隔離
- teacher / parent token → 403
- 端點與既有 announcement_recipients（員工端）相互不干擾
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
from api.announcements import router as announcements_router
from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    AnnouncementRecipient,
    Base,
    Classroom,
    Employee,
    Guardian,
    Student,
    User,
)
from utils.auth import create_access_token, hash_password
from utils.permissions import Permission


@pytest.fixture
def admin_client(tmp_path):
    db_path = tmp_path / "ann-admin.sqlite"
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
    app.include_router(announcements_router)
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed_minimal(session):
    """建立 admin、author、教室、學生、監護人、空白公告。"""
    admin = User(
        username="admin",
        password_hash=hash_password("Pw0rd!aa"),
        role="admin",
        permissions=-1,
        is_active=True,
        token_version=0,
    )
    author = Employee(
        employee_id="ANN-001",
        name="作者",
        base_salary=30000,
        is_active=True,
    )
    classroom = Classroom(name="向日葵", is_active=True)
    session.add_all([admin, author, classroom])
    session.flush()
    student = Student(
        student_id="S001", name="小明", classroom_id=classroom.id, is_active=True
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        student_id=student.id,
        name="父親",
        relation="父親",
        is_primary=True,
    )
    ann = Announcement(title="測試公告", content="正文", created_by=author.id)
    session.add_all([guardian, ann])
    session.flush()
    return admin, ann, classroom, student, guardian


def _admin_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permissions": user.permissions if user.permissions is not None else -1,
            "token_version": user.token_version or 0,
        }
    )


class TestGet:
    def test_initially_empty(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, _, _, _ = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id

        resp = client.get(
            f"/api/announcements/{ann_id}/parent-recipients",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["announcement_id"] == ann_id
        assert data["items"] == []
        assert data["total"] == 0

    def test_announcement_not_found(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, _, _, _, _ = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)

        resp = client.get(
            "/api/announcements/9999/parent-recipients",
            cookies={"access_token": token},
        )
        assert resp.status_code == 404


class TestPutReplaceAll:
    def test_replace_with_all_classroom_student_guardian(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, classroom, student, guardian = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id
            classroom_id = classroom.id
            student_id = student.id
            guardian_id = guardian.id

        body = {
            "recipients": [
                {"scope": "all"},
                {"scope": "classroom", "classroom_id": classroom_id},
                {"scope": "student", "student_id": student_id},
                {"scope": "guardian", "guardian_id": guardian_id},
            ]
        }
        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json=body,
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4
        scopes = sorted(item["scope"] for item in data["items"])
        assert scopes == ["all", "classroom", "guardian", "student"]

    def test_subsequent_put_replaces(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, classroom, _, _ = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id
            classroom_id = classroom.id

        # 第一次：classroom + all
        client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "all"},
                    {"scope": "classroom", "classroom_id": classroom_id},
                ]
            },
            cookies={"access_token": token},
        )
        # 第二次：只剩 classroom
        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "classroom", "classroom_id": classroom_id},
                ]
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["scope"] == "classroom"

    def test_empty_clears(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, classroom, _, _ = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id
            classroom_id = classroom.id

        client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "classroom", "classroom_id": classroom_id},
                ]
            },
            cookies={"access_token": token},
        )
        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={"recipients": []},
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        with session_factory() as session:
            assert session.query(AnnouncementParentRecipient).count() == 0


class TestValidation:
    def test_scope_all_with_extra_id_returns_422(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, classroom, _, _ = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id
            classroom_id = classroom.id

        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "all", "classroom_id": classroom_id},
                ]
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 422

    def test_scope_classroom_without_id_returns_422(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, _, _, _ = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id

        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={"recipients": [{"scope": "classroom"}]},
            cookies={"access_token": token},
        )
        assert resp.status_code == 422

    def test_scope_student_with_wrong_id_field_returns_422(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, classroom, _, _ = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id
            classroom_id = classroom.id

        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "student", "classroom_id": classroom_id}
                ]
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 422

    def test_nonexistent_classroom_returns_400(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, _, _, _ = _seed_minimal(session)
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id

        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "classroom", "classroom_id": 99999}
                ]
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400

    def test_soft_deleted_guardian_returns_400(self, admin_client):
        from datetime import datetime as _dt
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, _, _, guardian = _seed_minimal(session)
            guardian.deleted_at = _dt.now()
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id
            guardian_id = guardian.id

        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "guardian", "guardian_id": guardian_id}
                ]
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 400


class TestPermissionIsolation:
    def test_teacher_cannot_put(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, _, _, _ = _seed_minimal(session)
            employee = Employee(
                employee_id="T01", name="老師", base_salary=30000, is_active=True
            )
            session.add(employee)
            session.flush()
            teacher = User(
                employee_id=employee.id,
                username="teacher_t01",
                password_hash=hash_password("Pw0rd!aa"),
                role="teacher",
                permissions=-1,  # 即使全權限也應因 role=='teacher' 被擋
                is_active=True,
                token_version=0,
            )
            session.add(teacher)
            session.commit()
            ann_id = ann.id
            token = create_access_token(
                {
                    "user_id": teacher.id,
                    "employee_id": teacher.employee_id,
                    "role": "teacher",
                    "name": teacher.username,
                    "permissions": -1,
                    "token_version": 0,
                }
            )

        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={"recipients": []},
            cookies={"access_token": token},
        )
        assert resp.status_code == 403

    def test_parent_cannot_put(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, _, _, _ = _seed_minimal(session)
            parent = User(
                username="parent_line_Uxxx",
                password_hash="!LINE_ONLY",
                role="parent",
                permissions=0,
                is_active=True,
                line_user_id="Uxxx",
                token_version=0,
            )
            session.add(parent)
            session.commit()
            ann_id = ann.id
            token = create_access_token(
                {
                    "user_id": parent.id,
                    "employee_id": None,
                    "role": "parent",
                    "name": parent.username,
                    "permissions": 0,
                    "token_version": 0,
                }
            )

        resp = client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={"recipients": []},
            cookies={"access_token": token},
        )
        assert resp.status_code == 403


class TestEmployeeRecipientsUnaffected:
    """員工端 announcement_recipients 不受家長端 PUT 影響。"""

    def test_employee_recipients_not_touched(self, admin_client):
        client, session_factory = admin_client
        with session_factory() as session:
            admin, ann, classroom, _, _ = _seed_minimal(session)
            employee = Employee(
                employee_id="EMP01", name="收件員工", base_salary=30000, is_active=True
            )
            session.add(employee)
            session.flush()
            session.add(
                AnnouncementRecipient(announcement_id=ann.id, employee_id=employee.id)
            )
            session.commit()
            token = _admin_token(admin)
            ann_id = ann.id
            classroom_id = classroom.id
            employee_id = employee.id

        client.put(
            f"/api/announcements/{ann_id}/parent-recipients",
            json={
                "recipients": [
                    {"scope": "classroom", "classroom_id": classroom_id}
                ]
            },
            cookies={"access_token": token},
        )
        with session_factory() as session:
            emp_recipients = (
                session.query(AnnouncementRecipient)
                .filter(AnnouncementRecipient.announcement_id == ann_id)
                .all()
            )
            assert len(emp_recipients) == 1
            assert emp_recipients[0].employee_id == employee_id
