"""教師自助考核端點測試 — 對齊 portal/salary 隱私模式。"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.portal.appraisal import router as portal_appraisal_router
from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalScoreItemCatalog,
    AppraisalSummary,
    CycleStatus,
    Grade,
    RoleGroup,
    ScoreItemSign,
    Semester,
    SummaryStatus,
)
from models.database import Base
from models.auth import User
from models.employee import Employee, EmployeeType
from utils.auth import hash_password


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "portal-appraisal.sqlite"
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
    app.include_router(portal_appraisal_router, prefix="/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_employee(session, name="王老師"):
    # NOTE: 工號 employee_id 為 String(20) unique not-null，必須給；
    # EmployeeType 只有 REGULAR / HOURLY，本案用 REGULAR.value（plan 原文寫
    # HEAD_TEACHER 不存在於 EmployeeType，已修正）
    h = abs(hash(name))
    emp = Employee(
        employee_id=f"E{h % 100000000:08d}",
        name=name,
        id_number=f"A{h % 100000000:08d}",
        employee_type=EmployeeType.REGULAR.value,
        hire_date=date(2023, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user_for_employee(session, emp, username):
    user = User(
        username=username,
        password_hash=hash_password("TempPass123"),
        role="teacher",
        permissions=0,
        employee_id=emp.id,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    """登入並取 access_token（從 set_access_token_cookie 設的 httpOnly cookie 取出）。"""
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token = resp.cookies.get("access_token")
    assert token, f"login response 未設置 access_token cookie: {resp.cookies}"
    return token


def _make_cycle(session, year=114, semester=Semester.FIRST):
    cycle = AppraisalCycle(
        academic_year=year,
        semester=semester,
        start_date=date(2025, 8, 1) if semester == Semester.FIRST else date(2026, 2, 1),
        end_date=date(2026, 1, 31) if semester == Semester.FIRST else date(2026, 7, 31),
        base_score_calc_date=(
            date(2025, 9, 15) if semester == Semester.FIRST else date(2026, 3, 15)
        ),
        base_score=Decimal("70"),
        enrollment_target=100,
        enrollment_actual=70,
        status=CycleStatus.CLOSED,
    )
    session.add(cycle)
    session.flush()
    return cycle


def _make_participant_with_summary(
    session,
    cycle,
    emp,
    status=SummaryStatus.FINALIZED,
    grade=Grade.GOOD,
    total_score=Decimal("85"),
    bonus=Decimal("5000"),
):
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    session.add(p)
    session.flush()
    s = AppraisalSummary(
        participant_id=p.id,
        cycle_id=cycle.id,
        base_score=Decimal("70"),
        event_score_sum=total_score - Decimal("70"),
        total_score=total_score,
        grade=grade,
        bonus_amount=bonus,
        status=status,
    )
    if status == SummaryStatus.FINALIZED:
        s.finalized_at = datetime.now(timezone.utc)
    session.add(s)
    session.flush()
    return p, s


def test_my_appraisals_list_finalized_returns_scores(client_with_db):
    client, sf = client_with_db
    with sf() as session:
        emp = _make_employee(session, "王老師")
        _make_user_for_employee(session, emp, "wang")
        cycle = _make_cycle(session)
        _make_participant_with_summary(session, cycle, emp)
        session.commit()

    token = _login(client, "wang")
    resp = client.get(
        "/portal/my-appraisals",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["is_visible"] is True
    assert item["summary_status"] == "FINALIZED"
    assert Decimal(str(item["total_score"])) == Decimal("85")
    assert item["grade"] == "GOOD"
    assert Decimal(str(item["bonus_amount"])) == Decimal("5000")


def test_my_appraisals_list_draft_masks_scores(client_with_db):
    """DRAFT/SUPERVISOR_SIGNED/ACCOUNTING_SIGNED → total_score/grade/bonus_amount = None。"""
    client, sf = client_with_db
    with sf() as session:
        emp = _make_employee(session, "李老師")
        _make_user_for_employee(session, emp, "lee")
        for i, status in enumerate(
            [
                SummaryStatus.DRAFT,
                SummaryStatus.SUPERVISOR_SIGNED,
                SummaryStatus.ACCOUNTING_SIGNED,
            ]
        ):
            cycle = _make_cycle(session, year=112 + i, semester=Semester.FIRST)
            _make_participant_with_summary(
                session,
                cycle,
                emp,
                status=status,
                total_score=Decimal("90"),
                bonus=Decimal("8000"),
            )
        session.commit()

    token = _login(client, "lee")
    resp = client.get(
        "/portal/my-appraisals",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    for item in items:
        assert item["is_visible"] is False
        assert item["total_score"] is None
        assert item["grade"] is None
        assert item["bonus_amount"] is None
        assert item["summary_status"] in (
            "DRAFT",
            "SUPERVISOR_SIGNED",
            "ACCOUNTING_SIGNED",
        )


def test_my_appraisals_list_rejected_masks_scores(client_with_db):
    """rejected_at IS NOT NULL → 即使 status=FINALIZED 也不顯示。"""
    client, sf = client_with_db
    with sf() as session:
        emp = _make_employee(session, "張老師")
        _make_user_for_employee(session, emp, "zhang")
        cycle = _make_cycle(session)
        _, summary = _make_participant_with_summary(
            session, cycle, emp, status=SummaryStatus.FINALIZED
        )
        summary.rejected_at = datetime.now(timezone.utc)
        summary.rejected_reason = "test"
        session.commit()

    token = _login(client, "zhang")
    resp = client.get(
        "/portal/my-appraisals",
        headers={"Authorization": f"Bearer {token}"},
    )
    items = resp.json()["items"]
    assert items[0]["is_rejected"] is True
    assert items[0]["is_visible"] is False
    assert items[0]["total_score"] is None


def test_my_appraisals_trend_only_finalized(client_with_db):
    """trend 只回 FINALIZED 期，按時間 ASC。"""
    client, sf = client_with_db
    with sf() as session:
        emp = _make_employee(session, "陳老師")
        _make_user_for_employee(session, emp, "chen")
        # 113 上 FINALIZED 85 分
        c1 = _make_cycle(session, year=113, semester=Semester.FIRST)
        _make_participant_with_summary(
            session,
            c1,
            emp,
            status=SummaryStatus.FINALIZED,
            grade=Grade.GOOD,
            total_score=Decimal("85"),
        )
        # 113 下 DRAFT（要被過濾掉）
        c2 = _make_cycle(session, year=113, semester=Semester.SECOND)
        _make_participant_with_summary(
            session,
            c2,
            emp,
            status=SummaryStatus.DRAFT,
            total_score=Decimal("70"),
        )
        # 114 上 FINALIZED 92 分
        c3 = _make_cycle(session, year=114, semester=Semester.FIRST)
        _make_participant_with_summary(
            session,
            c3,
            emp,
            status=SummaryStatus.FINALIZED,
            grade=Grade.OUTSTANDING,
            total_score=Decimal("92"),
        )
        session.commit()

    token = _login(client, "chen")
    resp = client.get(
        "/portal/my-appraisals/trend",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    points = resp.json()["points"]
    assert len(points) == 2  # DRAFT 被過濾
    assert points[0]["academic_year"] == 113
    assert points[0]["semester"] == "FIRST"
    assert Decimal(str(points[0]["total_score"])) == Decimal("85")
    assert points[0]["label"] == "113上"
    assert points[1]["academic_year"] == 114
    assert points[1]["label"] == "114上"


def test_my_appraisals_detail_returns_items_in_catalog_order(client_with_db):
    """detail 回 score_items 按 catalog.display_order 排序，含 label。"""
    client, sf = client_with_db
    with sf() as session:
        emp = _make_employee(session, "趙老師")
        _make_user_for_employee(session, emp, "zhao")
        # 建 catalog 兩條（display_order 故意顛倒插入）
        c_late = AppraisalScoreItemCatalog(
            code="LATE_EARLY",
            label="遲到早退",
            sign=ScoreItemSign.NEGATIVE,
            default_weight=Decimal("1"),
            display_order=2,
        )
        c_after = AppraisalScoreItemCatalog(
            code="AFTER_CLASS_RATE",
            label="課後留校率",
            sign=ScoreItemSign.POSITIVE,
            default_weight=Decimal("1"),
            display_order=1,
        )
        session.add_all([c_late, c_after])
        session.flush()
        cycle = _make_cycle(session)
        p, _ = _make_participant_with_summary(
            session, cycle, emp, status=SummaryStatus.FINALIZED
        )
        session.add_all(
            [
                AppraisalScoreItem(
                    participant_id=p.id,
                    cycle_id=cycle.id,
                    catalog_id=c_late.id,
                    item_code="LATE_EARLY",
                    score_delta=Decimal("-2"),
                ),
                AppraisalScoreItem(
                    participant_id=p.id,
                    cycle_id=cycle.id,
                    catalog_id=c_after.id,
                    item_code="AFTER_CLASS_RATE",
                    score_delta=Decimal("3"),
                ),
            ]
        )
        session.commit()
        cycle_id = cycle.id

    token = _login(client, "zhao")
    resp = client.get(
        f"/portal/my-appraisals/{cycle_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["cycle_id"] == cycle_id
    items = data["score_items"]
    assert len(items) == 2
    # 按 display_order：AFTER_CLASS_RATE (1) 在前，LATE_EARLY (2) 在後
    assert items[0]["item_code"] == "AFTER_CLASS_RATE"
    assert items[0]["label"] == "課後留校率"
    assert items[0]["sign"] == "POSITIVE"
    assert items[1]["item_code"] == "LATE_EARLY"


def test_my_appraisals_detail_blocks_non_finalized(client_with_db):
    """非 FINALIZED 取 detail → 403。"""
    client, sf = client_with_db
    with sf() as session:
        emp = _make_employee(session, "錢老師")
        _make_user_for_employee(session, emp, "qian")
        cycle = _make_cycle(session)
        _make_participant_with_summary(session, cycle, emp, status=SummaryStatus.DRAFT)
        session.commit()
        cycle_id = cycle.id

    token = _login(client, "qian")
    resp = client.get(
        f"/portal/my-appraisals/{cycle_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "考核進行中" in detail or "尚未公布" in detail


def test_my_appraisals_other_employees_invisible(client_with_db):
    """A 教師不能用 cycle_id 拿到 B 教師的細節。"""
    client, sf = client_with_db
    with sf() as session:
        emp_a = _make_employee(session, "甲老師")
        emp_b = _make_employee(session, "乙老師")
        _make_user_for_employee(session, emp_a, "alice")
        _make_user_for_employee(session, emp_b, "bob")
        cycle = _make_cycle(session)
        _make_participant_with_summary(
            session, cycle, emp_b, status=SummaryStatus.FINALIZED
        )
        session.commit()
        cycle_id = cycle.id

    token = _login(client, "alice")
    resp = client.get(
        f"/portal/my-appraisals/{cycle_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    # A 沒參與該 cycle → 404
    assert resp.status_code == 404
