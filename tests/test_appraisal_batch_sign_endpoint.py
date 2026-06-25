"""POST /cycles/{id}/summaries:batch_sign 6 case 測試（Phase 2 Task 8）。

採「逐筆嘗試，回失敗清單」策略，不是 all-or-nothing。
某筆失敗不會 rollback 其他成功的筆。最後一次 commit。

fixture pattern 沿用 test_appraisal_comment_endpoint.py / test_appraisal_reject_endpoint.py
（SQLite + 真實 JWT cookie login）。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.appraisal import appraisal_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
    AppraisalSummaryLog,
    CycleStatus,
    Grade,
    RoleGroup,
    Semester,
    SummaryLogAction,
    SummaryStatus,
)
from models.auth import User
from models.database import Base
from models.employee import Employee, EmployeeType
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-batch-sign-endpoint.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(appraisal_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username, perms, password="TempPass123"):
    """admin 角色、無 employee_id（避免 assert_not_self_approval 誤殺）。"""
    if isinstance(perms, str):
        perms = [perms]
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=perms,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return res


def _seed_n_summaries(s, n, status=SummaryStatus.DRAFT):
    """建 cycle + n 個 summary。"""
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    s.add(cycle)
    s.flush()
    ids = []
    for i in range(n):
        emp = Employee(
            employee_id=f"E{i:03d}",
            name=f"員工{i}",
            employee_type=EmployeeType.REGULAR.value,
            is_active=True,
        )
        s.add(emp)
        s.flush()
        p = AppraisalParticipant(
            cycle_id=cycle.id,
            employee_id=emp.id,
            role_group=RoleGroup.HEAD_TEACHER,
            hire_months_in_cycle=Decimal("6"),
            is_excluded=False,
        )
        s.add(p)
        s.flush()
        summary = AppraisalSummary(
            participant_id=p.id,
            cycle_id=cycle.id,
            base_score=Decimal("75.6"),
            event_score_sum=Decimal("0"),
            total_score=Decimal("75.6"),
            grade=Grade.PASS,
            bonus_amount=Decimal("0"),
            status=status,
        )
        # 依當前 status 填上各階 signed_by（用一個假 user_id=999；不會與測試
        # 自己建的 actor 重疊衝撞）
        if status in (
            SummaryStatus.SUPERVISOR_SIGNED,
            SummaryStatus.ACCOUNTING_SIGNED,
            SummaryStatus.FINALIZED,
        ):
            summary.supervisor_signed_by = 999
        if status in (SummaryStatus.ACCOUNTING_SIGNED, SummaryStatus.FINALIZED):
            summary.accounting_signed_by = 999
        if status == SummaryStatus.FINALIZED:
            summary.finalized_by = 999
        s.add(summary)
        s.flush()
        ids.append(summary.id)
    s.commit()
    return cycle, ids


# ===== 6 case =====


def test_batch_sign_supervisor_happy(client_with_db):
    """3 個 DRAFT summary 全部 sign SUPERVISOR → succeeded=3, failed=0。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer1", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        cycle, ids = _seed_n_summaries(s, 3, SummaryStatus.DRAFT)
        cycle_id = cycle.id
    _login(client, "reviewer1")
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": ids, "stage": "SUPERVISOR"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["succeeded"]) == sorted(ids)
    assert body["failed"] == []
    with sf() as s:
        for sid in ids:
            assert (
                s.get(AppraisalSummary, sid).status == SummaryStatus.SUPERVISOR_SIGNED
            )


def test_batch_sign_partial_failure(client_with_db):
    """3 個 summary，其中 1 個已 SUPERVISOR_SIGNED 無法再 sign supervisor。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer2", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        cycle, ids = _seed_n_summaries(s, 3, SummaryStatus.DRAFT)
        cycle_id = cycle.id
        s.query(AppraisalSummary).filter_by(id=ids[1]).update(
            {"status": SummaryStatus.SUPERVISOR_SIGNED}
        )
        s.commit()
    _login(client, "reviewer2")
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": ids, "stage": "SUPERVISOR"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["succeeded"]) == sorted([ids[0], ids[2]])
    assert len(body["failed"]) == 1
    assert body["failed"][0]["summary_id"] == ids[1]


def test_batch_sign_finalize_requires_finalize_permission(client_with_db):
    """沒 APPRAISAL_FINALIZE 不可批次 FINALIZE → 403。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer3", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        cycle, ids = _seed_n_summaries(s, 2, SummaryStatus.ACCOUNTING_SIGNED)
        cycle_id = cycle.id
    _login(client, "reviewer3")
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": ids, "stage": "FINALIZE"},
    )
    assert r.status_code == 403


def test_batch_sign_cycle_locked_blocked(client_with_db):
    """cycle.status != OPEN → 400 整批拒絕。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer4", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        cycle, ids = _seed_n_summaries(s, 2, SummaryStatus.DRAFT)
        cycle_id = cycle.id
        cycle.status = CycleStatus.LOCKED
        s.commit()
    _login(client, "reviewer4")
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": ids, "stage": "SUPERVISOR"},
    )
    assert r.status_code == 400


def test_batch_sign_writes_log_per_summary(client_with_db):
    """每筆成功 sign 都寫 1 條 AppraisalSummaryLog。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer5", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        cycle, ids = _seed_n_summaries(s, 3, SummaryStatus.DRAFT)
        cycle_id = cycle.id
    _login(client, "reviewer5")
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": ids, "stage": "SUPERVISOR"},
    )
    assert r.status_code == 200, r.text
    with sf() as s:
        log_count = (
            s.query(AppraisalSummaryLog)
            .filter(
                AppraisalSummaryLog.summary_id.in_(ids),
                AppraisalSummaryLog.action == SummaryLogAction.SIGN_SUPERVISOR,
            )
            .count()
        )
        assert log_count == 3


def test_batch_sign_unknown_summary_in_list(client_with_db):
    """list 含不存在的 summary_id → 該筆 failed，其他成功。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer6", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        cycle, ids = _seed_n_summaries(s, 2, SummaryStatus.DRAFT)
        cycle_id = cycle.id
    _login(client, "reviewer6")
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": ids + [99999], "stage": "SUPERVISOR"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["succeeded"]) == sorted(ids)
    assert any(f["summary_id"] == 99999 for f in body["failed"])


def test_batch_sign_locks_in_sorted_id_order_regardless_of_input(client_with_db):
    """ABBA 防呆（行為層）：傳入反序 summary_ids，逐筆取鎖（處理）順序仍為 id 升冪。

    succeeded 依處理順序 append（= 逐筆 with_for_update 取鎖順序）；反序輸入下仍回升冪，
    證明鎖以 sorted(id) 取得 → 兩批次重疊反序不會 ABBA 死鎖。此案為 Python 迴圈順序
    （非 PG 鎖序），SQLite 即可重現。
    """
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer_order", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        cycle, ids = _seed_n_summaries(s, 3, SummaryStatus.DRAFT)
        cycle_id = cycle.id
    _login(client, "reviewer_order")
    reversed_ids = list(reversed(ids))  # 反序輸入
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": reversed_ids, "stage": "SUPERVISOR"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["failed"] == []
    assert body["succeeded"] == sorted(ids), (
        f"反序輸入 {reversed_ids} 下 succeeded 應為升冪 {sorted(ids)}（鎖以 sorted id 取得），"
        f"實得 {body['succeeded']}"
    )
