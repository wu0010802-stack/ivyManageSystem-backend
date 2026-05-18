"""batch_sign 中途 DB 錯誤不應牽連其他成功筆（P1-1）。

威脅：原本 batch_sign 沒用 nested savepoint，當某筆寫 log 或 update 觸發
SQLAlchemy 例外（IntegrityError / OperationalError）後，整個 session 進入
PendingRollbackError 狀態 → 後續 .commit() 失敗 → 已成功的筆也一起 rollback。

修補策略：每 iteration 用 with session.begin_nested() 包，單筆失敗只 rollback
該 savepoint，不污染外層 session。

fixture pattern 沿用 test_appraisal_batch_sign_endpoint.py。
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
    SummaryStatus,
)
from models.auth import User
from models.database import Base
from models.employee import Employee, EmployeeType
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-batch-sign-partial.sqlite"
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
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=int(perms),
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
        s.add(summary)
        s.flush()
        ids.append(summary.id)
    s.commit()
    return cycle, ids


def test_batch_sign_db_error_does_not_rollback_other_rows(client_with_db, monkeypatch):
    """中間筆 write_summary_log 拋例外，前後筆應仍 commit 成 SUPERVISOR_SIGNED。

    模擬手法：monkey-patch write_summary_log，僅在 ids[1] 觸發時拋例外，
    其他正常呼叫真正的 helper。原本 batch_sign 沒包 savepoint 時，這個例外
    會被外層 try/except 接住放進 failed[]，但 SQLAlchemy session 已被污染，
    後續 commit() 會將 ids[0] 的更新一起 rollback（或 session 直接掛掉）。
    """
    client, sf = client_with_db
    with sf() as s:
        _create_user(
            s,
            "reviewer1",
            Permission.APPRAISAL_READ | Permission.APPRAISAL_REVIEW,
        )
        s.commit()
        cycle, ids = _seed_n_summaries(s, 3, SummaryStatus.DRAFT)
        cycle_id = cycle.id

    # patch：只在 summary.id == ids[1] 時拋 IntegrityError
    from sqlalchemy.exc import IntegrityError

    import api.appraisal as appraisal_mod

    real_write_log = appraisal_mod.write_summary_log

    def fake_write_log(session, summary, *args, **kwargs):
        if summary.id == ids[1]:
            raise IntegrityError("forced for test", None, Exception("boom"))
        return real_write_log(session, summary, *args, **kwargs)

    monkeypatch.setattr(appraisal_mod, "write_summary_log", fake_write_log)

    _login(client, "reviewer1")
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": ids, "stage": "SUPERVISOR"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["succeeded"]) == sorted([ids[0], ids[2]])
    assert len(body["failed"]) == 1
    assert body["failed"][0]["summary_id"] == ids[1]

    # 載入 DB 驗證：ids[0] 與 ids[2] 真的進入 SUPERVISOR_SIGNED（不被 ids[1]
    # 牽連 rollback）
    with sf() as s:
        ok_rows = (
            s.query(AppraisalSummary)
            .filter(AppraisalSummary.id.in_([ids[0], ids[2]]))
            .all()
        )
        assert len(ok_rows) == 2
        assert all(r.status == SummaryStatus.SUPERVISOR_SIGNED for r in ok_rows)

        # ids[1] 仍保持 DRAFT（savepoint rollback）
        bad = s.get(AppraisalSummary, ids[1])
        assert bad.status == SummaryStatus.DRAFT

        # 成功筆有 log；失敗筆無 log（savepoint rollback 把 log INSERT 也撤了）
        log_count = (
            s.query(AppraisalSummaryLog)
            .filter(AppraisalSummaryLog.summary_id.in_(ids))
            .count()
        )
        assert log_count == 2
