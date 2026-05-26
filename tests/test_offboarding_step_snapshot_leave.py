"""驗證 snapshot_leave step：寫 leave_balance_snapshot JSONB + leave_snapshot_at。"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import models.overtime_comp_leave_grant  # noqa: F401 — 確保 Base.metadata 含 overtime_comp_leave_grants 表
import models.unused_leave_payout_log  # noqa: F401 — 確保 Base.metadata 含 unused_leave_payout_log 表
from models.database import Base, Employee, User, LeaveQuota
from models.offboarding import EmployeeOffboardingRecord
from models.salary import SalaryRecord

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊既有 offboarding step test pattern）。"""
    db_path = tmp_path / "offboarding_step_snapshot_leave.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def employee_factory(db_session):
    """建立測試員工。daily_wage 換算為 base_salary = daily_wage * 30 存入 DB。"""

    def _factory(
        *,
        hire_date=date(2020, 1, 1),
        is_active=True,
        daily_wage=None,
    ) -> Employee:
        global _counter
        _counter += 1
        # Employee model 只有 base_salary；daily_wage 是測試便利參數，換算存入
        base_salary = int(daily_wage * 30) if daily_wage is not None else 0
        emp = Employee(
            employee_id=f"SLT{_counter:04d}",
            name=f"測試員工{_counter}",
            hire_date=hire_date,
            is_active=is_active,
            base_salary=base_salary,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

    return _factory


@pytest.fixture
def user_factory(db_session):
    def _factory(*, role="admin") -> User:
        global _counter
        _counter += 1
        u = User(
            username=f"testuser{_counter}",
            password_hash="dummy",
            role=role,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return _factory


@pytest.fixture
def leave_quota_factory(db_session):
    def _factory(
        *,
        employee_id: int,
        year: int,
        leave_type: str,
        total_hours: float,
        school_year=None,
    ) -> LeaveQuota:
        quota = LeaveQuota(
            employee_id=employee_id,
            year=year,
            leave_type=leave_type,
            total_hours=total_hours,
            school_year=school_year,
        )
        db_session.add(quota)
        db_session.flush()
        return quota

    return _factory


@pytest.fixture
def salary_record_factory(db_session):
    def _factory(
        *,
        employee_id: int,
        salary_year: int,
        salary_month: int,
    ) -> SalaryRecord:
        sr = SalaryRecord(
            employee_id=employee_id,
            salary_year=salary_year,
            salary_month=salary_month,
        )
        db_session.add(sr)
        db_session.flush()
        return sr

    return _factory


def _make_record(db_session, employee_id, user_id):
    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_snapshot_writes_balance_to_jsonb(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """snapshot run() 寫入正確 JSONB snapshot 及 leave_snapshot_at。"""
    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run

    result = run(db_session, record)

    assert result["step"] == "snapshot_leave"
    assert result["status"] == "completed"
    assert record.leave_snapshot_at is not None
    snap = record.leave_balance_snapshot
    assert snap["total_hours"] == 112.0
    assert snap["used_hours"] == 0.0
    assert snap["remaining_hours"] == 112.0
    assert snap["remaining_days"] == 14.0
    # daily_wage = base_salary / 30 = 54000 / 30 = 1800.0
    assert snap["daily_wage"] == 1800.0
    assert snap["payout_amount"] == 14 * 1800  # 25200
    assert snap["calc_rule_version"] == "labor_act_38_2026_v1"
    assert result["payload"] == {"days": 14.0, "payout": 25200.0}


def test_snapshot_when_no_quota_returns_zero(
    db_session, employee_factory, user_factory
):
    """無 quota row 不算失敗：員工可能剛到職未生 quota，snapshot 寫 0。"""
    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run

    result = run(db_session, record)
    assert result["status"] == "completed"
    assert record.leave_balance_snapshot["remaining_days"] == 0.0
    assert record.leave_balance_snapshot["payout_amount"] == 0.0


def test_snapshot_raises_when_employee_has_no_daily_wage(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """daily_wage 缺失（base_salary=0）→ raise OffboardingError LEAVE_BALANCE_NOT_FOUND。"""
    emp = employee_factory(
        daily_wage=None
    )  # base_salary=0 → _resolve_daily_wage 回 None
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run
    from services.offboarding.orchestrator import OffboardingError

    with pytest.raises(OffboardingError) as exc_info:
        run(db_session, record)
    assert exc_info.value.code == "LEAVE_BALANCE_NOT_FOUND"


def test_prefill_salary_writes_to_existing_record(
    db_session,
    employee_factory,
    user_factory,
    leave_quota_factory,
    salary_record_factory,
):
    """prefill_salary 把 snapshot.payout_amount 寫入既有 SalaryRecord.unused_leave_payout。"""
    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_record(db_session, emp.id, user.id)
    sr = salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)

    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    run(db_session, record)
    result = prefill_salary(db_session, record)

    assert result["status"] == "completed"
    assert result["payload"]["salary_record_id"] == sr.id
    db_session.refresh(sr)
    assert float(sr.unused_leave_payout) == 25200.0


def test_prefill_salary_skips_when_no_record(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """prefill_salary 當月無 SalaryRecord → status=skipped。"""
    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    run(db_session, record)
    result = prefill_salary(db_session, record)

    assert result["status"] == "skipped"
    assert result["payload"]["reason"] == "salary_record_not_yet_created"


# ── Characterization tests: mid-month / cross-year / round_half_up 守邊界 ──
#
# 2026-05-26 補：補足月中離職、跨年離職、approved leave 邊界，並驗證
# round_half_up 在 .5 邊界守住 ROUND_HALF_UP（snapshot_leave.py 已從 builtin
# round() 切到 utils.rounding.round_half_up；CI money-rounding-gate paths
# 同步加 services/offboarding/）。
#
# 設計假設（user 確認 2026-05-26）：
#     特休「年初發給制」— 全年 quota 一次性配給，**不按服務月份 prorate**。
#     例：員工 1/1 入職、6/15 離職、全年配 120hr → snapshot 視為 120hr 可折現，
#     不是 120 × 6.5/12 = 65hr。多數台灣中小企業採此制；若公司政策日後改按月
#     vesting，需在 snapshot_leave.run 加 prorate 邏輯後修改此 test。


def _make_record_at(db_session, employee_id, user_id, resign_date):
    """允許指定 resign_date 的 record factory（_make_record 預設 6/15）。"""
    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=resign_date,
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_snapshot_mid_month_resign_uses_full_year_quota(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """月中離職用整年 quota（年初發給制 characterization）。

    服務 5.5 月仍給整年 120hr，不按 120 × 6.5/12 ≈ 65hr prorate。
    """
    emp = employee_factory(hire_date=date(2026, 1, 1), daily_wage=1500)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=120
    )
    record = _make_record_at(db_session, emp.id, user.id, date(2026, 6, 15))

    from services.offboarding.steps.snapshot_leave import run

    run(db_session, record)

    snap = record.leave_balance_snapshot
    assert snap["total_hours"] == 120.0  # 整年 quota，未 prorate
    assert snap["used_hours"] == 0.0
    assert snap["remaining_hours"] == 120.0
    assert snap["remaining_days"] == 15.0
    assert snap["payout_amount"] == 15 * 1500  # 22500.0


def test_snapshot_cross_year_dec_31_uses_resign_year_quota(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """12/31 離職：snapshot_date.year = resign_date.year → 查當年 quota。"""
    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    # 2026 與 2027 兩年 quota 並存（年度切換時可能同時有兩 row）
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    leave_quota_factory(
        employee_id=emp.id, year=2027, leave_type="annual", total_hours=120
    )
    record = _make_record_at(db_session, emp.id, user.id, date(2026, 12, 31))

    from services.offboarding.steps.snapshot_leave import run

    run(db_session, record)

    snap = record.leave_balance_snapshot
    assert snap["total_hours"] == 80.0  # 2026 quota（非 2027）
    assert snap["remaining_days"] == 10.0
    assert snap["payout_amount"] == 10 * 1500


def test_snapshot_jan_1_uses_new_year_quota_zero_if_missing(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """1/1 離職：用新年 quota；若新年 quota 尚未生 → total_hours=0（不會 fallback 去年）。"""
    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    # 只有 2026 quota，2027 尚未生（年度切換 race condition）
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=120
    )
    record = _make_record_at(db_session, emp.id, user.id, date(2027, 1, 1))

    from services.offboarding.steps.snapshot_leave import run

    run(db_session, record)

    snap = record.leave_balance_snapshot
    assert snap["total_hours"] == 0.0  # 2027 quota 不存在，不回退 2026
    assert snap["remaining_days"] == 0.0
    assert snap["payout_amount"] == 0.0


def test_snapshot_excludes_leave_starting_after_resign_date(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """approved annual leave start_date > resign_date 不算入 used。"""
    from models.leave import LeaveRecord

    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    # 6/15 離職，6/20 之後的 approved 假單不算入 used
    db_session.add(
        LeaveRecord(
            employee_id=emp.id,
            leave_type="annual",
            start_date=date(2026, 6, 20),
            end_date=date(2026, 6, 20),
            leave_hours=8.0,
            status="approved",
        )
    )
    db_session.flush()
    record = _make_record_at(db_session, emp.id, user.id, date(2026, 6, 15))

    from services.offboarding.steps.snapshot_leave import run

    run(db_session, record)

    snap = record.leave_balance_snapshot
    assert snap["used_hours"] == 0.0  # 6/20 假單 start_date > 6/15 不算
    assert snap["remaining_hours"] == 80.0


def test_snapshot_includes_leave_starting_on_resign_date(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """start_date == resign_date 算入 used（filter 條件 <= 為 inclusive）。"""
    from models.leave import LeaveRecord

    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    # 6/15 當天 approved annual leave → 算入 used
    db_session.add(
        LeaveRecord(
            employee_id=emp.id,
            leave_type="annual",
            start_date=date(2026, 6, 15),
            end_date=date(2026, 6, 15),
            leave_hours=8.0,
            status="approved",
        )
    )
    db_session.flush()
    record = _make_record_at(db_session, emp.id, user.id, date(2026, 6, 15))

    from services.offboarding.steps.snapshot_leave import run

    run(db_session, record)

    snap = record.leave_balance_snapshot
    assert snap["used_hours"] == 8.0  # 6/15 邊界 inclusive
    assert snap["remaining_hours"] == 72.0


def test_snapshot_payout_uses_round_half_up_at_half_boundary(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """payout .005 邊界用 ROUND_HALF_UP（守 2026-05-25 rollout 規則）。

    Boundary: remaining_days × daily_wage = 180.015
        - HALF_EVEN (builtin round) → 180.01（.5 toward even 0）
        - HALF_UP (round_half_up)   → 180.02

    若此 test 失敗回到 180.01，表示 snapshot_leave.py 又退回 builtin round()，
    違反 2026-05-25 round_half_up rollout（ref: utils/rounding.py docstring）。
    """
    emp = employee_factory(
        daily_wage=None
    )  # base_salary=0 → _resolve_daily_wage 走 fallback
    # 動態加 daily_wage instance attr（model 預留欄位，see snapshot_leave.py:_resolve_daily_wage）
    emp.daily_wage = 1500.125
    user = user_factory()
    # quota=1hr → leave_quota_helpers 算 remaining_days = round(1/8, 2) = 0.12
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=1
    )
    record = _make_record_at(db_session, emp.id, user.id, date(2026, 6, 15))

    from services.offboarding.steps.snapshot_leave import run

    run(db_session, record)

    snap = record.leave_balance_snapshot
    assert snap["daily_wage"] == 1500.125
    assert snap["remaining_days"] == 0.12
    # 0.12 × 1500.125 = 180.015 → HALF_UP=180.02（非 builtin HALF_EVEN=180.01）
    assert snap["payout_amount"] == 180.02
