"""家園溝通平台 — 訊息核心（Phase 3）。"""

import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router
from api.portal import router as portal_router
from models.database import (
    Base,
    Classroom,
    Employee,
    Guardian,
    ParentMessage,
    ParentMessageThread,
    Student,
    User,
)
from utils.auth import create_access_token
from utils.permissions import Permission


@pytest.fixture
def msg_client(tmp_path):
    db_path = tmp_path / "parent-msg.sqlite"
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
    app.include_router(parent_router)
    app.include_router(portal_router)
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed(session, *, line_id="UA", student_name="A1", classroom_name="A班"):
    """建立 (parent_user, student, classroom, head_teacher_user, head_teacher_emp)。"""
    parent = User(
        username=f"p_{line_id}",
        password_hash="!",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=line_id,
        token_version=0,
    )
    session.add(parent)
    session.flush()

    head_emp = Employee(
        employee_id=f"E_{line_id}",
        name=f"{classroom_name}班導",
        is_active=True,
        base_salary=30000,
    )
    session.add(head_emp)
    session.flush()
    teacher = User(
        username=f"t_{line_id}",
        password_hash="!",
        role="teacher",
        permissions=int(Permission.PARENT_MESSAGES_WRITE.value),
        is_active=True,
        token_version=0,
    )
    session.add(teacher)
    session.flush()

    classroom = Classroom(
        name=classroom_name,
        is_active=True,
        head_teacher_id=head_emp.id,
    )
    session.add(classroom)
    session.flush()

    student = Student(
        student_id=f"ST_{student_name}",
        name=student_name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    session.add(
        Guardian(
            student_id=student.id,
            user_id=parent.id,
            name="家長",
            relation="父親",
            is_primary=True,
        )
    )
    session.flush()
    return parent, teacher, head_emp, student, classroom


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


def _teacher_token(user: User, employee_id: int) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": employee_id,
            "role": "teacher",
            "name": user.username,
            "permissions": int(Permission.PARENT_MESSAGES_WRITE.value),
            "token_version": user.token_version or 0,
        }
    )


# ════════════════════════════════════════════════════════════════════════
# 教師發起 thread（班導師守衛）
# ════════════════════════════════════════════════════════════════════════


class TestTeacherCreateThread:
    def test_homeroom_teacher_can_create_thread(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            tk = _teacher_token(teacher, emp.id)
            sid, pid = student.id, parent.id

        resp = client.post(
            "/api/portal/parent-messages/threads",
            json={
                "student_id": sid,
                "parent_user_id": pid,
                "body": "請家長注意明日請帶外套",
            },
            cookies={"access_token": tk},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["thread"]["student_id"] == sid
        assert data["message"]["body"] == "請家長注意明日請帶外套"
        assert data["message"]["sender_role"] == "teacher"
        assert data["idempotent_replay"] is False

    def test_non_homeroom_teacher_cannot_create(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            # 另一個 teacher，不是 head_teacher
            other_emp = Employee(
                employee_id="E_OTHER", name="其他", is_active=True, base_salary=30000
            )
            session.add(other_emp)
            session.flush()
            other_user = User(
                username="t_other",
                password_hash="!",
                role="teacher",
                permissions=int(Permission.PARENT_MESSAGES_WRITE.value),
                is_active=True,
                token_version=0,
            )
            session.add(other_user)
            session.flush()
            session.commit()
            tk = _teacher_token(other_user, other_emp.id)
            sid, pid = student.id, parent.id

        resp = client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "Hi"},
            cookies={"access_token": tk},
        )
        assert resp.status_code == 403

    def test_parent_user_must_be_guardian_of_student(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            _, teacher, emp, student, _ = _seed(session)
            unrelated = User(
                username="random",
                password_hash="!",
                role="parent",
                permissions=0,
                is_active=True,
                token_version=0,
            )
            session.add(unrelated)
            session.flush()
            session.commit()
            tk = _teacher_token(teacher, emp.id)
            sid, pid = student.id, unrelated.id

        resp = client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "Hi"},
            cookies={"access_token": tk},
        )
        assert resp.status_code == 400


# ════════════════════════════════════════════════════════════════════════
# 家長 reply + IDOR
# ════════════════════════════════════════════════════════════════════════


class TestParentReply:
    def _setup_thread(self, client, sf):
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_tk = _parent_token(parent)
            sid, pid = student.id, parent.id
        client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "首則"},
            cookies={"access_token": t_tk},
        )
        # 撈 thread id
        with sf() as session:
            t = session.query(ParentMessageThread).first()
            return p_tk, t_tk, t.id

    def test_parent_can_reply(self, msg_client):
        client, sf = msg_client
        p_tk, _, thread_id = self._setup_thread(client, sf)
        resp = client.post(
            f"/api/parent/messages/threads/{thread_id}/messages",
            json={"body": "知道了"},
            cookies={"access_token": p_tk},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["sender_role"] == "parent"
        assert resp.json()["body"] == "知道了"

    def test_other_parent_cannot_see_thread(self, msg_client):
        client, sf = msg_client
        _, _, thread_id = self._setup_thread(client, sf)
        with sf() as session:
            stranger = User(
                username="stranger",
                password_hash="!",
                role="parent",
                permissions=0,
                is_active=True,
                token_version=0,
            )
            session.add(stranger)
            session.commit()
            stranger_tk = _parent_token(stranger)
        resp = client.get(
            f"/api/parent/messages/threads/{thread_id}",
            cookies={"access_token": stranger_tk},
        )
        assert resp.status_code == 403

    def test_idempotent_replay(self, msg_client):
        client, sf = msg_client
        p_tk, _, thread_id = self._setup_thread(client, sf)
        body = {"body": "回覆 A", "client_request_id": "abc12345xyz"}
        r1 = client.post(
            f"/api/parent/messages/threads/{thread_id}/messages",
            json=body,
            cookies={"access_token": p_tk},
        )
        r2 = client.post(
            f"/api/parent/messages/threads/{thread_id}/messages",
            json=body,
            cookies={"access_token": p_tk},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]
        assert r2.json()["idempotent_replay"] is True


# ════════════════════════════════════════════════════════════════════════
# 撤回（30 分內）
# ════════════════════════════════════════════════════════════════════════


class TestRecall:
    def test_sender_can_recall_within_window(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            sid, pid = student.id, parent.id
        rsp = client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "錯字訊息"},
            cookies={"access_token": t_tk},
        )
        msg_id = rsp.json()["message"]["id"]
        rec = client.post(
            f"/api/portal/parent-messages/messages/{msg_id}/recall",
            cookies={"access_token": t_tk},
        )
        assert rec.status_code == 200

        with sf() as session:
            m = session.query(ParentMessage).filter(ParentMessage.id == msg_id).first()
            assert m.deleted_at is not None

    def test_recall_after_window_fails(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            sid, pid = student.id, parent.id
        rsp = client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "舊訊息"},
            cookies={"access_token": t_tk},
        )
        msg_id = rsp.json()["message"]["id"]

        # 強制把 created_at 推到 31 分鐘前
        with sf() as session:
            m = session.query(ParentMessage).filter(ParentMessage.id == msg_id).first()
            m.created_at = datetime.now() - timedelta(minutes=31)
            session.commit()

        rec = client.post(
            f"/api/portal/parent-messages/messages/{msg_id}/recall",
            cookies={"access_token": t_tk},
        )
        assert rec.status_code == 403

    def test_non_sender_cannot_recall(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_tk = _parent_token(parent)
            sid, pid = student.id, parent.id
        rsp = client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "教師發"},
            cookies={"access_token": t_tk},
        )
        msg_id = rsp.json()["message"]["id"]
        # 家長嘗試撤回教師訊息
        rec = client.post(
            f"/api/parent/messages/messages/{msg_id}/recall",
            cookies={"access_token": p_tk},
        )
        assert rec.status_code == 403


# ════════════════════════════════════════════════════════════════════════
# 已讀 / 未讀
# ════════════════════════════════════════════════════════════════════════


class TestUnreadCount:
    def test_unread_starts_at_one_after_teacher_msg(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_tk = _parent_token(parent)
            sid, pid = student.id, parent.id
        client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "通知"},
            cookies={"access_token": t_tk},
        )
        n = client.get(
            "/api/parent/messages/unread-count",
            cookies={"access_token": p_tk},
        ).json()["unread_count"]
        assert n == 1

    def test_mark_read_zeros_unread(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_tk = _parent_token(parent)
            sid, pid = student.id, parent.id
        client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "通知"},
            cookies={"access_token": t_tk},
        )
        with sf() as session:
            tid = session.query(ParentMessageThread).first().id
        client.post(
            f"/api/parent/messages/threads/{tid}/read",
            cookies={"access_token": p_tk},
        )
        n = client.get(
            "/api/parent/messages/unread-count",
            cookies={"access_token": p_tk},
        ).json()["unread_count"]
        assert n == 0

    def test_parent_reply_does_not_count_for_parent(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_tk = _parent_token(parent)
            sid, pid = student.id, parent.id
        client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "教師訊息"},
            cookies={"access_token": t_tk},
        )
        with sf() as session:
            tid = session.query(ParentMessageThread).first().id
        # 家長 mark read，再回覆，再查 — 應仍是 0（因為自己 reply 不算未讀）
        client.post(
            f"/api/parent/messages/threads/{tid}/read",
            cookies={"access_token": p_tk},
        )
        client.post(
            f"/api/parent/messages/threads/{tid}/messages",
            json={"body": "我看到了"},
            cookies={"access_token": p_tk},
        )
        n = client.get(
            "/api/parent/messages/unread-count",
            cookies={"access_token": p_tk},
        ).json()["unread_count"]
        assert n == 0


class TestTeacherUnreadCount:
    """教師端跨 thread 未讀總數（家長 → 教師方向）。"""

    def test_teacher_unread_zero_when_no_thread(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            _, teacher, emp, _, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
        n = client.get(
            "/api/portal/parent-messages/unread-count",
            cookies={"access_token": t_tk},
        ).json()["unread_count"]
        assert n == 0

    def test_teacher_unread_increments_on_parent_reply(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_tk = _parent_token(parent)
            sid, pid = student.id, parent.id
        # 教師發起 thread
        client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "通知"},
            cookies={"access_token": t_tk},
        )
        with sf() as session:
            tid = session.query(ParentMessageThread).first().id
        # 家長回覆兩則
        client.post(
            f"/api/parent/messages/threads/{tid}/messages",
            json={"body": "好"},
            cookies={"access_token": p_tk},
        )
        client.post(
            f"/api/parent/messages/threads/{tid}/messages",
            json={"body": "謝謝"},
            cookies={"access_token": p_tk},
        )
        # 教師查詢未讀
        n = client.get(
            "/api/portal/parent-messages/unread-count",
            cookies={"access_token": t_tk},
        ).json()["unread_count"]
        assert n == 2

    def test_teacher_mark_read_zeros_unread(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_tk = _parent_token(parent)
            sid, pid = student.id, parent.id
        client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "通知"},
            cookies={"access_token": t_tk},
        )
        with sf() as session:
            tid = session.query(ParentMessageThread).first().id
        client.post(
            f"/api/parent/messages/threads/{tid}/messages",
            json={"body": "回覆"},
            cookies={"access_token": p_tk},
        )
        # 教師標已讀
        client.post(
            f"/api/portal/parent-messages/threads/{tid}/read",
            cookies={"access_token": t_tk},
        )
        n = client.get(
            "/api/portal/parent-messages/unread-count",
            cookies={"access_token": t_tk},
        ).json()["unread_count"]
        assert n == 0

    def test_teacher_own_messages_dont_count(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            sid, pid = student.id, parent.id
        # 教師連發 3 則
        client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "1"},
            cookies={"access_token": t_tk},
        )
        with sf() as session:
            tid = session.query(ParentMessageThread).first().id
        client.post(
            f"/api/portal/parent-messages/threads/{tid}/messages",
            json={"body": "2"},
            cookies={"access_token": t_tk},
        )
        client.post(
            f"/api/portal/parent-messages/threads/{tid}/messages",
            json={"body": "3"},
            cookies={"access_token": t_tk},
        )
        n = client.get(
            "/api/portal/parent-messages/unread-count",
            cookies={"access_token": t_tk},
        ).json()["unread_count"]
        assert n == 0


# ════════════════════════════════════════════════════════════════════════
# Thread 列表 + 訊息列表
# ════════════════════════════════════════════════════════════════════════


class TestThreadListing:
    def test_parent_thread_list_only_own(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent_a, teacher, emp, student_a, _ = _seed(
                session, line_id="UA", student_name="A1", classroom_name="A班"
            )
            parent_b, _, _, student_b, _ = _seed(
                session, line_id="UB", student_name="B1", classroom_name="B班"
            )
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_a_tk = _parent_token(parent_a)
            sid_a, pid_a = student_a.id, parent_a.id

        client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid_a, "parent_user_id": pid_a, "body": "對 A"},
            cookies={"access_token": t_tk},
        )
        # Parent A 視角應只看到自己 thread
        resp = client.get(
            "/api/parent/messages/threads", cookies={"access_token": p_a_tk}
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["student_id"] == sid_a

    def test_messages_paginated(self, msg_client):
        client, sf = msg_client
        with sf() as session:
            parent, teacher, emp, student, _ = _seed(session)
            session.commit()
            t_tk = _teacher_token(teacher, emp.id)
            p_tk = _parent_token(parent)
            sid, pid = student.id, parent.id
        # 先發 1 + 35 條訊息
        rsp = client.post(
            "/api/portal/parent-messages/threads",
            json={"student_id": sid, "parent_user_id": pid, "body": "頭一條"},
            cookies={"access_token": t_tk},
        )
        thread_id = rsp.json()["thread"]["id"]
        for i in range(35):
            client.post(
                f"/api/portal/parent-messages/threads/{thread_id}/messages",
                json={"body": f"msg {i}"},
                cookies={"access_token": t_tk},
            )
        page1 = client.get(
            f"/api/parent/messages/threads/{thread_id}/messages?limit=30",
            cookies={"access_token": p_tk},
        ).json()
        assert len(page1["items"]) == 30
        assert page1["next_cursor"] is not None

        page2 = client.get(
            f"/api/parent/messages/threads/{thread_id}/messages"
            f"?limit=30&cursor={page1['next_cursor']}",
            cookies={"access_token": p_tk},
        ).json()
        assert len(page2["items"]) == 6  # 35 + 1 首則 - 30
