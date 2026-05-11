"""2026-05-11 leaves/overtimes 14 條 bug batch 修補的回歸測試。

每個測試對應一條 finding；測試命名格式：
    test_p<priority>_<n>_<short_description>

修補完成後此檔須全綠。所有測試共用 app_client fixture（in-memory SQLite + mini app）。
"""

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import api.overtimes as overtimes_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from models.database import (
    Base,
    Employee,
    LeaveRecord,
    OvertimeRecord,
    SalaryRecord,
    User,
)
from utils.auth import hash_password

# ────────────────────────────────────────────────────────────────────────────
# 共用 fixture
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """In-memory SQLite + mini FastAPI app（含 auth/leaves/overtimes router）。"""
    db_path = tmp_path / "bug-batch.sqlite"
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
    _ip_attempts.clear()
    _account_failures.clear()

    fake_salary_engine = MagicMock()
    monkeypatch.setattr(leaves_module, "_salary_engine", fake_salary_engine)
    monkeypatch.setattr(overtimes_module, "_salary_engine", fake_salary_engine)
    monkeypatch.setattr(leaves_module, "_line_service", None)
    monkeypatch.setattr(overtimes_module, "_line_service", None)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)
    app.include_router(overtimes_router)

    with TestClient(app) as client:
        yield client, session_factory, monkeypatch

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _emp(session, employee_id: str, name: str, is_active: bool = True) -> Employee:
    e = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=is_active,
    )
    session.add(e)
    session.flush()
    return e


def _admin(session, *, employee=None, username: str = "hr_admin") -> User:
    """純管理員（預設無 employee_id，避免觸發自我核准守衛）。"""
    u = User(
        employee_id=employee.id if employee else None,
        username=username,
        password_hash=hash_password("AdminPass123"),
        role="admin",
        permissions=-1,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(
    client: TestClient, username: str = "hr_admin", password: str = "AdminPass123"
):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _pending_leave(
    session,
    employee_id: int,
    *,
    start: date = date(2026, 6, 1),
    end: date | None = None,
    leave_type: str = "personal",
    leave_hours: float = 8.0,
    start_time: str | None = None,
    end_time: str | None = None,
) -> LeaveRecord:
    lv = LeaveRecord(
        employee_id=employee_id,
        leave_type=leave_type,
        start_date=start,
        end_date=end or start,
        leave_hours=leave_hours,
        start_time=start_time,
        end_time=end_time,
        is_approved=None,
        is_deductible=True,
        deduction_ratio=1.0,
    )
    session.add(lv)
    session.flush()
    return lv


def _ot_dt(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m)


def _approved_overtime(
    session,
    employee_id: int,
    *,
    overtime_date: date = date(2026, 6, 10),
    start_time: str = "18:00",
    end_time: str = "20:00",
    hours: float = 2.0,
    use_comp_leave: bool = False,
    is_approved: bool | None = True,
) -> OvertimeRecord:
    ot = OvertimeRecord(
        employee_id=employee_id,
        overtime_date=overtime_date,
        start_time=_ot_dt(overtime_date, start_time),
        end_time=_ot_dt(overtime_date, end_time),
        hours=hours,
        overtime_type="weekday",
        is_approved=is_approved,
        use_comp_leave=use_comp_leave,
        comp_leave_granted=use_comp_leave and is_approved is True,
    )
    session.add(ot)
    session.flush()
    return ot


# ────────────────────────────────────────────────────────────────────────────
# Task A — P0-1: batch_approve two-pass 驗證
# ────────────────────────────────────────────────────────────────────────────


class TestP0_1BatchApproveTwoPass:
    """Phase 1 catch-all rollback 不應抹掉同 batch 其他驗證通過條目的 setattr/log。

    修補前的行為：
      第二筆驗證階段拋出非 HTTPException → session.rollback() + expire_all()
      → 第一筆已 setattr 的 is_approved=True 被 rollback；Phase 2 commit 變 no-op
      → 但 succeeded 仍含第一筆，回傳體與 DB 脫鉤（silent data loss）

    修補後（two-pass）：
      Pass 1 純驗證收集 validated_ids，setattr 全部移到 Pass 2 → catch-all rollback
      時 Pass 1 無 dirty state，前面條目不受影響。
    """

    def test_partial_validation_failure_does_not_silently_succeed(self, app_client):
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp1 = _emp(session, "B001", "員工一")
            emp2 = _emp(session, "B002", "員工二")
            _admin(session)
            lv1 = _pending_leave(session, emp1.id)
            lv2 = _pending_leave(session, emp2.id, start=date(2026, 6, 5))
            session.commit()
            lv1_id, lv2_id = lv1.id, lv2.id

        assert _login(client).status_code == 200

        # 讓 _write_approval_log 第二筆觸發 RuntimeError；第一筆正常
        original_write = leaves_module._write_approval_log
        seen_ids: list[int] = []

        def fake_write(entity_type, entity_id, action, current_user, reason, session):
            seen_ids.append(entity_id)
            if entity_id == lv2_id:
                raise RuntimeError("simulated unexpected failure")
            return original_write(
                entity_type, entity_id, action, current_user, reason, session
            )

        mp.setattr(leaves_module, "_write_approval_log", fake_write)

        res = client.post(
            "/api/leaves/batch-approve",
            json={"ids": [lv1_id, lv2_id], "approved": True},
        )
        # 不論 HTTP status，回傳體應一致：
        body = res.json()
        succeeded = set(body.get("succeeded") or [])
        failed_ids = {f.get("id") for f in (body.get("failed") or [])}

        # 重新讀 DB 確認 lv1 狀態
        with session_factory() as session:
            lv1_db = session.get(LeaveRecord, lv1_id)
            lv2_db = session.get(LeaveRecord, lv2_id)

        # 核心斷言：lv1 若在 succeeded，DB 必須真的核准；不允許 succeeded 與 DB 脫鉤
        if lv1_id in succeeded:
            assert (
                lv1_db.is_approved is True
            ), f"silent data loss: lv1 在 succeeded 但 DB is_approved={lv1_db.is_approved}"
        # lv2 必定 failed
        assert lv2_id in failed_ids, f"lv2 應被視為失敗；body={body}"
        # lv2 不應被部分套用
        assert (
            lv2_db.is_approved is None
        ), f"lv2 失敗但 is_approved={lv2_db.is_approved}（部分套用）"

    def test_overtime_batch_partial_failure_consistent(self, app_client):
        """overtimes 同 pattern 的 batch_approve 也須一致行為。"""
        client, session_factory, mp = app_client
        with session_factory() as session:
            emp1 = _emp(session, "B011", "OT 員工一")
            emp2 = _emp(session, "B012", "OT 員工二")
            _admin(session)
            ot1 = _approved_overtime(
                session, emp1.id, overtime_date=date(2026, 6, 1), is_approved=None
            )
            ot2 = _approved_overtime(
                session, emp2.id, overtime_date=date(2026, 6, 2), is_approved=None
            )
            session.commit()
            ot1_id, ot2_id = ot1.id, ot2.id

        assert _login(client).status_code == 200

        original_write = overtimes_module._write_approval_log

        def fake_write(entity_type, entity_id, action, current_user, reason, session):
            if entity_id == ot2_id:
                raise RuntimeError("simulated")
            return original_write(
                entity_type, entity_id, action, current_user, reason, session
            )

        mp.setattr(overtimes_module, "_write_approval_log", fake_write)

        res = client.post(
            "/api/overtimes/batch-approve",
            json={"ids": [ot1_id, ot2_id], "approved": True},
        )
        body = res.json()
        succeeded = set(body.get("succeeded") or [])
        failed_ids = {f.get("id") for f in (body.get("failed") or [])}

        with session_factory() as session:
            ot1_db = session.get(OvertimeRecord, ot1_id)
            ot2_db = session.get(OvertimeRecord, ot2_id)

        if ot1_id in succeeded:
            assert (
                ot1_db.is_approved is True
            ), f"silent data loss: ot1 succeeded 但 DB is_approved={ot1_db.is_approved}"
        assert ot2_id in failed_ids
        assert ot2_db.is_approved is None


# ────────────────────────────────────────────────────────────────────────────
# Task B — P0-2: Portal 病假繞過勞基雙配額
# ────────────────────────────────────────────────────────────────────────────


import types as _types
from unittest.mock import patch as _patch


def _portal_emp():
    e = _types.SimpleNamespace()
    e.id = 99
    e.name = "Portal 教師"
    e.base_salary = 30000
    e.hire_date = date(2020, 1, 1)
    return e


class TestP0_2PortalSickStatutoryCap:
    """Portal sick 必須走 _guard_leave_quota（呼叫 assert_sick_leave_within_statutory_caps）。

    修補前：portal/leaves.py:312-326 sick 分支只走 _check_quota（看 LeaveQuota 總量），
    LeaveQuota 未初始化時直接 return，雙桶（未住院 240h / 住院 2080h / 合計 2080h）
    完全繞過。
    """

    def _build_payload(
        self, *, leave_type: str = "sick", hours: float = 4.0, is_hosp: bool = False
    ):
        from api.portal._shared import LeaveCreatePortal

        return LeaveCreatePortal(
            leave_type=leave_type,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 1),
            leave_hours=hours,
            reason="生病",
            is_hospitalized=is_hosp,
        )

    def _common_patches(self, emp):
        from api.portal import leaves as portal_lv

        session = MagicMock()
        return session, [
            _patch.object(portal_lv, "get_session", return_value=session),
            _patch.object(portal_lv, "_get_employee", return_value=emp),
            _patch.object(portal_lv, "_check_overlap", return_value=None),
            _patch.object(portal_lv, "_check_substitute_leave_conflict"),
            _patch.object(portal_lv, "validate_leave_hours_against_schedule"),
            _patch.object(portal_lv, "_check_leave_limits"),
            _patch.object(portal_lv, "validate_portal_leave_rules"),
        ]

    def test_sick_dispatched_to_guard_leave_quota(self):
        """portal sick 必須呼叫 _guard_leave_quota（觸發雙桶檢查）"""
        from api.portal import leaves as portal_lv

        emp = _portal_emp()
        session, patches = self._common_patches(emp)
        for p in patches:
            p.start()
        try:
            with (
                _patch.object(portal_lv, "_guard_leave_quota") as mock_guard,
                _patch.object(portal_lv, "_check_quota") as mock_quota,
            ):
                try:
                    portal_lv.create_my_leave(
                        data=self._build_payload(),
                        request=MagicMock(),
                        current_user={"username": "t", "employee_id": 99},
                    )
                except Exception:
                    pass
            assert mock_guard.called, "portal sick 必須走 _guard_leave_quota"
        finally:
            for p in patches:
                p.stop()

    def test_sick_outpatient_241h_blocked_by_statutory_cap(self):
        """未住院 sick 已用 240h，再申請 1h 應被擋（勞工請假規則第 4 條）"""
        from api.portal import leaves as portal_lv

        emp = _portal_emp()
        session, patches = self._common_patches(emp)
        for p in patches:
            p.start()
        try:
            # 已用 240h 未住院 sick，年度上限 240h
            with (
                _patch(
                    "api.leaves._get_sick_committed_hours",
                    side_effect=lambda s, eid, year, is_hospitalized, exclude_id=None: (
                        240.0 if not is_hospitalized else 0.0
                    ),
                ),
            ):
                with pytest.raises(HTTPException) as exc:
                    portal_lv.create_my_leave(
                        data=self._build_payload(hours=1.0, is_hosp=False),
                        request=MagicMock(),
                        current_user={"username": "t", "employee_id": 99},
                    )
            assert exc.value.status_code == 400
            assert "勞工請假規則" in exc.value.detail or "未住院" in exc.value.detail
        finally:
            for p in patches:
                p.stop()
