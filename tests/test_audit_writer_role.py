"""Spec D PR-D3: audit middleware SET LOCAL ROLE switching + fail-open。

覆蓋 _write_audit_sync 的兩個行為：
1. PG dialect 時 INSERT 前 SET LOCAL ROLE ivy_audit_writer 被 call。
2. SET LOCAL ROLE raise 時 audit INSERT 仍成功 + log warning（fail-open）。

設計說明：_write_audit_sync 內部透過 get_session() 建立 session，因此 test
必須 mock utils.audit.get_session 才能注入 mock session；直接傳 test_db_session
fixture 無法攔截該 path。
"""

import logging
import os
import sys
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy.exc import ProgrammingError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.audit import _write_audit_sync


def _make_payload() -> dict:
    """最小合法 audit payload（SQLAlchemy model 必要欄位）。"""
    return dict(
        user_id=1,
        username="test_user",
        action="CREATE",
        entity_type="employee",
        entity_id="99",
        summary="新增員工",
        changes=None,
        ip_address="127.0.0.1",
        created_at=None,
    )


def _make_mock_session(dialect_name: str = "postgresql") -> MagicMock:
    """建立 mock session，dialect.name 設為指定值。"""
    session = MagicMock()
    session.bind.dialect.name = dialect_name
    return session


class TestAuditMiddlewareUsesAuditWriterRole:
    """PG dialect 時 _write_audit_sync 執行 SET LOCAL ROLE ivy_audit_writer。"""

    def test_set_local_role_called_for_postgresql(self):
        mock_session = _make_mock_session("postgresql")

        with patch("utils.audit.get_session", return_value=mock_session):
            _write_audit_sync(_make_payload())

        # 驗證 session.execute 中有一次 call 包含 SET LOCAL ROLE ivy_audit_writer
        execute_calls = mock_session.execute.call_args_list
        set_role_calls = [
            c
            for c in execute_calls
            if c.args
            and "SET LOCAL ROLE" in str(c.args[0])
            and "ivy_audit_writer" in str(c.args[0])
        ]
        assert (
            set_role_calls
        ), f"Expected SET LOCAL ROLE ivy_audit_writer call, got execute calls: {execute_calls}"
        # session.add + session.commit 也必須執行（確保 INSERT 發生）
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    def test_set_local_role_not_called_for_sqlite(self):
        """SQLite dialect 時跳過 SET LOCAL ROLE（SQLite 無 role 概念）。"""
        mock_session = _make_mock_session("sqlite")

        with patch("utils.audit.get_session", return_value=mock_session):
            _write_audit_sync(_make_payload())

        execute_calls = mock_session.execute.call_args_list
        set_role_calls = [
            c for c in execute_calls if c.args and "SET LOCAL ROLE" in str(c.args[0])
        ]
        assert (
            not set_role_calls
        ), f"Expected no SET LOCAL ROLE for SQLite, got: {set_role_calls}"
        # INSERT 仍必須發生
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()


class TestAuditWriterRoleMissingFallsOpen:
    """SET LOCAL ROLE raise → fall-open：audit INSERT 仍成功 + log warning。"""

    def test_falls_open_on_set_local_role_error(self, caplog):
        mock_session = _make_mock_session("postgresql")

        # 讓 session.execute 在遇到 SET LOCAL ROLE 時 raise ProgrammingError
        def _execute_side_effect(stmt, *args, **kwargs):
            if "SET LOCAL ROLE" in str(stmt):
                raise ProgrammingError(
                    "SET LOCAL ROLE ivy_audit_writer",
                    None,
                    Exception("permission denied for role ivy_audit_writer"),
                )
            return MagicMock()

        mock_session.execute.side_effect = _execute_side_effect

        with patch("utils.audit.get_session", return_value=mock_session):
            with caplog.at_level(logging.WARNING, logger="utils.audit"):
                # 不應 raise：fail-open 保證
                _write_audit_sync(_make_payload())

        # INSERT 仍必須執行（fail-open）
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

        # 應有 warning log 記錄 fallback
        warning_msgs = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "SET LOCAL ROLE ivy_audit_writer failed" in m for m in warning_msgs
        ), f"Expected SET LOCAL ROLE fail warning, got: {warning_msgs}"
