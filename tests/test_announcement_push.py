"""公告 LINE 推播 scope 解析（Phase 4）。

驗證 _resolve_parent_user_ids 對四種 scope（all / classroom / student / guardian）
皆正確展開到家長 user_id 集合，並驗證 _fire_announcement_push 對每個 user 走
should_push_to_parent gate。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.announcements import (
    _fire_announcement_push,
    _resolve_parent_user_ids,
    init_announcement_line_service,
)
from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    Base,
    Classroom,
    Employee,
    Guardian,
    Student,
    User,
)


def _make_announcement(session, **kwargs):
    """建 Announcement（含必需的 employee author）。"""
    emp = Employee(
        employee_id=f"EMP_{kwargs.get('title', 'X')}",
        name="作者",
        is_active=True,
        base_salary=30000,
    )
    session.add(emp)
    session.flush()
    ann = Announcement(
        title=kwargs.get("title", "公告"),
        content=kwargs.get("content", "x"),
        created_by=emp.id,
    )
    session.add(ann)
    session.flush()
    return ann


@pytest.fixture
def session_factory(tmp_path):
    db_path = tmp_path / "ann-push.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=db_engine)

    old = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(db_engine)
    yield sf
    base_module._engine = old
    base_module._SessionFactory = old_sf
    db_engine.dispose()


def _seed_parent_with_child(
    session,
    *,
    line_id: str,
    student_name: str,
    classroom_name: str,
    follow_confirmed: bool = True,
):
    user = User(
        username=f"p_{line_id}",
        password_hash="!",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_id,
        line_follow_confirmed_at=datetime.now() if follow_confirmed else None,
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
        name="家長",
        relation="父親",
        is_primary=True,
    )
    session.add(guardian)
    session.flush()
    return user, student, classroom, guardian


class TestResolveParentUserIds:
    def test_all_scope_includes_all_active_parents(self, session_factory):
        """'all' scope 不預先做 LINE 可達性過濾 — gate 統一在 push 時擋。"""
        sf = session_factory
        with sf() as session:
            ua, _, _, _ = _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            ub, _, _, _ = _seed_parent_with_child(
                session,
                line_id="UB",
                student_name="B1",
                classroom_name="B班",
                follow_confirmed=False,
            )
            session.commit()
            recipients = [AnnouncementParentRecipient(announcement_id=1, scope="all")]
            uids = _resolve_parent_user_ids(session, recipients)
            assert {ua.id, ub.id}.issubset(uids)

    def test_classroom_scope_only_that_class(self, session_factory):
        sf = session_factory
        with sf() as session:
            ua, _, ca, _ = _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            ub, _, _, _ = _seed_parent_with_child(
                session, line_id="UB", student_name="B1", classroom_name="B班"
            )
            session.commit()
            recipients = [
                AnnouncementParentRecipient(
                    announcement_id=1, scope="classroom", classroom_id=ca.id
                )
            ]
            uids = _resolve_parent_user_ids(session, recipients)
            assert uids == {ua.id}

    def test_student_scope_only_that_student(self, session_factory):
        sf = session_factory
        with sf() as session:
            ua, sa, _, _ = _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            _seed_parent_with_child(
                session, line_id="UB", student_name="B1", classroom_name="B班"
            )
            session.commit()
            recipients = [
                AnnouncementParentRecipient(
                    announcement_id=1, scope="student", student_id=sa.id
                )
            ]
            uids = _resolve_parent_user_ids(session, recipients)
            assert uids == {ua.id}

    def test_guardian_scope_only_that_guardian(self, session_factory):
        sf = session_factory
        with sf() as session:
            ua, _, _, ga = _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            _seed_parent_with_child(
                session, line_id="UB", student_name="B1", classroom_name="B班"
            )
            session.commit()
            recipients = [
                AnnouncementParentRecipient(
                    announcement_id=1, scope="guardian", guardian_id=ga.id
                )
            ]
            uids = _resolve_parent_user_ids(session, recipients)
            assert uids == {ua.id}

    def test_multiple_scopes_union(self, session_factory):
        sf = session_factory
        with sf() as session:
            ua, _, ca, _ = _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            ub, sb, _, _ = _seed_parent_with_child(
                session, line_id="UB", student_name="B1", classroom_name="B班"
            )
            session.commit()
            recipients = [
                AnnouncementParentRecipient(
                    announcement_id=1, scope="classroom", classroom_id=ca.id
                ),
                AnnouncementParentRecipient(
                    announcement_id=1, scope="student", student_id=sb.id
                ),
            ]
            uids = _resolve_parent_user_ids(session, recipients)
            assert uids == {ua.id, ub.id}

    def test_classroom_scope_excludes_deleted_guardian(self, session_factory):
        sf = session_factory
        with sf() as session:
            ua, _, ca, _ = _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            # 軟刪除 A 的 guardian
            g = session.query(Guardian).filter(Guardian.user_id == ua.id).first()
            g.deleted_at = datetime.now()
            session.commit()
            recipients = [
                AnnouncementParentRecipient(
                    announcement_id=1, scope="classroom", classroom_id=ca.id
                )
            ]
            uids = _resolve_parent_user_ids(session, recipients)
            assert uids == set()


class TestFireAnnouncementPush:
    def test_each_user_goes_through_gate(self, session_factory):
        sf = session_factory
        with sf() as session:
            ua, sa, ca, _ = _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            ann = _make_announcement(session, title="重要公告", content="今日停課")
            recipients = [
                AnnouncementParentRecipient(
                    announcement_id=ann.id, scope="classroom", classroom_id=ca.id
                )
            ]
            session.commit()

            mock_svc = MagicMock()
            mock_svc.should_push_to_parent.return_value = "U_OK"
            init_announcement_line_service(mock_svc)
            try:
                _fire_announcement_push(session, ann, recipients)
            finally:
                init_announcement_line_service(None)

            mock_svc.should_push_to_parent.assert_called_once_with(
                session, user_id=ua.id, event_type="announcement"
            )
            mock_svc.notify_parent_announcement.assert_called_once()

    def test_blocked_gate_skips_push(self, session_factory):
        sf = session_factory
        with sf() as session:
            ua, _, ca, _ = _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            ann = _make_announcement(session, title="公告", content="x")
            recipients = [
                AnnouncementParentRecipient(
                    announcement_id=ann.id, scope="classroom", classroom_id=ca.id
                )
            ]
            session.commit()

            mock_svc = MagicMock()
            mock_svc.should_push_to_parent.return_value = None  # gate blocks
            init_announcement_line_service(mock_svc)
            try:
                _fire_announcement_push(session, ann, recipients)
            finally:
                init_announcement_line_service(None)

            mock_svc.should_push_to_parent.assert_called_once()
            mock_svc.notify_parent_announcement.assert_not_called()

    def test_no_line_service_is_noop(self, session_factory):
        sf = session_factory
        with sf() as session:
            _seed_parent_with_child(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            ann = _make_announcement(session, title="公告", content="x")
            session.commit()
            # _line_service 為 None：應靜默
            init_announcement_line_service(None)
            _fire_announcement_push(session, ann, [])  # 不應拋
