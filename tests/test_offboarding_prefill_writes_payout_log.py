"""驗 prefill_salary 同步寫 UnusedLeavePayoutLog + revoke active grants。"""

import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# SQLite 相容性修補（對齊 test_expire_comp_leave_grants.py 既有 pattern）
_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    """SQLite 相容的 BigInteger 替代型別。"""

    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]

_counter = itertools.count(1)


@pytest.fixture
def session(tmp_path):
    """SQLite in-memory 測試 DB，建立全 schema（含 overtime_comp_leave_grant + unused_leave_payout_log）。"""
    from models.database import Base  # noqa: F401 — 觸發核心 model 的 metadata 注冊

    # 明確注冊本測試依賴的非核心 model
    import models.overtime_comp_leave_grant  # noqa: F401
    import models.unused_leave_payout_log  # noqa: F401

    db_path = tmp_path / "test_prefill_payout_log.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    sess = session_factory()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture
def employee_factory(session):
    """輕量員工 factory（base_salary = daily_wage * 30）。"""
    from models.employee import Employee

    def _make(daily_wage=1800, is_active=True, hire_date=None):
        n = next(_counter)
        base_salary = int(daily_wage * 30)
        emp = Employee(
            employee_id=f"PREF{n:04d}",
            name=f"測試員工{n}",
            is_active=is_active,
            base_salary=base_salary,
            hire_date=hire_date or date(2020, 1, 1),
        )
        session.add(emp)
        session.flush()
        return emp

    return _make


@pytest.fixture
def user_factory(session):
    """輕量使用者 factory。"""
    from models.database import User

    def _make(role="admin"):
        n = next(_counter)
        u = User(username=f"user{n}", password_hash="dummy", role=role)
        session.add(u)
        session.flush()
        return u

    return _make


@pytest.fixture
def leave_quota_factory(session):
    """輕量 LeaveQuota factory。"""
    from models.database import LeaveQuota

    def _make(employee_id, year=2026, leave_type="annual", total_hours=112):
        from models.database import LeaveQuota as LQ

        lq = LQ(
            employee_id=employee_id,
            year=year,
            leave_type=leave_type,
            total_hours=total_hours,
        )
        session.add(lq)
        session.flush()
        return lq

    return _make


@pytest.fixture
def salary_record_factory(session):
    """輕量 SalaryRecord factory（離職月份）。"""
    from models.salary import SalaryRecord

    def _make(employee_id, salary_year=2026, salary_month=6):
        sr = SalaryRecord(
            employee_id=employee_id,
            salary_year=salary_year,
            salary_month=salary_month,
        )
        session.add(sr)
        session.flush()
        return sr

    return _make


@pytest.fixture
def ot_grant_factory(session):
    """輕量 OvertimeCompLeaveGrant factory（自動建底層 OvertimeRecord）。"""
    from models.overtime import OvertimeRecord
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    def _make(emp_id, granted_hours=8.0, status="active", granted_at=None):
        ot = OvertimeRecord(
            employee_id=emp_id,
            overtime_date=granted_at or date(2025, 6, 1),
            overtime_type="weekday",
            hours=granted_hours,
            use_comp_leave=True,
            comp_leave_granted=True,
            status="approved",
        )
        session.add(ot)
        session.flush()

        grant = OvertimeCompLeaveGrant(
            overtime_record_id=ot.id,
            employee_id=emp_id,
            granted_hours=granted_hours,
            granted_at=granted_at or date(2025, 6, 1),
            expires_at=date(2026, 6, 1),
            status=status,
        )
        session.add(grant)
        session.flush()
        return grant

    return _make


def _make_offboarding_record(
    session, employee_id, user_id, resign_date=date(2026, 6, 15)
):
    """建立 EmployeeOffboardingRecord 並 flush。"""
    from models.offboarding import EmployeeOffboardingRecord

    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=resign_date,
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    session.add(record)
    session.flush()
    return record


# ── Test 1: 寫 UnusedLeavePayoutLog ──


def test_prefill_salary_writes_unused_leave_payout_log(
    session, employee_factory, user_factory, leave_quota_factory, salary_record_factory
):
    """prefill_salary 跑後 UnusedLeavePayoutLog 有一筆 source_type='offboarding' 且 amount 對齊 snapshot。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_offboarding_record(session, emp.id, user.id)
    sr = salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)

    run(session, record)
    result = prefill_salary(session, record)

    assert result["status"] == "completed"

    # 驗 log row 存在
    logs = (
        session.query(UnusedLeavePayoutLog)
        .filter_by(employee_id=emp.id, source_type="offboarding")
        .all()
    )
    assert len(logs) == 1, "應有且只有一筆 offboarding log"

    log = logs[0]
    snap = record.leave_balance_snapshot
    # EmployeeOffboardingRecord PK = employee_id（無獨立 id 欄位）
    assert (
        log.source_ref_id == record.employee_id
    ), "source_ref_id 應對應 offboarding record 的 employee_id PK"
    assert float(log.amount) == float(
        snap["payout_amount"]
    ), "amount 應對齊 snapshot.payout_amount"
    assert log.salary_record_id == sr.id, "salary_record_id 應反向綁定 SalaryRecord"
    assert log.salary_period_year == 2026
    assert log.salary_period_month == 6
    # hourly_wage = daily_wage / 8
    expected_hourly_wage = Decimal(str(round(1800.0 / 8, 2)))
    assert abs(log.hourly_wage - expected_hourly_wage) < Decimal("0.01")
    assert log.meta.get("offboarding_record_id") == record.employee_id


# ── Test 2: revoke active grants ──


def test_prefill_salary_revokes_active_comp_grants(
    session,
    employee_factory,
    user_factory,
    leave_quota_factory,
    salary_record_factory,
    ot_grant_factory,
):
    """既有 active grants 全部 mark revoked；already-revoked 不動。"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)
    record = _make_offboarding_record(session, emp.id, user.id)

    # 建 2 active + 1 already-revoked grant
    grant_a = ot_grant_factory(emp.id, granted_hours=8.0, status="active")
    grant_b = ot_grant_factory(emp.id, granted_hours=4.0, status="active")
    grant_c = ot_grant_factory(emp.id, granted_hours=8.0, status="revoked")

    run(session, record)
    prefill_salary(session, record)

    session.refresh(grant_a)
    session.refresh(grant_b)
    session.refresh(grant_c)

    assert grant_a.status == "revoked", "active grant A 應被 revoked"
    assert grant_b.status == "revoked", "active grant B 應被 revoked"
    assert grant_c.status == "revoked", "already-revoked grant C 不受影響（仍 revoked）"

    # 確認所有 active → 0
    remaining_active = (
        session.query(OvertimeCompLeaveGrant)
        .filter_by(employee_id=emp.id, status="active")
        .count()
    )
    assert remaining_active == 0, "prefill 後員工應無任何 active grant"


# ── Test 3: no-op when no SalaryRecord ──


def test_prefill_salary_no_op_when_no_salary_record(
    session, employee_factory, user_factory, leave_quota_factory
):
    """SalaryRecord 不存在 → status=skipped，不寫 log（保留既有 SKIP 邏輯）。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    record = _make_offboarding_record(session, emp.id, user.id)

    run(session, record)
    result = prefill_salary(session, record)

    assert result["status"] == "skipped"
    assert result["payload"]["reason"] == "salary_record_not_yet_created"

    # 不應有 log 寫入
    log_count = (
        session.query(UnusedLeavePayoutLog)
        .filter_by(employee_id=emp.id, source_type="offboarding")
        .count()
    )
    assert log_count == 0, "SalaryRecord 不存在時不應寫 UnusedLeavePayoutLog"


# ── Test 4 (P1-B): 累加而非覆寫，不可抹掉 scheduler 已寫入的補休折現 ──


def test_prefill_salary_accumulates_onto_existing_payout(
    session, employee_factory, user_factory, leave_quota_factory, salary_record_factory
):
    """既有 unused_leave_payout（scheduler comp_leave_expiry 已 += 補休折現 4000）
    不可被 offboarding 的特休折現覆寫，須累加。"""
    from decimal import Decimal
    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_offboarding_record(session, emp.id, user.id)
    sr = salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)
    # 模擬 scheduler 月初已對同月寫入補休折現 4000
    sr.unused_leave_payout = Decimal("4000")
    session.flush()

    run(session, record)
    payout = float(record.leave_balance_snapshot["payout_amount"])
    assert payout > 0, "前置：特休折現須 > 0"

    result = prefill_salary(session, record)
    assert result["status"] == "completed"

    session.refresh(sr)
    assert float(sr.unused_leave_payout) == 4000 + payout, (
        f"offboarding 覆寫抹掉 scheduler 已寫入的 4000："
        f"得 {float(sr.unused_leave_payout)}，應為 {4000 + payout}"
    )


def test_prefill_salary_idempotent_no_double_add(
    session, employee_factory, user_factory, leave_quota_factory, salary_record_factory
):
    """prefill_salary 重跑不可雙加 unused_leave_payout、不可寫第二筆 offboarding log。"""
    from decimal import Decimal
    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_offboarding_record(session, emp.id, user.id)
    sr = salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)
    sr.unused_leave_payout = Decimal("4000")
    session.flush()

    run(session, record)
    payout = float(record.leave_balance_snapshot["payout_amount"])

    prefill_salary(session, record)
    prefill_salary(session, record)  # 重跑

    session.refresh(sr)
    assert (
        float(sr.unused_leave_payout) == 4000 + payout
    ), f"重跑雙加：得 {float(sr.unused_leave_payout)}，應為 {4000 + payout}"
    logs = (
        session.query(UnusedLeavePayoutLog)
        .filter_by(employee_id=emp.id, source_type="offboarding")
        .count()
    )
    assert logs == 1, "重跑不可寫第二筆 offboarding log"


# ── Test 6 (rank 6): 離職未消耗補休須折現，不可直接 revoke 蒸發 ──


def test_prefill_salary_cashes_out_unconsumed_comp_leave(
    session,
    employee_factory,
    user_factory,
    leave_quota_factory,
    salary_record_factory,
    ot_grant_factory,
):
    """rank 6：離職時未消耗補休 grant 應以時薪折現併入 unused_leave_payout（勞基法§32-1），
    而非只 revoke 讓時數蒸發。grant 仍須 revoke 防 scheduler 重撈。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    emp = employee_factory(daily_wage=1800)  # hourly_wage = 225
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    sr = salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)
    record = _make_offboarding_record(session, emp.id, user.id)

    # A: 8h 未消耗；B: 4h 已消耗 1h（net 3h）→ 未消耗合計 11h
    grant_a = ot_grant_factory(emp.id, granted_hours=8.0, status="active")
    grant_b = ot_grant_factory(emp.id, granted_hours=4.0, status="active")
    grant_b.consumed_hours = 1.0
    session.flush()

    run(session, record)
    annual_payout = float(record.leave_balance_snapshot["payout_amount"])

    prefill_salary(session, record)
    session.refresh(sr)

    expected_comp = 11.0 * 225.0  # 2475.0
    assert float(sr.unused_leave_payout) == pytest.approx(
        annual_payout + expected_comp
    ), (
        f"未消耗補休 11h 應折現 {expected_comp}；得 {float(sr.unused_leave_payout)}，"
        f"annual={annual_payout}"
    )

    comp_logs = (
        session.query(UnusedLeavePayoutLog)
        .filter_by(employee_id=emp.id, source_type="offboarding_comp_leave")
        .all()
    )
    assert len(comp_logs) == 1, "應寫一筆 offboarding_comp_leave 折現 log"
    assert float(comp_logs[0].hours) == pytest.approx(11.0)
    assert float(comp_logs[0].amount) == pytest.approx(expected_comp)

    session.refresh(grant_a)
    session.refresh(grant_b)
    assert (
        grant_a.status == "revoked" and grant_b.status == "revoked"
    ), "折現後 grant 仍須 revoke 防 scheduler 重撈"
