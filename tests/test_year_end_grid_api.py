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


def test_manual_patch_only_recomputes_target_employee(client_with_db):
    """單筆 manual_patch 只重算被 patch 的員工，不動其他員工的 version。

    回歸：manual_patch 過去呼叫 build_settlements 未帶 only_employee_ids，會重算整個
    cycle 的所有 DRAFT 員工（每人 version+=1）。修補後只重算目標員工，未被 patch 的
    員工 version 不變（避免版本 churn 與不必要的全 cohort 重算）。
    """
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_a_id = _seed_cycle_and_employee(sf)

    # 加第二位全年在職員工 B（不帶班，避免相依班級資料）
    with sf() as s:
        emp_b = Employee(
            employee_id="E_GRID_002",
            name="李老師",
            position="行政",
            title="行政人員",
            base_salary=32000,
            is_active=True,
            hire_date=date(2020, 1, 1),
        )
        s.add(emp_b)
        s.commit()
        emp_b_id = emp_b.id

    _login(client)
    _build(client, cycle_id)

    def _versions():
        with sf() as s:
            a = (
                s.query(YearEndSettlement)
                .filter_by(year_end_cycle_id=cycle_id, employee_id=emp_a_id)
                .one()
            )
            b = (
                s.query(YearEndSettlement)
                .filter_by(year_end_cycle_id=cycle_id, employee_id=emp_b_id)
                .one()
            )
            return a.id, a.version, b.version

    a_id, a_ver_before, b_ver_before = _versions()

    # patch A
    patch_res = client.patch(
        f"/api/year_end/settlements/{a_id}/manual",
        json={"deduction_disciplinary": "-6000"},
    )
    assert patch_res.status_code == 200, patch_res.text

    _, a_ver_after, b_ver_after = _versions()
    assert a_ver_after > a_ver_before, "被 patch 的員工 version 應 bump"
    assert b_ver_after == b_ver_before, (
        f"未被 patch 的員工 version 不應變動："
        f"before={b_ver_before} after={b_ver_after}"
    )


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
    assert hire_months_before == Decimal(
        "12"
    ), f"初次 build hire_months 應為 12，got {hire_months_before}"

    # PATCH hire_months_override=6 → 比例應為 6/12=0.5
    patch_res = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"hire_months_override": "6"},
    )
    assert patch_res.status_code == 200, patch_res.text
    updated = patch_res.json()

    # hire_months 覆寫為 6，proration_rate 對應 0.5
    assert Decimal(str(updated["hire_months"])) == Decimal(
        "6"
    ), f"hire_months 應為 6，got {updated['hire_months']}"
    assert Decimal(str(updated["proration_rate"])) == Decimal(
        "0.5000"
    ), f"proration_rate 應為 0.5000（6/12），got {updated['proration_rate']}"


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


def test_manual_patch_requires_write_permission(client_with_db):
    """read-only user (YEAR_END_READ, no WRITE) 打 manual-patch → 403。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)

    # 先用 admin 建立 settlement
    _login(client)
    _build(client, cycle_id)
    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    settlement_id = res.json()[0]["id"]

    # 切換成 read-only viewer
    _login(client, "viewer")
    patch_res = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"deduction_disciplinary": "-1000"},
    )
    assert patch_res.status_code == 403, patch_res.text


def test_manual_patch_excess_idempotent(client_with_db):
    """PATCH excess_amount=2000 連打兩次 → 只有 ONE EXCESS_ENROLLMENT item，金額為 2000。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    _build(client, cycle_id)

    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    settlement_id = res.json()[0]["id"]

    # 第一次 PATCH
    r1 = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"excess_amount": "2000"},
    )
    assert r1.status_code == 200, r1.text

    # 第二次 PATCH（相同金額 → upsert，不應再新增一筆）
    r2 = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"excess_amount": "2000"},
    )
    assert r2.status_code == 200, r2.text

    # DB 驗證：該 (cycle, employee) 只有一筆 EXCESS_ENROLLMENT，金額 2000
    with sf() as s:
        items = (
            s.query(SpecialBonusItem)
            .filter_by(
                year_end_cycle_id=cycle_id,
                employee_id=emp_id,
                bonus_type=SpecialBonusType.EXCESS_ENROLLMENT,
            )
            .all()
        )
    assert len(items) == 1, f"預期 1 筆 EXCESS_ENROLLMENT，got {len(items)}"
    assert items[0].amount == Decimal(
        "2000"
    ), f"預期 amount=2000，got {items[0].amount}"


def test_manual_patch_accounting_signed_409(client_with_db):
    """ACCOUNTING_SIGNED settlement → PATCH manual → 409（已簽核請先退回）。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    _build(client, cycle_id)

    # 把 settlement 設為 ACCOUNTING_SIGNED
    with sf() as s:
        st = s.query(YearEndSettlement).filter_by(year_end_cycle_id=cycle_id).first()
        st.status = YearEndSettlementStatus.ACCOUNTING_SIGNED
        settlement_id = st.id
        s.commit()

    patch_res = client.patch(
        f"/api/year_end/settlements/{settlement_id}/manual",
        json={"deduction_disciplinary": "-1000"},
    )
    assert patch_res.status_code == 409, patch_res.text
    assert "DRAFT" in patch_res.json()["detail"]


def test_two_gate_signoff(client_with_db):
    """兩關簽核：會計從 DRAFT 直簽 ACCOUNTING_SIGNED → 老闆 FINALIZE。"""
    client, sf = client_with_db
    # 種三位使用者：admin(build), accountant(sign_accounting), boss(finalize)
    with sf() as s:
        from models.database import User
        from utils.auth import hash_password

        s.add(
            User(
                username="admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["YEAR_END_WRITE", "YEAR_END_READ"],
                is_active=True,
            )
        )
        s.add(
            User(
                username="accountant",
                password_hash=hash_password("TempPass123"),
                role="staff",
                permission_names=["APPRAISAL_ACCOUNTING", "YEAR_END_READ"],
                is_active=True,
            )
        )
        s.add(
            User(
                username="boss",
                password_hash=hash_password("TempPass123"),
                role="staff",
                permission_names=["YEAR_END_FINALIZE", "YEAR_END_READ"],
                is_active=True,
            )
        )
        s.commit()

    cycle_id, emp_id = _seed_cycle_and_employee(sf)

    # admin builds
    _login(client)
    res = _build(client, cycle_id)
    assert res.status_code == 200, res.text

    # 取 settlement_id（DRAFT 狀態）
    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    settlements = res.json()
    assert len(settlements) >= 1
    settlement_id = settlements[0]["id"]
    assert settlements[0]["status"] == "DRAFT"

    # 會計從 DRAFT 直接簽核 → ACCOUNTING_SIGNED（跳過 supervisor）
    _login(client, "accountant")
    sign_res = client.post(f"/api/year_end/settlements/{settlement_id}/sign_accounting")
    assert sign_res.status_code == 200, sign_res.text
    assert sign_res.json()["status"] == "ACCOUNTING_SIGNED"

    # 老闆 finalize
    _login(client, "boss")
    fin_res = client.post(f"/api/year_end/settlements/{settlement_id}/finalize")
    assert fin_res.status_code == 200, fin_res.text
    assert fin_res.json()["status"] == "FINALIZED"


# ============================================================
# B0: settlement_id in grid rows
# ============================================================


def test_grid_endpoint_has_settlement_id(client_with_db):
    """grid 每列應包含 settlement_id，且與 DB 中的 settlement.id 一致。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    _build(client, cycle_id)

    # 取 settlement_id from /settlements
    res = client.get(f"/api/year_end/cycles/{cycle_id}/settlements")
    assert res.status_code == 200
    settlements = res.json()
    assert len(settlements) >= 1
    expected_settlement_id = settlements[0]["id"]

    # grid 應含 settlement_id，且與 settlements 列表的 id 對應
    grid_res = client.get(f"/api/year_end/cycles/{cycle_id}/grid")
    assert grid_res.status_code == 200, grid_res.text
    rows = grid_res.json()
    assert len(rows) >= 1
    row = rows[0]
    assert "settlement_id" in row, f"settlement_id 不在 grid row: {row.keys()}"
    assert (
        row["settlement_id"] == expected_settlement_id
    ), f"grid settlement_id={row['settlement_id']} 不等於 settlements[0].id={expected_settlement_id}"


# ============================================================
# B1: POST /cycles/{cycle_id}/class_targets upsert
# ============================================================


def _seed_classroom(sf, cycle_id: int) -> tuple[int, int]:
    """新建一個不與預設 fixture 衝突的 classroom，回傳 (classroom_id, emp_id)。
    注意：不建 ClassEnrollmentTarget（讓 upsert 測試自己建）。
    """
    with sf() as s:
        classroom = Classroom(name="小班B", school_year=ACADEMIC_YEAR + 1, semester=1)
        s.add(classroom)
        s.flush()
        emp = Employee(
            employee_id="E_CT_001",
            name="李老師",
            position="班導",
            bonus_grade="b",
            title="幼兒園教師",
            base_salary=36160,
            bypass_standard_base=False,
            is_active=True,
            classroom_id=classroom.id,
            hire_date=date(2021, 1, 1),
        )
        s.add(emp)
        s.commit()
        return classroom.id, emp.id


def test_upsert_class_target(client_with_db):
    """POST class_targets → 200 + 正確值寫入。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)
    classroom_id, emp_id = _seed_classroom(sf, cycle_id)
    _login(client)

    payload = {
        "semester_first": True,
        "classroom_id": classroom_id,
        "head_teacher_employee_id": emp_id,
        "head_count_target": 25,
        "returning_student_rate": "0.850",
    }
    res = client.post(f"/api/year_end/cycles/{cycle_id}/class_targets", json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["classroom_id"] == classroom_id
    assert body["head_count_target"] == 25
    assert Decimal(str(body["returning_student_rate"])) == Decimal("0.850")
    assert body["head_teacher_employee_id"] == emp_id
    # 計算欄位應為 0（build_settlements 才重算）
    assert Decimal(str(body["avg_monthly_enrollment"])) == Decimal("0")
    assert Decimal(str(body["class_performance_rate"])) == Decimal("0")


def test_upsert_class_target_idempotent(client_with_db):
    """repeat upsert → 仍只有 1 列，值以最後一次為準。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)
    classroom_id, emp_id = _seed_classroom(sf, cycle_id)
    _login(client)

    base_payload = {
        "semester_first": False,
        "classroom_id": classroom_id,
        "head_teacher_employee_id": emp_id,
        "head_count_target": 20,
        "returning_student_rate": "0.800",
    }
    r1 = client.post(
        f"/api/year_end/cycles/{cycle_id}/class_targets", json=base_payload
    )
    assert r1.status_code == 200, r1.text

    # 第二次：更新 head_count_target
    updated_payload = dict(base_payload)
    updated_payload["head_count_target"] = 22
    r2 = client.post(
        f"/api/year_end/cycles/{cycle_id}/class_targets", json=updated_payload
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["head_count_target"] == 22

    # DB 應只有一列
    with sf() as s:
        rows = (
            s.query(ClassEnrollmentTarget)
            .filter_by(
                year_end_cycle_id=cycle_id,
                semester_first=False,
                classroom_id=classroom_id,
            )
            .all()
        )
    assert len(rows) == 1, f"預期 1 列，got {len(rows)}"
    assert rows[0].head_count_target == 22


def test_upsert_class_target_requires_write(client_with_db):
    """read-only user → POST class_targets → 403。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)
    classroom_id, emp_id = _seed_classroom(sf, cycle_id)
    _login(client, "viewer")

    payload = {
        "semester_first": True,
        "classroom_id": classroom_id,
        "head_count_target": 18,
        "returning_student_rate": "0.700",
    }
    res = client.post(f"/api/year_end/cycles/{cycle_id}/class_targets", json=payload)
    assert res.status_code == 403, res.text


# ============================================================
# B2: create_cycle with clone_from_academic_year
# ============================================================

FINALIZE_PERMS = ["YEAR_END_FINALIZE", "YEAR_END_WRITE", "YEAR_END_READ"]


def _seed_finalize_user(sf):
    """種具 YEAR_END_FINALIZE 權限的 admin2 帳號（避免與 _seed_users admin 衝突）。"""
    with sf() as s:
        s.add(
            User(
                username="admin2",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=FINALIZE_PERMS,
                is_active=True,
            )
        )
        s.commit()


def test_create_cycle_clone_previous(client_with_db):
    """clone_from_academic_year → 新 cycle 複製 org_settings + class_targets，
    且 enrollment_actual=None、school_achievement_rate=0、avg_monthly_enrollment=0。
    """
    client, sf = client_with_db
    _seed_users(sf)
    _seed_finalize_user(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)  # seeds cycle 114
    _login(client, "admin2")

    # 建新 cycle 115，從 114 clone
    new_cycle_payload = {
        "academic_year": 115,
        "start_date": "2026-08-01",
        "end_date": "2027-07-31",
        "bonus_calc_date": "2027-01-15",
        "clone_from_academic_year": ACADEMIC_YEAR,  # 114
    }
    res = client.post("/api/year_end/cycles", json=new_cycle_payload)
    assert res.status_code == 200, res.text
    new_cycle_id = res.json()["id"]

    # 新 cycle 的 org_settings 應有兩筆（複製自 114）
    org_res = client.get(f"/api/year_end/cycles/{new_cycle_id}/org_settings")
    assert org_res.status_code == 200
    org_settings = org_res.json()
    assert len(org_settings) == 2, f"預期 2 筆 org_settings，got {len(org_settings)}"
    for o in org_settings:
        assert o["enrollment_actual"] is None, f"enrollment_actual 應 reset 為 None"
        assert Decimal(str(o["school_achievement_rate"])) == Decimal(
            "0"
        ), f"school_achievement_rate 應 reset 為 0"
        # org_achievement_rate 應從來源複製（114 的兩筆都是 0，但至少欄位存在）
        assert "org_achievement_rate" in o

    # 新 cycle 的 class_targets 應有兩筆（上下學期各一，複製自 114）
    ct_res = client.get(f"/api/year_end/cycles/{new_cycle_id}/class_targets")
    assert ct_res.status_code == 200
    class_targets = ct_res.json()
    assert len(class_targets) == 2, f"預期 2 筆 class_targets，got {len(class_targets)}"
    for ct in class_targets:
        assert Decimal(str(ct["avg_monthly_enrollment"])) == Decimal(
            "0"
        ), f"avg_monthly_enrollment 應 reset 為 0"
        assert Decimal(str(ct["class_performance_rate"])) == Decimal(
            "0"
        ), f"class_performance_rate 應 reset 為 0"
        # head_count_target 應從來源複製
        assert ct["head_count_target"] == 30


def test_create_cycle_clone_missing_source_422(client_with_db):
    """clone_from_academic_year 指向不存在的學年 → 422。"""
    client, sf = client_with_db
    _seed_users(sf)
    _seed_finalize_user(sf)
    _seed_cycle_and_employee(sf)  # seed cycle 114
    _login(client, "admin2")

    res = client.post(
        "/api/year_end/cycles",
        json={
            "academic_year": 116,
            "start_date": "2027-08-01",
            "end_date": "2028-07-31",
            "bonus_calc_date": "2028-01-15",
            "clone_from_academic_year": 999,  # 不存在
        },
    )
    assert res.status_code == 422, res.text
