"""回歸測試：/salaries/simulate 在發放月須與正式落帳口徑一致，
從期間累積的節慶+超額獎金扣減 pending 懲處。

Bug（2026-06-02 修補前）：simulate endpoint 自己重寫計算尾段，但漏呼叫
engine._adjust_period_totals_for_discipline（該函式唯一 caller 原為
_finalize_breakdown）。員工在發放月有 pending 懲處時，simulate 顯示的
festival/overtime 會高於實際 /calculate 落帳值 → simulate↔actual 不對稱。

simulate 是沙盒（no-write），故只驗「顯示值」對稱，不驗懲處被標記 applied
（simulate 刻意不呼叫 _mark_discipline_applied）。
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.salary import init_salary_services
from api.salary import router as salary_router
from models.database import (
    Base,
    ClassGrade,
    Classroom,
    DisciplinaryAction,
    Employee,
    SalaryRecord,
    Student,
    User,
)
from services.salary.engine import SalaryEngine
from utils.auth import hash_password


@pytest.fixture
def sim_client(tmp_path):
    db_path = tmp_path / "sim-disc.sqlite"
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
    init_salary_services(SalaryEngine(load_from_db=False), MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_teacher_with_class(session):
    """建立一名月薪導師 + 班級 + 27 名學生（足以在發放月產生節慶獎金）。"""
    grade = ClassGrade(name="大班", is_active=True)
    session.add(grade)
    session.flush()
    teacher = Employee(
        employee_id="SIM_DISC_T",
        name="試算懲處老師",
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        insurance_salary_level=30000,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(teacher)
    session.flush()
    cls = Classroom(
        name="甲班", grade_id=grade.id, head_teacher_id=teacher.id, is_active=True
    )
    session.add(cls)
    session.flush()
    teacher.classroom_id = cls.id
    for i in range(27):
        session.add(
            Student(
                student_id=f"SIMS{i:03d}",
                name=f"學生{i}",
                classroom_id=cls.id,
                enrollment_date=date(2024, 8, 1),  # 早於 Feb period（Dec/Jan）
                is_active=True,
            )
        )
    session.flush()
    return teacher.id


def _login_admin(client, session_factory):
    with session_factory() as session:
        session.add(
            User(
                username="sim_admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=["SALARY_READ", "SALARY_WRITE"],
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login", json={"username": "sim_admin", "password": "TempPass123"}
    )
    assert res.status_code == 200


def _simulate_bonus(client, emp_id):
    """呼叫 simulate 取得發放月（2026/2）的節慶+超額合計（顯示值）。"""
    res = client.post(
        "/api/salaries/simulate",
        json={"employee_id": emp_id, "year": 2026, "month": 2},
    )
    assert res.status_code == 200, res.text
    sim = res.json()["simulated"]
    return float(sim["festival_bonus"]) + float(sim["overtime_bonus"])


def test_simulate_deducts_pending_discipline_from_bonus(sim_client):
    """發放月（Feb）simulate：pending 懲處須從節慶+超額顯示值扣減（對齊 /calculate）。"""
    client, session_factory = sim_client
    DEDUCTION = 1000  # warning 預設金額；確保 < 基準獎金以完整扣減

    with session_factory() as session:
        emp_id = _seed_teacher_with_class(session)
        session.commit()
    _login_admin(client, session_factory)

    # ── 基準：無懲處時的節慶+超額合計 ──
    base_bonus = _simulate_bonus(client, emp_id)
    assert (
        base_bonus > DEDUCTION
    ), f"前置條件失敗：基準獎金 {base_bonus} 須 > {DEDUCTION} 才能驗證完整扣減"

    # ── 加 pending 懲處後再 simulate ──
    with session_factory() as session:
        session.add(
            DisciplinaryAction(
                employee_id=emp_id,
                action_date=date(2026, 1, 15),
                action_type="warning",
                deduction_amount=DEDUCTION,
            )
        )
        session.commit()

    new_bonus = _simulate_bonus(client, emp_id)

    # 修補前：base==new（simulate 漏扣懲處）；修補後：差額 == DEDUCTION（對稱於落帳）
    assert base_bonus - new_bonus == DEDUCTION, (
        f"simulate 未從獎金顯示值扣減懲處：base={base_bonus} new={new_bonus}"
        f"（差額應為 {DEDUCTION}）"
    )

    # simulate 是沙盒：懲處須維持 pending（不可被標記 applied）
    with session_factory() as session:
        action = session.query(DisciplinaryAction).filter_by(employee_id=emp_id).one()
        assert (
            action.applied_to_salary_id is None
        ), "simulate 不應標記懲處為已抵扣（no-write 沙盒）"


def test_simulate_passes_override_aware_supplementary_basis(sim_client, monkeypatch):
    """qa-loop #7：simulate 的補充保費基底須傳「覆寫感知總額」，與 _finalize_breakdown 口徑一致。

    員工有 manual_override 的獎金欄位（HR 手填）時，引擎落帳用 record 持久化覆寫值算補充
    保費基底；simulate 原未傳 breakdown_bonus_total_override → 用引擎重算值 → 試算顯示的
    補充保費與實算對 override 員工不一致。補傳覆寫感知總額。
    """
    client, sf = sim_client
    _login_admin(client, sf)
    with sf() as s:
        emp_id = _seed_teacher_with_class(s)
        # 持久化 2026/2 record，festival_bonus 經 HR 手動覆寫為極大值
        s.add(
            SalaryRecord(
                employee_id=emp_id,
                salary_year=2026,
                salary_month=2,
                festival_bonus=999999,
                manual_overrides=["festival_bonus"],
                is_finalized=False,
            )
        )
        s.commit()

    import services.salary.supplementary_premium as sup_mod

    captured = {}
    orig = sup_mod.apply_bonus_supplementary_to_breakdown

    def _spy(*args, **kwargs):
        captured["override"] = kwargs.get("breakdown_bonus_total_override", "MISSING")
        return orig(*args, **kwargs)

    monkeypatch.setattr(sup_mod, "apply_bonus_supplementary_to_breakdown", _spy)

    res = client.post(
        "/api/salaries/simulate",
        json={"employee_id": emp_id, "year": 2026, "month": 2},
    )
    assert res.status_code == 200, res.text
    # 修補前：simulate 未傳此 kwarg → MISSING；修補後：傳覆寫感知總額（含 festival 覆寫 999999）
    assert captured.get("override") not in (
        None,
        "MISSING",
    ), "simulate 未傳 breakdown_bonus_total_override（補充保費基底未覆寫感知，與實算不一致）"
    assert (
        captured["override"] >= 999999
    ), f"覆寫感知總額應含 festival_bonus 覆寫值 999999，實得 {captured['override']}"
