"""tests/test_year_end_grid_api.py — build-settlements / grid / manual-patch 端點測試。

使用 FastAPI TestClient + SQLite in-memory DB，
透過 /api/auth/login 取得 cookie 後打年終獎金端點。
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

import models.base as base_module  # noqa: E402
from api.auth import _account_failures, _ip_attempts  # noqa: E402
from api.auth import router as auth_router  # noqa: E402
from api.year_end import year_end_router  # noqa: E402
from models.classroom import Classroom  # noqa: E402
from models.config import BonusConfig, PositionSalaryConfig  # noqa: E402
from models.database import Base, User  # noqa: E402
from models.employee import Employee  # noqa: E402
from models.year_end import (  # noqa: E402
    ClassEnrollmentTarget,
    OrgYearSettings,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from utils.auth import hash_password  # noqa: E402

# ============================================================
# Fixtures
# ============================================================

WRITE_PERMS = ["YEAR_END_WRITE", "YEAR_END_READ"]
READ_ONLY_PERMS = ["YEAR_END_READ"]


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "grid-api-test.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(year_end_router)

    with TestClient(app) as client:
        yield client, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


# ============================================================
# Seed helpers
# ============================================================

ACADEMIC_YEAR = 114
CYCLE_START = date(2025, 8, 1)
CYCLE_END = date(2026, 7, 31)
BONUS_CALC_DATE = date(2026, 1, 15)


def _seed_users(sf):
    with sf() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=WRITE_PERMS,
                is_active=True,
            )
        )
        s.add(
            User(
                username="viewer",
                password_hash=hash_password("TempPass123"),
                role="staff",
                permission_names=READ_ONLY_PERMS,
                is_active=True,
            )
        )
        s.commit()


def _seed_cycle_and_employee(sf) -> tuple[int, int]:
    """種最小必要資料：cycle + 一位全年在職的月薪班導員工 + OrgYearSettings。
    回傳 (cycle_id, employee_id)。
    """
    with sf() as s:
        s.add(
            PositionSalaryConfig(
                head_teacher_a=39240, head_teacher_b=36160, head_teacher_c=33000
            )
        )
        s.add(
            BonusConfig(
                config_year=2025,
                version=1,
                is_active=True,
                head_teacher_ab=2000,
                head_teacher_c=1500,
                assistant_teacher_ab=1200,
                assistant_teacher_c=1200,
                principal_festival=6500,
                director_festival=3500,
                leader_festival=2000,
                driver_festival=1000,
                designer_festival=1000,
                admin_festival=2000,
                art_teacher_festival=2000,
            )
        )
        s.flush()

        cycle = YearEndCycle(
            academic_year=ACADEMIC_YEAR,
            start_date=CYCLE_START,
            end_date=CYCLE_END,
            bonus_calc_date=BONUS_CALC_DATE,
        )
        s.add(cycle)
        s.flush()

        # OrgYearSettings（兩學期，stored rates 直接種）
        s.add(
            OrgYearSettings(
                year_end_cycle_id=cycle.id,
                semester_first=True,
                enrollment_target=160,
                school_achievement_rate=Decimal("91.5"),
                org_achievement_rate=Decimal("0"),
            )
        )
        s.add(
            OrgYearSettings(
                year_end_cycle_id=cycle.id,
                semester_first=False,
                enrollment_target=160,
                school_achievement_rate=Decimal("75.6"),
                org_achievement_rate=Decimal("0"),
            )
        )

        classroom = Classroom(name="大班A", school_year=ACADEMIC_YEAR, semester=1)
        s.add(classroom)
        s.flush()

        emp = Employee(
            employee_id="E_GRID_001",
            name="王老師",
            position="班導",
            bonus_grade="b",
            title="幼兒園教師",
            base_salary=36160,
            bypass_standard_base=False,
            is_active=True,
            classroom_id=classroom.id,
            hire_date=date(2020, 1, 1),  # 滿年在職
        )
        s.add(emp)
        s.flush()

        # ClassEnrollmentTarget（讓 build_settlements 不跳缺資料）
        s.add(
            ClassEnrollmentTarget(
                year_end_cycle_id=cycle.id,
                semester_first=True,
                classroom_id=classroom.id,
                head_teacher_employee_id=emp.id,
                head_count_target=30,
                class_performance_rate=Decimal("100.0"),
                returning_student_rate=Decimal("0.900"),
            )
        )
        s.add(
            ClassEnrollmentTarget(
                year_end_cycle_id=cycle.id,
                semester_first=False,
                classroom_id=classroom.id,
                head_teacher_employee_id=emp.id,
                head_count_target=30,
                class_performance_rate=Decimal("100.0"),
                returning_student_rate=Decimal("0.900"),
            )
        )
        s.commit()

        cycle_id = cycle.id
        emp_id = emp.id

    return cycle_id, emp_id


def _login(client, username="admin"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "TempPass123"}
    )
    assert res.status_code == 200, f"login failed: {res.text}"


def _build(client, cycle_id, included_resigned=None):
    body = {"included_resigned_employee_ids": included_resigned or []}
    return client.post(f"/api/year_end/cycles/{cycle_id}/build-settlements", json=body)


# ============================================================
# Tests
# ============================================================


def test_build_settlements_endpoint(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)
    _login(client)

    res = _build(client, cycle_id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["built"] >= 1
    assert body["skipped_finalized"] == 0


def test_grid_endpoint_shape(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)
    _login(client)

    _build(client, cycle_id)

    res = client.get(f"/api/year_end/cycles/{cycle_id}/grid")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert isinstance(rows, list)
    assert len(rows) >= 1
    row = rows[0]
    assert "employee_name" in row
    assert "payable_amount" in row
    assert "special_bonuses" in row
    assert isinstance(row["special_bonuses"], dict)
    assert "total_amount" in row
    assert "status" in row


def test_manual_patch_disciplinary_recomputes(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    _build(client, cycle_id)

    # 取 settlement_id
    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    settlements = res.json()
    assert len(settlements) >= 1
    settlement_id = settlements[0]["id"]
    total_before = Decimal(str(settlements[0]["total_amount"]))

    # PATCH 獎懲扣項 -6000
    patch_res = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"deduction_disciplinary": "-6000"},
    )
    assert patch_res.status_code == 200, patch_res.text
    updated = patch_res.json()

    total_after = Decimal(str(updated["total_amount"]))
    # proration_rate == 1（全年在職），total 應下降 6000
    diff = total_before - total_after
    assert diff == Decimal("6000"), f"expected 6000 drop, got {diff}"


def test_manual_patch_excess_adds_special(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    _build(client, cycle_id)

    # 取 settlement_id
    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    settlement_id = res.json()[0]["id"]

    patch_res = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"excess_amount": "2000"},
    )
    assert patch_res.status_code == 200, patch_res.text

    # grid 應反映 EXCESS_ENROLLMENT 2000
    grid_res = client.get(f"/api/year_end/cycles/{cycle_id}/grid")
    row = grid_res.json()[0]
    specials = row["special_bonuses"]
    assert (
        SpecialBonusType.EXCESS_ENROLLMENT.value in specials
    ), f"expected EXCESS_ENROLLMENT in {specials}"
    assert Decimal(str(specials[SpecialBonusType.EXCESS_ENROLLMENT.value])) == Decimal(
        "2000"
    )
    # total_amount 應包含 2000
    assert Decimal(str(row["total_amount"])) >= Decimal("2000")


def test_manual_patch_hire_months_override(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    _build(client, cycle_id)

    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    s = res.json()[0]
    settlement_id = s["id"]
    hire_months_before = Decimal(str(s["hire_months"]))

    # 第一次 build 員工全年在職 → hire_months 應為 12
    assert hire_months_before == Decimal("12"), (
        f"初次 build hire_months 應為 12，got {hire_months_before}"
    )

    # PATCH hire_months_override=6 → 比例應為 6/12=0.5
    patch_res = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"hire_months_override": "6"},
    )
    assert patch_res.status_code == 200, patch_res.text
    updated = patch_res.json()

    # hire_months 覆寫為 6，proration_rate 對應 0.5
    assert Decimal(str(updated["hire_months"])) == Decimal("6"), (
        f"hire_months 應為 6，got {updated['hire_months']}"
    )
    assert Decimal(str(updated["proration_rate"])) == Decimal("0.5000"), (
        f"proration_rate 應為 0.5000（6/12），got {updated['proration_rate']}"
    )


def test_manual_patch_finalized_409(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    _build(client, cycle_id)

    # 取 settlement 並 finalize 它
    with sf() as s:
        st = s.query(YearEndSettlement).filter_by(year_end_cycle_id=cycle_id).first()
        st.status = YearEndSettlementStatus.FINALIZED
        settlement_id = st.id
        s.commit()

    patch_res = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"deduction_disciplinary": "-1000"},
    )
    assert patch_res.status_code == 409, patch_res.text


def test_build_requires_write_permission(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)
    _login(client, "viewer")

    res = _build(client, cycle_id)
    assert res.status_code == 403, res.text
