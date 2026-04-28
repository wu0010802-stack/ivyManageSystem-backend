"""LINE 推播 gate（Phase 4）。

驗證 should_push_to_parent 在不同條件下的行為：
- service disabled / 未配 token → None
- 找不到 user / user inactive → None
- line_user_id NULL → None
- line_follow_confirmed_at NULL → None
- 全部齊備 → 回 line_user_id

並驗證 notify_parent_message_received 在 gate 不通時不會打 LINE API（透過 mock _push_to_user）。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, User
from services.line_service import LineService


@pytest.fixture
def session_factory(tmp_path):
    db_path = tmp_path / "push-gate.sqlite"
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


def _enabled_svc() -> LineService:
    s = LineService()
    s.configure(token="t", target_id="g", enabled=True)
    return s


def _make_parent(
    sf,
    *,
    line_user_id="U001",
    follow_confirmed=True,
    is_active=True,
):
    with sf() as session:
        u = User(
            username="p",
            password_hash="!",
            role="parent",
            permissions=0,
            is_active=is_active,
            line_user_id=line_user_id,
            line_follow_confirmed_at=datetime.now() if follow_confirmed else None,
            token_version=0,
        )
        session.add(u)
        session.commit()
        return u.id


class TestShouldPushGate:
    def test_disabled_service_returns_none(self, session_factory):
        sf = session_factory
        uid = _make_parent(sf)
        svc = LineService()  # not configured
        with sf() as session:
            assert (
                svc.should_push_to_parent(
                    session, user_id=uid, event_type="message_received"
                )
                is None
            )

    def test_user_not_found_returns_none(self, session_factory):
        sf = session_factory
        svc = _enabled_svc()
        with sf() as session:
            assert (
                svc.should_push_to_parent(
                    session, user_id=99999, event_type="message_received"
                )
                is None
            )

    def test_inactive_user_returns_none(self, session_factory):
        sf = session_factory
        uid = _make_parent(sf, is_active=False)
        svc = _enabled_svc()
        with sf() as session:
            assert (
                svc.should_push_to_parent(
                    session, user_id=uid, event_type="message_received"
                )
                is None
            )

    def test_no_line_user_id_returns_none(self, session_factory):
        sf = session_factory
        uid = _make_parent(sf, line_user_id=None)
        svc = _enabled_svc()
        with sf() as session:
            assert (
                svc.should_push_to_parent(
                    session, user_id=uid, event_type="message_received"
                )
                is None
            )

    def test_no_follow_confirmed_returns_none(self, session_factory):
        sf = session_factory
        uid = _make_parent(sf, follow_confirmed=False)
        svc = _enabled_svc()
        with sf() as session:
            assert (
                svc.should_push_to_parent(
                    session, user_id=uid, event_type="message_received"
                )
                is None
            )

    def test_all_conditions_met_returns_line_user_id(self, session_factory):
        sf = session_factory
        uid = _make_parent(sf, line_user_id="U_OK")
        svc = _enabled_svc()
        with sf() as session:
            assert (
                svc.should_push_to_parent(
                    session, user_id=uid, event_type="message_received"
                )
                == "U_OK"
            )


class TestNotifyParentMessageReceived:
    def test_skips_push_when_gate_blocked(self, session_factory):
        sf = session_factory
        uid = _make_parent(sf, follow_confirmed=False)  # gate blocks
        svc = _enabled_svc()
        with sf() as session:
            with patch.object(svc, "_push_to_user") as p:
                svc.notify_parent_message_received(
                    session,
                    parent_user_id=uid,
                    teacher_name="王老師",
                    student_name="小明",
                    body_preview="記得帶外套",
                )
                assert p.call_count == 0

    def test_pushes_when_gate_passes(self, session_factory):
        sf = session_factory
        uid = _make_parent(sf, line_user_id="U_TEST")
        svc = _enabled_svc()
        with sf() as session:
            with patch.object(svc, "_push_to_user", return_value=True) as p:
                svc.notify_parent_message_received(
                    session,
                    parent_user_id=uid,
                    teacher_name="王老師",
                    student_name="小明",
                    body_preview="記得帶外套",
                )
                assert p.call_count == 1
                args, _ = p.call_args
                assert args[0] == "U_TEST"
                # 訊息含 teacher_name, student_name 與 preview
                text = args[1]
                assert "王老師" in text
                assert "小明" in text
                assert "記得帶外套" in text

    def test_long_preview_truncated(self, session_factory):
        sf = session_factory
        uid = _make_parent(sf)
        svc = _enabled_svc()
        long = "a" * 200
        with sf() as session:
            with patch.object(svc, "_push_to_user", return_value=True) as p:
                svc.notify_parent_message_received(
                    session,
                    parent_user_id=uid,
                    teacher_name="老師",
                    student_name=None,
                    body_preview=long,
                )
        text = p.call_args[0][1]
        # 60 字 + 省略號
        assert "…" in text
        # 整段預覽不應全長 200 字
        assert "a" * 200 not in text
