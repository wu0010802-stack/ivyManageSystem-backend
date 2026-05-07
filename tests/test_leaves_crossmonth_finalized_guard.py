"""跨月假單封存月份守衛回歸測試。

Bug 描述：
    `_check_salary_months_not_finalized` 在 4 個入口都只傳入 start_date 月份，
    但 `lock_and_premark_stale` 卻迴圈完整跨月區間（approve_leave 路徑明證）。
    若假單跨多月（例 2026-01-31 ~ 2026-03-01），且中間月份（2 月）或 end_date 月份
    已封存，原 check 看不到 → 放行；事後鎖延伸/重算被 finalize 守衛擋下，
    但假單已 commit → DB 進入「假單翻面、薪資未動」的矛盾狀態
    （正是 `_check_salary_months_not_finalized` docstring 自己宣告要避免的情境）。

修復方式：
    四處統一改為 `_collect_leave_months(leave.start_date, leave.end_date)`，
    確保 check 範圍與 lock/recalc 範圍對齊。

Schema 驗證註：
    LeaveCreate / LeaveUpdate / import_leaves 三條路徑都已擋跨月，
    本測試直接以 ORM 寫入跨月假單，模擬「歷史資料、直接 DB 寫入、未來新路徑」
    場景下的防禦縱深 — 即使資料層出現跨月假單，APP 層仍須守住一致性。
"""

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from models.database import (
    Base,
    Employee,
    LeaveRecord,
    SalaryRecord,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "crossmonth-finalize.sqlite"
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

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _emp(session, employee_id: str, name: str) -> Employee:
    e = Employee(employee_id=employee_id, name=name, base_salary=36000, is_active=True)
    session.add(e)
    session.flush()
    return e


def _admin(session, *, employee=None) -> User:
    """建立純管理員（預設無 employee_id，避免觸發自我核准守衛）。"""
    u = User(
        employee_id=employee.id if employee else None,
        username="hr_admin",
        password_hash=hash_password("AdminPass123"),
        role="admin",
        permissions=-1,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _make_finalized_salary(session, employee_id: int, year: int, month: int):
    """造一筆封存薪資記錄（is_finalized=True）。"""
    rec = SalaryRecord(
        employee_id=employee_id,
        salary_year=year,
        salary_month=month,
        is_finalized=True,
        finalized_by="HR",
        finalized_at=datetime(year, month, 28),
    )
    session.add(rec)
    session.flush()
    return rec


def _make_crossmonth_leave(
    session,
    employee_id: int,
    start: date,
    end: date,
    *,
    is_approved=None,
    leave_hours: float = 4.0,
):
    """繞過 Pydantic 驗證，直接以 ORM 建立跨月假單（模擬歷史資料/未來路徑漏洞）。

    附件預設掛上 fake path：跨多月會超過 2 天附件門檻，附件不是本測試焦點。
    """
    lv = LeaveRecord(
        employee_id=employee_id,
        leave_type="personal",
        start_date=start,
        end_date=end,
        leave_hours=leave_hours,
        is_approved=is_approved,
        is_deductible=True,
        deduction_ratio=1.0,
        attachment_paths='["fake-evidence.png"]',
    )
    session.add(lv)
    session.flush()
    return lv


# ── 路徑 1：approve_leave（單筆核准） ───────────────────────────────────────


class TestApproveLeaveCrossMonthFinalizedGuard:
    """跨月假單核准時，end_date 月或中間月份封存應 409。"""

    def test_approve_blocks_when_end_month_finalized(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session, "X001", "跨月教師")
            _admin(session)
            # 跨月假單 1/31 ~ 3/1，pending 狀態
            lv = _make_crossmonth_leave(
                session, emp.id, date(2026, 1, 31), date(2026, 3, 1)
            )
            # 只封存 end_date 月份（3 月），1 月與 2 月未封存
            _make_finalized_salary(session, emp.id, 2026, 3)
            session.commit()
            lv_id = lv.id

        assert _login(client, "hr_admin", "AdminPass123").status_code == 200

        res = client.put(
            f"/api/leaves/{lv_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 409, (
            "end_date 月份（3 月）已封存，跨月假單核准應被擋；"
            f"實際 status={res.status_code} body={res.json()}"
        )
        assert "封存" in res.json()["detail"]

    def test_approve_blocks_when_middle_month_finalized(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session, "X002", "跨三月教師")
            _admin(session)
            # 假單橫跨 1、2、3 月
            lv = _make_crossmonth_leave(
                session, emp.id, date(2026, 1, 31), date(2026, 3, 1)
            )
            # 只封存「中間月份」（2 月）
            _make_finalized_salary(session, emp.id, 2026, 2)
            session.commit()
            lv_id = lv.id

        assert _login(client, "hr_admin", "AdminPass123").status_code == 200

        res = client.put(
            f"/api/leaves/{lv_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 409, (
            "中間月份（2 月）已封存，跨月假單核准應被擋；"
            f"實際 status={res.status_code} body={res.json()}"
        )


# ── 路徑 2：update_leave（管理端編輯，已核准退審） ────────────────────────


class TestUpdateLeaveCrossMonthFinalizedGuard:
    """已核准跨月假單編輯時，原跨月區間任一月份封存應 409。"""

    def test_update_blocks_when_orig_end_month_finalized(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session, "X003", "編輯教師")
            _admin(session)
            # 已核准的跨月假單 1/31 ~ 3/1
            lv = _make_crossmonth_leave(
                session,
                emp.id,
                date(2026, 1, 31),
                date(2026, 3, 1),
                is_approved=True,
            )
            # 封存原 end_date 月（3 月）
            _make_finalized_salary(session, emp.id, 2026, 3)
            session.commit()
            lv_id = lv.id

        assert _login(client, "hr_admin", "AdminPass123").status_code == 200

        # 嘗試把假單改到 4 月（單月，schema 允許），但原跨月區間中 3 月已封存
        res = client.put(
            f"/api/leaves/{lv_id}",
            json={"start_date": "2026-04-10", "end_date": "2026-04-10"},
        )
        assert res.status_code == 409, (
            "原跨月區間的 end_date 月（3 月）已封存，編輯應被擋；"
            f"實際 status={res.status_code} body={res.json()}"
        )


# ── 路徑 3：delete_leave（刪除已核准跨月假單） ────────────────────────────


class TestDeleteLeaveCrossMonthFinalizedGuard:
    """刪除已核准跨月假單時，end_date 月或中間月份封存應 409。"""

    def test_delete_blocks_when_end_month_finalized(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session, "X004", "刪除教師")
            _admin(session)
            lv = _make_crossmonth_leave(
                session,
                emp.id,
                date(2026, 1, 31),
                date(2026, 3, 1),
                is_approved=True,
            )
            _make_finalized_salary(session, emp.id, 2026, 3)
            session.commit()
            lv_id = lv.id

        assert _login(client, "hr_admin", "AdminPass123").status_code == 200

        res = client.delete(f"/api/leaves/{lv_id}")
        assert res.status_code == 409, (
            "end_date 月（3 月）已封存，刪除應被擋；"
            f"實際 status={res.status_code} body={res.json()}"
        )


# ── 路徑 4：batch_approve（批次核准） ─────────────────────────────────────


class TestBatchApproveLeaveCrossMonthFinalizedGuard:
    """批次核准跨月假單時，end_date 月或中間月份封存應記錄為失敗條目。"""

    def test_batch_approve_marks_failure_when_end_month_finalized(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session, "X005", "批次教師")
            _admin(session)
            lv = _make_crossmonth_leave(
                session, emp.id, date(2026, 1, 31), date(2026, 3, 1)
            )
            _make_finalized_salary(session, emp.id, 2026, 3)
            session.commit()
            lv_id = lv.id

        assert _login(client, "hr_admin", "AdminPass123").status_code == 200

        res = client.post(
            "/api/leaves/batch-approve",
            json={"ids": [lv_id], "approved": True},
        )
        # 批次核准的 finalize 守衛失敗會將該條歸入 failed
        assert res.status_code == 200, res.json()
        body = res.json()
        # 結構視實作可能為 {"approved": [], "failed": [...]} 或類似
        # 至少斷言：此假單沒進入成功列表
        succeeded_ids = [
            item.get("id") if isinstance(item, dict) else item
            for item in body.get("succeeded", [])
        ]
        failed_items = body.get("failed", [])
        failed_ids = [
            item.get("id") if isinstance(item, dict) else item for item in failed_items
        ]
        assert lv_id not in succeeded_ids, (
            "end_date 月（3 月）已封存，批次核准不該將此假單標為成功；" f"body={body}"
        )
        assert lv_id in failed_ids, (
            "封存月份保護應將跨月假單放入 failed；" f"body={body}"
        )
