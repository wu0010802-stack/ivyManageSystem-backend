"""驗證升級後的 _write_approval_log：keyword-only + metadata 序列化。

設計重點：
- keyword-only：三 router 共用此 helper，加 metadata 為新增欄位，強制 keyword 呼叫避免位置混淆。
- metadata-in-comment：不動 ApprovalLog schema，用 `[META]` 分隔符嵌入 comment 尾段；
  前端僅顯示 `[META]` 前段，metadata 留給 audit/report 解析。
"""

import json
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import ApprovalLog, Base
from utils.approval_helpers import _write_approval_log


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "approval-log-writer.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


def test_write_approval_log_with_metadata_serializes_into_comment(db_session):
    log = _write_approval_log(
        session=db_session,
        doc_type="leave",
        doc_id=999,
        action="approve",
        approver={"id": 1, "username": "admin", "role": "admin"},
        comment="ok",
        metadata={"delegate_id": 42, "cross_offset_ot_id": None},
    )
    assert log is not None
    assert log.comment is not None
    assert "[META]" in log.comment
    payload = json.loads(log.comment.split("[META]", 1)[1])
    assert payload == {"delegate_id": 42, "cross_offset_ot_id": None}


def test_write_approval_log_without_metadata_backward_compatible(db_session):
    log = _write_approval_log(
        session=db_session,
        doc_type="overtime",
        doc_id=1,
        action="reject",
        approver={"id": 2, "username": "manager", "role": "supervisor"},
        comment="overlap",
    )
    assert log is not None
    assert log.comment == "overlap"


def test_write_approval_log_positional_rejected():
    """改 keyword-only 後，舊 positional 呼叫應拋 TypeError"""
    with pytest.raises(TypeError):
        _write_approval_log(  # type: ignore[call-arg]
            "leave",
            1,
            "approve",
            {"id": 1},
            "ok",
            None,
        )
