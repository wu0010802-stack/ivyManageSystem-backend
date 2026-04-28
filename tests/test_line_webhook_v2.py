"""LINE Webhook 雙向（Phase 5）。

驗證：
- webhookEventId 防重
- 家長 reply with context → 寫 ParentMessage(source='line')
- 家長 reply without context → quick-reply 回傳
- postback → upsert LineReplyContext
- context TTL 過期不命中
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    Classroom,
    Employee,
    Guardian,
    LineReplyContext,
    LineWebhookEvent,
    ParentMessage,
    ParentMessageThread,
    Student,
    User,
)
from services.line_reply_router import (
    CONTEXT_TTL,
    deduplicate_event,
    handle_parent_postback,
    handle_parent_text_message,
    upsert_reply_context,
)


@pytest.fixture
def session_factory(tmp_path):
    db_path = tmp_path / "webhook.sqlite"
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


def _seed_thread(session, *, parent_line_id="UA"):
    parent = User(
        username="p",
        password_hash="!",
        role="parent",
        permissions=0,
        is_active=True,
        line_user_id=parent_line_id,
        line_follow_confirmed_at=datetime.now(),
        token_version=0,
    )
    session.add(parent)
    session.flush()
    teacher = User(
        username="王老師",
        password_hash="!",
        role="teacher",
        permissions=0,
        is_active=True,
        token_version=0,
    )
    session.add(teacher)
    session.flush()
    classroom = Classroom(name="A班", is_active=True)
    session.add(classroom)
    session.flush()
    student = Student(
        student_id="S1",
        name="小明",
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
    thread = ParentMessageThread(
        parent_user_id=parent.id,
        teacher_user_id=teacher.id,
        student_id=student.id,
        last_message_at=datetime.now(),
    )
    session.add(thread)
    session.flush()
    return parent, teacher, student, thread


# ════════════════════════════════════════════════════════════════════════
# webhookEventId 防重
# ════════════════════════════════════════════════════════════════════════


class TestDeduplicateEvent:
    def test_first_event_returns_true(self, session_factory):
        sf = session_factory
        with sf() as session:
            assert (
                deduplicate_event(
                    session,
                    webhook_event_id="evt-1",
                    event_type="message",
                    line_user_id="U1",
                )
                is True
            )
            session.commit()

    def test_duplicate_event_returns_false(self, session_factory):
        sf = session_factory
        with sf() as session:
            deduplicate_event(
                session,
                webhook_event_id="evt-x",
                event_type="message",
                line_user_id="U1",
            )
            session.commit()
        with sf() as session:
            assert (
                deduplicate_event(
                    session,
                    webhook_event_id="evt-x",
                    event_type="message",
                    line_user_id="U1",
                )
                is False
            )

    def test_empty_event_id_passes_through(self, session_factory):
        sf = session_factory
        with sf() as session:
            assert (
                deduplicate_event(
                    session,
                    webhook_event_id="",
                    event_type="message",
                    line_user_id="U1",
                )
                is True
            )


# ════════════════════════════════════════════════════════════════════════
# Postback handler
# ════════════════════════════════════════════════════════════════════════


class TestPostback:
    def test_postback_creates_reply_context(self, session_factory):
        sf = session_factory
        with sf() as session:
            parent, _, _, thread = _seed_thread(session)
            session.commit()

            mock_svc = MagicMock()
            handle_parent_postback(
                session,
                line_service=mock_svc,
                parent_user=parent,
                data=f"thread_id={thread.id}",
                reply_token="rt",
            )
            ctx = (
                session.query(LineReplyContext)
                .filter(LineReplyContext.line_user_id == parent.line_user_id)
                .first()
            )
            assert ctx is not None
            assert ctx.thread_id == thread.id
            assert ctx.expires_at > datetime.now()

    def test_postback_invalid_thread_replies_error(self, session_factory):
        sf = session_factory
        with sf() as session:
            parent, _, _, _ = _seed_thread(session)
            session.commit()

            mock_svc = MagicMock()
            handle_parent_postback(
                session,
                line_service=mock_svc,
                parent_user=parent,
                data="thread_id=99999",
                reply_token="rt",
            )
            mock_svc._reply.assert_called_once()
            assert "找不到" in mock_svc._reply.call_args[0][1]


# ════════════════════════════════════════════════════════════════════════
# Parent text message
# ════════════════════════════════════════════════════════════════════════


class TestParentTextMessage:
    def test_with_context_writes_message(self, session_factory):
        sf = session_factory
        with sf() as session:
            parent, _, _, thread = _seed_thread(session)
            upsert_reply_context(
                session, line_user_id=parent.line_user_id, thread_id=thread.id
            )
            session.commit()

            mock_svc = MagicMock()
            handle_parent_text_message(
                session,
                line_service=mock_svc,
                parent_user=parent,
                text="老師早安",
                reply_token="rt",
            )

            msg = (
                session.query(ParentMessage)
                .filter(ParentMessage.thread_id == thread.id)
                .first()
            )
            assert msg is not None
            assert msg.body == "老師早安"
            assert msg.source == "line"
            assert msg.sender_role == "parent"
            mock_svc._reply.assert_called_once()
            assert "送出" in mock_svc._reply.call_args[0][1]

    def test_without_context_returns_quick_reply(self, session_factory):
        sf = session_factory
        with sf() as session:
            parent, _, _, _ = _seed_thread(session)
            session.commit()

            mock_svc = MagicMock()
            handle_parent_text_message(
                session,
                line_service=mock_svc,
                parent_user=parent,
                text="哈囉",
                reply_token="rt",
            )
            mock_svc._reply_with_quick_reply.assert_called_once()
            args = mock_svc._reply_with_quick_reply.call_args[0]
            assert "選擇" in args[1]
            quick = args[2]
            assert quick["items"]
            assert quick["items"][0]["action"]["type"] == "postback"

    def test_expired_context_does_not_write(self, session_factory):
        sf = session_factory
        with sf() as session:
            parent, _, _, thread = _seed_thread(session)
            ctx = upsert_reply_context(
                session, line_user_id=parent.line_user_id, thread_id=thread.id
            )
            ctx.expires_at = datetime.now() - timedelta(minutes=1)
            session.commit()

            mock_svc = MagicMock()
            handle_parent_text_message(
                session,
                line_service=mock_svc,
                parent_user=parent,
                text="late",
                reply_token="rt",
            )
            # 應走無 context 路徑，不寫 message
            n = (
                session.query(ParentMessage)
                .filter(ParentMessage.thread_id == thread.id)
                .count()
            )
            assert n == 0
            # 並且應該回 quick-reply 而不是已送出
            mock_svc._reply_with_quick_reply.assert_called_once()

    def test_context_refreshed_on_reply(self, session_factory):
        sf = session_factory
        with sf() as session:
            parent, _, _, thread = _seed_thread(session)
            ctx = upsert_reply_context(
                session, line_user_id=parent.line_user_id, thread_id=thread.id
            )
            old_expires = ctx.expires_at
            # 把 expires_at 往前 5 分鐘做差距觀察
            ctx.expires_at = old_expires - timedelta(minutes=5)
            session.commit()

            mock_svc = MagicMock()
            handle_parent_text_message(
                session,
                line_service=mock_svc,
                parent_user=parent,
                text="再來一句",
                reply_token="rt",
            )
            ctx_after = (
                session.query(LineReplyContext)
                .filter(LineReplyContext.line_user_id == parent.line_user_id)
                .first()
            )
            assert ctx_after.expires_at > old_expires - timedelta(minutes=5)


# ════════════════════════════════════════════════════════════════════════
# context upsert
# ════════════════════════════════════════════════════════════════════════


class TestWebhookEndpointIntegration:
    """直接打 /api/line/webhook 端點，驗證 dispatcher 把 parent 路由到 reply handler、
    teacher 路由到既有 staff handler。"""

    def _post(self, client, payload, *, signature="bypass"):
        return client.post(
            "/api/line/webhook",
            content=payload,
            headers={"X-Line-Signature": signature, "Content-Type": "application/json"},
        )

    def test_parent_text_dispatched_to_parent_handler(
        self, session_factory, monkeypatch
    ):
        import json

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.line_webhook import (
            init_webhook_service,
            router as webhook_router,
            verify_line_signature,
        )

        sf = session_factory
        with sf() as session:
            parent, _, _, thread = _seed_thread(session)
            from services.line_reply_router import upsert_reply_context

            upsert_reply_context(
                session, line_user_id=parent.line_user_id, thread_id=thread.id
            )
            session.commit()
            # 抽出 primitives；不持有 ORM instance 跨 session
            line_id = parent.line_user_id
            thread_id = thread.id

        # mock LineService（避免打真 LINE API）
        from services.line_service import LineService

        svc = LineService()
        svc.configure(token="t", target_id="g", enabled=True, channel_secret="s")
        svc._reply = MagicMock(return_value=True)
        svc._reply_with_quick_reply = MagicMock(return_value=True)
        svc.handle_webhook_message = MagicMock()
        init_webhook_service(svc)

        payload = json.dumps(
            {
                "events": [
                    {
                        "type": "message",
                        "webhookEventId": "evt-int-1",
                        "source": {"userId": line_id},
                        "replyToken": "rt-int-1",
                        "message": {"type": "text", "text": "謝謝老師"},
                    }
                ]
            }
        ).encode()

        app = FastAPI()
        app.include_router(webhook_router)
        # bypass signature check：override dependency 回實際 body
        app.dependency_overrides[verify_line_signature] = lambda: payload

        with TestClient(app) as client:
            resp = client.post(
                "/api/line/webhook",
                content=payload,
                headers={
                    "X-Line-Signature": "x",
                    "Content-Type": "application/json",
                },
            )

        assert resp.status_code == 200
        # parent 路徑：應呼叫 _reply（已送出）；不應呼叫 staff handler
        svc._reply.assert_called()
        svc.handle_webhook_message.assert_not_called()
        # DB 應寫入一筆 source='line' 訊息
        with sf() as session:
            m = (
                session.query(ParentMessage)
                .filter(ParentMessage.thread_id == thread_id)
                .first()
            )
            assert m is not None
            assert m.source == "line"
            assert m.body == "謝謝老師"

    def test_teacher_text_routed_to_staff_handler(self, session_factory):
        import json

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.line_webhook import (
            init_webhook_service,
            router as webhook_router,
            verify_line_signature,
        )

        sf = session_factory
        with sf() as session:
            teacher = User(
                username="t",
                password_hash="!",
                role="teacher",
                permissions=0,
                is_active=True,
                line_user_id="UT001",
                token_version=0,
            )
            session.add(teacher)
            session.commit()

        from services.line_service import LineService

        svc = LineService()
        svc.configure(token="t", target_id="g", enabled=True, channel_secret="s")
        svc._reply = MagicMock()
        svc.handle_webhook_message = MagicMock()
        init_webhook_service(svc)

        payload = json.dumps(
            {
                "events": [
                    {
                        "type": "message",
                        "webhookEventId": "evt-int-2",
                        "source": {"userId": "UT001"},
                        "replyToken": "rt2",
                        "message": {"type": "text", "text": "我的薪資"},
                    }
                ]
            }
        ).encode()

        app = FastAPI()
        app.include_router(webhook_router)
        app.dependency_overrides[verify_line_signature] = lambda: payload

        with TestClient(app) as client:
            resp = client.post(
                "/api/line/webhook",
                content=payload,
                headers={
                    "X-Line-Signature": "x",
                    "Content-Type": "application/json",
                },
            )

        assert resp.status_code == 200
        svc.handle_webhook_message.assert_called_once()


class TestPushWithQuickReply:
    """Phase 5 實際使用者可見的功能：教師發訊推播時帶 quick-reply postback，
    家長點 → 觸發 postback → 進 reply 模式。"""

    def test_notify_parent_message_with_thread_id_uses_quick_reply(
        self, session_factory
    ):
        from datetime import datetime as _dt

        from services.line_service import LineService

        sf = session_factory
        with sf() as session:
            parent = User(
                username="p",
                password_hash="!",
                role="parent",
                permissions=0,
                is_active=True,
                line_user_id="U_PUSH",
                line_follow_confirmed_at=_dt.now(),
                token_version=0,
            )
            session.add(parent)
            session.commit()
            uid = parent.id

        svc = LineService()
        svc.configure(token="t", target_id="g", enabled=True)
        svc._push_to_user = MagicMock()
        svc._push_to_user_with_quick_reply = MagicMock(return_value=True)

        with sf() as session:
            svc.notify_parent_message_received(
                session,
                parent_user_id=uid,
                teacher_name="陳老師",
                student_name="小華",
                body_preview="今日表現很好",
                thread_id=42,
            )

        # 走 quick-reply 路徑，不應呼叫純文字 push
        svc._push_to_user.assert_not_called()
        svc._push_to_user_with_quick_reply.assert_called_once()
        args = svc._push_to_user_with_quick_reply.call_args[0]
        line_id, text, quick_reply = args
        assert line_id == "U_PUSH"
        assert "陳老師" in text
        assert "小華" in text
        # 驗證 postback data
        assert quick_reply["items"]
        action = quick_reply["items"][0]["action"]
        assert action["type"] == "postback"
        assert action["data"] == "thread_id=42"

    def test_notify_parent_message_without_thread_id_uses_plain_push(
        self, session_factory
    ):
        from datetime import datetime as _dt

        from services.line_service import LineService

        sf = session_factory
        with sf() as session:
            parent = User(
                username="p",
                password_hash="!",
                role="parent",
                permissions=0,
                is_active=True,
                line_user_id="U_PLAIN",
                line_follow_confirmed_at=_dt.now(),
                token_version=0,
            )
            session.add(parent)
            session.commit()
            uid = parent.id

        svc = LineService()
        svc.configure(token="t", target_id="g", enabled=True)
        svc._push_to_user = MagicMock(return_value=True)
        svc._push_to_user_with_quick_reply = MagicMock()

        with sf() as session:
            svc.notify_parent_message_received(
                session,
                parent_user_id=uid,
                teacher_name="王老師",
                student_name=None,
                body_preview="hi",
                # thread_id 不傳
            )

        svc._push_to_user.assert_called_once()
        svc._push_to_user_with_quick_reply.assert_not_called()


class TestUpsertReplyContext:
    def test_creates_when_missing(self, session_factory):
        sf = session_factory
        with sf() as session:
            parent, _, _, thread = _seed_thread(session)
            session.commit()
            ctx = upsert_reply_context(
                session, line_user_id=parent.line_user_id, thread_id=thread.id
            )
            session.commit()
            assert ctx.thread_id == thread.id

    def test_updates_when_exists(self, session_factory):
        sf = session_factory
        with sf() as session:
            parent, _, _, thread = _seed_thread(session)
            session.commit()
            upsert_reply_context(
                session, line_user_id=parent.line_user_id, thread_id=thread.id
            )
            # 第二次同 line_user_id；UNIQUE → 應更新
            other_thread = ParentMessageThread(
                parent_user_id=parent.id,
                teacher_user_id=thread.teacher_user_id,
                student_id=thread.student_id + 1,  # 不同 student（為通過 UNIQUE）
                last_message_at=datetime.now(),
            )
            # 但 thread 表 UNIQUE(parent, teacher, student) 不應違規（student_id+1 是不同）
            # 但 student_id+1 可能並不存在於 students 表 — SQLite 沒 FK enforcement 所以ok
            session.add(other_thread)
            session.flush()
            ctx = upsert_reply_context(
                session,
                line_user_id=parent.line_user_id,
                thread_id=other_thread.id,
            )
            session.commit()
            rows = (
                session.query(LineReplyContext)
                .filter(LineReplyContext.line_user_id == parent.line_user_id)
                .all()
            )
            assert len(rows) == 1
            assert rows[0].thread_id == other_thread.id
