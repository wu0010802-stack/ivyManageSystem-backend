from unittest.mock import MagicMock
"""SQLite 整合測試:批次核准遇到「同批兩張同員工同時段」的 in-batch 重疊。

P2-5 補丁驗證:
    我在 batch_approve_leaves 的 phase 1 迴圈內加了 _check_overlap。
    對於兩張 pending 的同員工同時段假單,若同時送入批次核准,
    autoflush 應讓後一張的 _check_overlap 看到前一張已被 set is_approved=True
    而把後一張擋下,只成功核准其中一張。

    這次測試用真 SQLite session 而非 mock,以驗證 autoflush 確實生效;
    若不生效,測試會失敗,提示需要在 set is_approved 後顯式 session.flush()。
"""

import sys
import os
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, LeaveRecord, LeaveQuota


@pytest.fixture
def integration_session(tmp_path):
    """建立隔離的 sqlite 環境並覆寫 base_module 的 session factory。"""
    db_path = tmp_path / "batch-overlap.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    try:
        yield session_factory
    finally:
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


def _seed_two_overlapping_pending(session_factory):
    """同員工同期間兩張 pending 假單。"""
    s = session_factory()
    try:
        emp = Employee(
            employee_id="E001",
            name="員工 A",
            base_salary=30000,
            is_active=True,
        )
        s.add(emp)
        s.flush()

        # 充足配額(56h)
        s.add(
            LeaveQuota(
                employee_id=emp.id,
                year=2026,
                leave_type="annual",
                total_hours=56.0,
                note="整合測試",
            )
        )

        leave_a = LeaveRecord(
            employee_id=emp.id,
            leave_type="annual",
            start_date=date(2026, 3, 15),
            end_date=date(2026, 3, 15),
            leave_hours=8.0,
            is_deductible=False,
            deduction_ratio=0.0,
            is_approved=None,
        )
        leave_b = LeaveRecord(
            employee_id=emp.id,
            leave_type="annual",
            start_date=date(2026, 3, 15),
            end_date=date(2026, 3, 15),
            leave_hours=8.0,
            is_deductible=False,
            deduction_ratio=0.0,
            is_approved=None,
        )
        s.add(leave_a)
        s.add(leave_b)
        s.commit()
        return emp.id, leave_a.id, leave_b.id
    finally:
        s.close()


def test_in_batch_same_employee_same_period_blocks_second(
    integration_session, monkeypatch
):
    """同批兩張同員工同時段一起核准,第二張必須被擋(autoflush 必須讓 _check_overlap 看到第一張已 approved)。"""
    from api.leaves import batch_approve_leaves, LeaveBatchApproveRequest
    import api.leaves as leaves_module

    emp_id, id_a, id_b = _seed_two_overlapping_pending(integration_session)

    # 跳過跟本案無關的檢查/副作用
    monkeypatch.setattr(leaves_module, "_check_approval_eligibility", lambda *a, **k: True)
    monkeypatch.setattr(leaves_module, "_check_substitute_guard", lambda *a, **k: None)
    monkeypatch.setattr(
        leaves_module, "_check_substitute_leave_conflict", lambda *a, **k: None
    )
    monkeypatch.setattr(
        leaves_module, "validate_leave_hours_against_schedule", lambda *a, **k: None
    )
    monkeypatch.setattr(leaves_module, "_check_leave_limits", lambda *a, **k: None)
    monkeypatch.setattr(leaves_module, "_guard_leave_quota", lambda *a, **k: None)
    monkeypatch.setattr(
        leaves_module, "_check_salary_months_not_finalized", lambda *a, **k: None
    )
    monkeypatch.setattr(leaves_module, "_write_approval_log", lambda *a, **k: None)
    monkeypatch.setattr(leaves_module, "_salary_engine", None)
    monkeypatch.setattr(
        "services.leave_policy.requires_supporting_document", lambda *a, **k: False
    )

    req = LeaveBatchApproveRequest(ids=[id_a, id_b], approved=True)
    result = batch_approve_leaves(
        request=MagicMock(),
        data=req,
        _rl=None,
        current_user={"username": "supervisor", "role": "supervisor"},
    )

    succeeded = result["succeeded"]
    failed_ids = [f["id"] for f in result["failed"]]

    assert len(succeeded) == 1, (
        f"in-batch 同員工同時段:應僅一張成功,實際 succeeded={succeeded}, failed={result['failed']}"
    )
    assert len(failed_ids) == 1, f"應有一張失敗,實際 failed={result['failed']}"

    # 後者(id_b)被擋
    assert succeeded == [id_a]
    assert failed_ids == [id_b]
    fail_reason = result["failed"][0]["reason"]
    assert "重疊" in fail_reason or "重複" in fail_reason or str(id_a) in fail_reason

    # DB 驗證:只有 id_a 被 approve
    s = integration_session()
    try:
        a = s.query(LeaveRecord).filter(LeaveRecord.id == id_a).first()
        b = s.query(LeaveRecord).filter(LeaveRecord.id == id_b).first()
        assert a.is_approved is True
        assert b.is_approved is None  # 仍 pending(被擋下,batch 不會改它)
    finally:
        s.close()
