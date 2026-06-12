"""考核年終 payout router 測試。

使用 FastAPI + TestClient 直接起 app，
透過 /api/auth/login 取得 cookie，再打 /api/year_end/appraisal-payout/* 端點。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module  # noqa: E402
from api.auth import _account_failures, _ip_attempts  # noqa: E402
from api.auth import router as auth_router  # noqa: E402
from api.year_end import year_end_router  # noqa: E402
from models.appraisal import (  # noqa: E402
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
    CycleStatus,
    Grade,
    RoleGroup,
    Semester,
    SummaryStatus,
)
from models.database import Base  # noqa: E402
from models.employee import Employee  # noqa: E402
from models.year_end import SpecialBonusItem, YearEndCycle  # noqa: E402
from utils.auth import hash_password  # noqa: E402
from utils.permissions import Permission  # noqa: E402

PREFIX = "/api/year_end/appraisal-payout"

# 需要 APPRAISAL_FINALIZE 的使用者
FINALIZE_PERM = ["APPRAISAL_FINALIZE"]
# 只有一個無關權限的使用者（YEAR_END_READ 不含 APPRAISAL_FINALIZE）
VIEWER_PERM = ["YEAR_END_READ"]


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "payout-router-test.sqlite"
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
    app.include_router(year_end_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_users(sf):
    """建 admin (APPRAISAL_FINALIZE) 與 viewer (YEAR_END_READ only) 帳號。"""
    from models.database import User

    with sf() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=FINALIZE_PERM,
                is_active=True,
            )
        )
        s.add(
            User(
                username="viewer",
                password_hash=hash_password("TempPass123"),
                role="staff",
                permission_names=VIEWER_PERM,
                is_active=True,
            )
        )
        # 教師帳號即使被誤發 APPRAISAL_FINALIZE，也不得撞管理端 payout API
        # （對齊 2026-06-04 滲透測試 #1：管理端一律 require_staff_permission）
        s.add(
            User(
                username="teacher",
                password_hash=hash_password("TempPass123"),
                role="teacher",
                permission_names=FINALIZE_PERM,
                is_active=True,
            )
        )
        s.flush()
        s.commit()


def _seed_cycles_and_summaries(sf):
    """建兩個 AppraisalCycle + 1 ACTIVE 員工 + finalized summary（兩 cycle 各一筆）。
    決策②：抓前一完整學年 113上(FIRST) + 113下(SECOND)。
    """
    with sf() as s:
        earlier = AppraisalCycle(
            academic_year=113,
            semester=Semester.FIRST,
            start_date=date(2024, 8, 1),
            end_date=date(2025, 1, 31),
            base_score_calc_date=date(2024, 9, 15),
            base_score=Decimal("100"),
            status=CycleStatus.CLOSED,
        )
        later = AppraisalCycle(
            academic_year=113,
            semester=Semester.SECOND,
            start_date=date(2025, 2, 1),
            end_date=date(2025, 7, 31),
            base_score_calc_date=date(2025, 2, 15),
            base_score=Decimal("100"),
            status=CycleStatus.CLOSED,
        )
        s.add_all([earlier, later])
        s.flush()

        emp = Employee(
            employee_id="E_T6_001",
            name="林老師",
            id_number="A666666666",
            hire_date=date(2024, 8, 1),
            is_active=True,
        )
        s.add(emp)
        s.flush()

        for cyc, score, grade, amt in [
            (earlier, Decimal("80"), Grade.GOOD, Decimal("6400")),
            (later, Decimal("90"), Grade.OUTSTANDING, Decimal("7200")),
        ]:
            p = AppraisalParticipant(
                cycle_id=cyc.id,
                employee_id=emp.id,
                role_group=RoleGroup.HEAD_TEACHER,
                hire_months_in_cycle=Decimal("6"),
            )
            s.add(p)
            s.flush()
            s.add(
                AppraisalSummary(
                    participant_id=p.id,
                    cycle_id=cyc.id,
                    base_score=Decimal("100"),
                    total_score=score,
                    grade=grade,
                    bonus_amount=amt,
                    status=SummaryStatus.FINALIZED,
                )
            )
        s.flush()
        s.commit()


def _login(client, username="admin"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "TempPass123"}
    )
    assert res.status_code == 200, f"login failed: {res.text}"


# ===== tests =====


def test_preview_returns_rows(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    _seed_cycles_and_summaries(sf)
    _login(client)
    res = client.get(f"{PREFIX}/preview", params={"year": 2026})
    assert res.status_code == 200, res.text
    rows = res.json()
    assert isinstance(rows, list)
    assert len(rows) >= 1
    assert "employee_id" in rows[0]
    assert "total_amount" in rows[0]


def test_preview_missing_year_returns_422(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    _login(client)
    res = client.get(f"{PREFIX}/preview")
    assert res.status_code == 422, res.text


def test_preview_requires_permission(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    _seed_cycles_and_summaries(sf)
    _login(client, "viewer")
    res = client.get(f"{PREFIX}/preview", params={"year": 2026})
    assert res.status_code == 403, res.text


def test_generate_happy_path(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    _seed_cycles_and_summaries(sf)
    _login(client)
    res = client.post(
        f"{PREFIX}/generate",
        json={"year": 2026, "included_inactive_employee_ids": []},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["affected_employee_count"] >= 1
    with sf() as s:
        cycle = s.scalar(select(YearEndCycle).where(YearEndCycle.academic_year == 114))
        assert cycle is not None


def test_generate_idempotent_via_api(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    _seed_cycles_and_summaries(sf)
    _login(client)
    client.post(
        f"{PREFIX}/generate",
        json={"year": 2026, "included_inactive_employee_ids": []},
    )
    with sf() as s:
        first = s.scalar(select(func.count()).select_from(SpecialBonusItem))
    client.post(
        f"{PREFIX}/generate",
        json={"year": 2026, "included_inactive_employee_ids": []},
    )
    with sf() as s:
        second = s.scalar(select(func.count()).select_from(SpecialBonusItem))
    assert first == second


def test_list_returns_generated_items(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    _seed_cycles_and_summaries(sf)
    _login(client)
    client.post(
        f"{PREFIX}/generate",
        json={"year": 2026, "included_inactive_employee_ids": []},
    )
    res = client.get(PREFIX, params={"year": 2026})
    assert res.status_code == 200, res.text
    items = res.json()
    assert len(items) >= 2


def test_teacher_blocked_from_all_payout_endpoints(client_with_db):
    """role=teacher 持 APPRAISAL_FINALIZE 仍應被擋出管理端 payout 4 端點（403）。

    bare require_permission 不擋角色，教師誤持權限即可讀寫全員考核年終
    （金額級資料）；管理端必須走 require_staff_permission。
    """
    client, sf = client_with_db
    _seed_users(sf)
    _seed_cycles_and_summaries(sf)
    _login(client, "teacher")

    res = client.get(f"{PREFIX}/preview", params={"year": 2026})
    assert res.status_code == 403, f"GET /preview 教師應 403，實得 {res.status_code}"

    res = client.post(
        f"{PREFIX}/generate",
        json={"year": 2026, "included_inactive_employee_ids": []},
    )
    assert res.status_code == 403, f"POST /generate 教師應 403，實得 {res.status_code}"

    res = client.get(PREFIX, params={"year": 2026})
    assert res.status_code == 403, f"GET list 教師應 403，實得 {res.status_code}"

    res = client.delete(f"{PREFIX}/2026", params={"confirm": True})
    assert res.status_code == 403, f"DELETE 教師應 403，實得 {res.status_code}"


def test_void_requires_confirm(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    _seed_cycles_and_summaries(sf)
    _login(client)
    client.post(
        f"{PREFIX}/generate",
        json={"year": 2026, "included_inactive_employee_ids": []},
    )
    res_no = client.delete(f"{PREFIX}/2026")
    assert res_no.status_code == 400, res_no.text
    res_ok = client.delete(f"{PREFIX}/2026", params={"confirm": True})
    assert res_ok.status_code == 200, res_ok.text
    assert res_ok.json()["deleted_count"] >= 2
