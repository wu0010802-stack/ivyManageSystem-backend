"""驗證 services.approval.batch_executor 兩段提交。

Covers:
- 全部通過 → side_effects 執行、commit、succeeded 含 (record, approval_log_id)
- fail_fast=True：任一 validator 失敗 → 整批 abort（無 side_effects、無 commit）
- fail_fast=False（預設）：partial-success — 失敗條目落 failed、通過條目仍走 Pass 2
- 收集所有 validator 失敗（不在第一個失敗就停）
- reject 時 rejection_reason 傳進 _write_approval_log.comment
"""

import os
import sys
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.approval.batch_executor import BatchResult, execute_batch_approval


@dataclass
class _FakeRecord:
    id: int


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.adds = []

    def add(self, obj):
        self.adds.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@dataclass
class _FakeLog:
    id: int


def _make_actor():
    return {"id": 1, "username": "admin", "role": "admin"}


def _no_op_validator(_s, _rec):
    pass


def _no_op_side_effects(_s, _recs):
    pass


def test_batch_approve_all_pass_runs_side_effects_and_commits():
    session = _FakeSession()
    records = [_FakeRecord(id=i) for i in range(1, 4)]
    side_effect_calls = []

    def side_effects(_s, recs):
        side_effect_calls.append([r.id for r in recs])

    with patch(
        "services.approval.batch_executor._write_approval_log",
        side_effect=lambda **kw: _FakeLog(id=kw["doc_id"] * 100),
    ):
        result = execute_batch_approval(
            session=session,
            doc_type="leave",
            record_ids=[r.id for r in records],
            action="approve",
            actor=_make_actor(),
            validator=_no_op_validator,
            side_effects=side_effects,
            record_loader=lambda _s, _ids: records,
        )

    assert isinstance(result, BatchResult)
    assert len(result.succeeded) == 3
    assert [rec.id for rec, _log_id in result.succeeded] == [1, 2, 3]
    assert [log_id for _rec, log_id in result.succeeded] == [100, 200, 300]
    assert result.failed == []
    assert session.commits == 1
    assert side_effect_calls == [[1, 2, 3]]


def test_batch_approve_fail_fast_one_invalid_aborts_all():
    session = _FakeSession()
    records = [_FakeRecord(id=i) for i in range(1, 6)]

    def validator(_s, rec):
        if rec.id == 3:
            raise HTTPException(status_code=422, detail="overlap")

    def side_effects(_s, _recs):
        raise AssertionError("side_effects should not run when validator fails")

    with patch("services.approval.batch_executor._write_approval_log") as mock_log:
        result = execute_batch_approval(
            session=session,
            doc_type="leave",
            record_ids=[r.id for r in records],
            action="approve",
            actor=_make_actor(),
            validator=validator,
            side_effects=side_effects,
            record_loader=lambda _s, _ids: records,
            fail_fast=True,
        )

    assert result.succeeded == []
    assert len(result.failed) == 1
    assert result.failed[0]["id"] == 3
    assert result.failed[0]["reason"] == "overlap"
    assert session.commits == 0
    assert not mock_log.called


def test_batch_approve_fail_fast_collects_all_failures_before_aborting():
    """fail_fast=True 時，Pass 1 仍跑完所有 record（收集全部失敗給使用者），但不寫入。"""
    session = _FakeSession()
    records = [_FakeRecord(id=i) for i in range(1, 6)]

    def validator(_s, rec):
        if rec.id in (2, 4):
            raise HTTPException(status_code=422, detail=f"err-{rec.id}")

    def side_effects(_s, _recs):
        raise AssertionError("不應執行")

    with patch("services.approval.batch_executor._write_approval_log") as mock_log:
        result = execute_batch_approval(
            session=session,
            doc_type="leave",
            record_ids=[r.id for r in records],
            action="approve",
            actor=_make_actor(),
            validator=validator,
            side_effects=side_effects,
            record_loader=lambda _s, _ids: records,
            fail_fast=True,
        )

    assert result.succeeded == []
    assert {f["id"] for f in result.failed} == {2, 4}
    assert {f["reason"] for f in result.failed} == {"err-2", "err-4"}
    assert session.commits == 0
    assert not mock_log.called


def test_batch_approve_partial_success_mode_default():
    """fail_fast=False（預設）：失敗條目落 failed、通過條目仍寫入 + commit。

    對應現行 leaves / overtimes batch_approve UX，2026-05-12 修補後即此語意。
    """
    session = _FakeSession()
    records = [_FakeRecord(id=i) for i in range(1, 6)]
    side_effect_calls = []

    def validator(_s, rec):
        if rec.id in (2, 4):
            raise HTTPException(status_code=422, detail=f"err-{rec.id}")

    def side_effects(_s, recs):
        side_effect_calls.append([r.id for r in recs])

    with patch(
        "services.approval.batch_executor._write_approval_log",
        side_effect=lambda **kw: _FakeLog(id=kw["doc_id"] * 100),
    ):
        result = execute_batch_approval(
            session=session,
            doc_type="leave",
            record_ids=[r.id for r in records],
            action="approve",
            actor=_make_actor(),
            validator=validator,
            side_effects=side_effects,
            record_loader=lambda _s, _ids: records,
            # fail_fast 預設 False
        )

    assert {rec.id for rec, _ in result.succeeded} == {1, 3, 5}
    assert {f["id"] for f in result.failed} == {2, 4}
    assert session.commits == 1
    assert side_effect_calls == [[1, 3, 5]]


def test_batch_approve_partial_success_all_fail_no_commit():
    """partial-success 模式下，若全部 validator 失敗，passed 為空 → 不執行 side_effects、不 commit。"""
    session = _FakeSession()
    records = [_FakeRecord(id=i) for i in range(1, 4)]

    def validator(_s, rec):
        raise HTTPException(status_code=422, detail=f"err-{rec.id}")

    def side_effects(_s, _recs):
        raise AssertionError("passed 為空時不應執行 side_effects")

    with patch("services.approval.batch_executor._write_approval_log") as mock_log:
        result = execute_batch_approval(
            session=session,
            doc_type="leave",
            record_ids=[r.id for r in records],
            action="approve",
            actor=_make_actor(),
            validator=validator,
            side_effects=side_effects,
            record_loader=lambda _s, _ids: records,
        )

    assert result.succeeded == []
    assert len(result.failed) == 3
    assert session.commits == 0
    assert not mock_log.called


def test_batch_reject_passes_reason_to_log():
    """reject action + rejection_reason 應傳成 _write_approval_log 的 comment。"""
    session = _FakeSession()
    records = [_FakeRecord(id=1)]

    with patch("services.approval.batch_executor._write_approval_log") as mock_log:
        mock_log.return_value = _FakeLog(id=999)
        execute_batch_approval(
            session=session,
            doc_type="leave",
            record_ids=[1],
            action="reject",
            actor=_make_actor(),
            validator=_no_op_validator,
            side_effects=_no_op_side_effects,
            record_loader=lambda _s, _ids: records,
            rejection_reason="missing proof",
        )

    assert mock_log.called
    call_kwargs = mock_log.call_args.kwargs
    assert call_kwargs["comment"] == "missing proof"
    assert call_kwargs["action"] == "reject"
    assert call_kwargs["doc_type"] == "leave"
    assert call_kwargs["doc_id"] == 1


def test_batch_approve_handles_write_log_returning_none():
    """_write_approval_log 失敗回 None 時，仍視為 succeeded，approval_log_id=None。"""
    session = _FakeSession()
    records = [_FakeRecord(id=1), _FakeRecord(id=2)]

    with patch(
        "services.approval.batch_executor._write_approval_log", return_value=None
    ):
        result = execute_batch_approval(
            session=session,
            doc_type="leave",
            record_ids=[1, 2],
            action="approve",
            actor=_make_actor(),
            validator=_no_op_validator,
            side_effects=_no_op_side_effects,
            record_loader=lambda _s, _ids: records,
        )

    assert len(result.succeeded) == 2
    assert all(log_id is None for _rec, log_id in result.succeeded)
    assert session.commits == 1
