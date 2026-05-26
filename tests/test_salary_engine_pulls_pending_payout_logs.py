"""salary engine layer 2：_pull_pending_payout_logs 撈 pending log 加總 + 反向綁定。

策略：直接測試模組層級 helper `_pull_pending_payout_logs(session, salary_record)`，
以 SQLite in-memory DB + 輕量 fixture 避免啟動整個 SalaryEngine 計算鏈。
layer-2 路徑（寫入 unused_leave_payout + 反向綁定 salary_record_id）必須真實走 DB。
"""

import itertools
import pytest
from datetime import date
from decimal import Decimal

import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# SQLite 相容性修補（與 conftest.py + 其他測試檔同步）
_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    """SQLite 相容的 BigInteger 替代型別。"""

    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]


@pytest.fixture
def session(tmp_path):
    """SQLite in-memory 測試 DB，建立全 schema。"""
    from models.database import Base  # noqa: F401 — 觸發核心 model 的 metadata 注冊

    # 明確注冊本測試依賴的 model
    import models.unused_leave_payout_log  # noqa: F401

    db_path = tmp_path / "test_engine_layer2.sqlite"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    test_session_factory = sessionmaker(bind=test_engine)
    Base.metadata.create_all(test_engine)

    sess = test_session_factory()
    yield sess
    sess.close()
    test_engine.dispose()


_emp_counter = itertools.count(1)


@pytest.fixture
def employee_factory(session):
    """輕量員工 factory。"""
    from models.employee import Employee

    def _make(employee_type="monthly", base_salary=48000.0, is_active=True):
        n = next(_emp_counter)
        emp = Employee(
            employee_id=f"EL{n:04d}",
            name=f"Layer2測試員工{n}",
            employee_type=employee_type,
            base_salary=base_salary,
            is_active=is_active,
            hire_date=date(2020, 1, 1),
        )
        session.add(emp)
        session.flush()
        return emp

    return _make


@pytest.fixture
def salary_record_factory(session):
    """輕量 SalaryRecord factory，只填必要欄位。"""
    from models.salary import SalaryRecord

    def _make(
        emp_id, year, month, unused_leave_payout=Decimal("0"), is_finalized=False
    ):
        sr = SalaryRecord(
            employee_id=emp_id,
            salary_year=year,
            salary_month=month,
            base_salary=48000,
            unused_leave_payout=unused_leave_payout,
            is_finalized=is_finalized,
        )
        session.add(sr)
        session.flush()
        return sr

    return _make


# ─────────────────────────────────────────────────────────────────────────────
# 輔助：直接呼叫 _fill_salary_record 並帶 session 以觸發 layer-2
# ─────────────────────────────────────────────────────────────────────────────


def _fill_with_session(session, salary_record):
    """用最小 breakdown + dummy engine 呼叫 _fill_salary_record(session=session)。

    目的：讓 layer-2 helper（_pull_pending_payout_logs）被觸發，
    同時不需跑完整的 SalaryEngine.calculate 流程。
    """
    from services.salary.engine import _fill_salary_record
    from services.salary.breakdown import SalaryBreakdown

    breakdown = SalaryBreakdown(
        employee_name="dummy",
        employee_id=str(salary_record.employee_id),
        year=salary_record.salary_year,
        month=salary_record.salary_month,
    )
    breakdown.base_salary = float(salary_record.base_salary or 0)

    class _FakeEngine:
        _bonus_config_id = None
        _attendance_policy_id = None

    _fill_salary_record(salary_record, breakdown, _FakeEngine(), session=session)
    session.flush()


# ─────────────────────────────────────────────────────────────────────────────
# 測試案例
# ─────────────────────────────────────────────────────────────────────────────


def test_pull_pending_logs_sum_and_bind(
    session, employee_factory, salary_record_factory
):
    """pending log（salary_record_id IS NULL）加總後寫入 SalaryRecord + 反向綁定。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    emp = employee_factory(employee_type="monthly", base_salary=48000.0)
    sr = salary_record_factory(emp.id, 2026, 5)

    log = UnusedLeavePayoutLog(
        employee_id=emp.id,
        source_type="comp_grant_expiry",
        source_ref_id=None,
        hours=4.0,
        hourly_wage=Decimal("200.00"),
        amount=Decimal("800.00"),
        wage_basis_date=date(2026, 4, 1),
        salary_period_year=2026,
        salary_period_month=5,
        meta={},
    )
    session.add(log)
    session.commit()

    # 直接呼叫 helper（layer-2 entry）
    from services.salary.engine import _pull_pending_payout_logs

    _pull_pending_payout_logs(session, sr)
    session.flush()

    assert sr.unused_leave_payout == Decimal("800.00")
    session.refresh(log)
    assert log.salary_record_id == sr.id


def test_pull_pending_logs_two_logs_summed(
    session, employee_factory, salary_record_factory
):
    """同員工同月兩筆 pending log → 加總為一個值。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    emp = employee_factory()
    sr = salary_record_factory(emp.id, 2026, 5)

    for amount in [Decimal("800.00"), Decimal("400.00")]:
        log = UnusedLeavePayoutLog(
            employee_id=emp.id,
            source_type="comp_grant_expiry",
            source_ref_id=None,
            hours=4.0,
            hourly_wage=Decimal("200.00"),
            amount=amount,
            wage_basis_date=date(2026, 4, 1),
            salary_period_year=2026,
            salary_period_month=5,
            meta={},
        )
        session.add(log)
    session.commit()

    from services.salary.engine import _pull_pending_payout_logs

    _pull_pending_payout_logs(session, sr)
    session.flush()

    assert sr.unused_leave_payout == Decimal("1200.00")


def test_already_bound_logs_not_double_counted(
    session, employee_factory, salary_record_factory
):
    """已綁定（salary_record_id IS NOT NULL）的 log 不會重複計入。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    emp = employee_factory()
    sr = salary_record_factory(emp.id, 2026, 5, unused_leave_payout=Decimal("800.00"))

    # 已綁定 log
    log_bound = UnusedLeavePayoutLog(
        employee_id=emp.id,
        source_type="comp_grant_expiry",
        source_ref_id=None,
        hours=4.0,
        hourly_wage=Decimal("200.00"),
        amount=Decimal("800.00"),
        wage_basis_date=date(2026, 4, 1),
        salary_period_year=2026,
        salary_period_month=5,
        salary_record_id=sr.id,
        meta={},
    )
    session.add(log_bound)
    session.commit()

    from services.salary.engine import _pull_pending_payout_logs

    _pull_pending_payout_logs(session, sr)
    session.flush()

    # 應維持 800，不是 1600
    assert sr.unused_leave_payout == Decimal("800.00")


def test_other_employee_log_not_pulled(
    session, employee_factory, salary_record_factory
):
    """其他員工的 pending log 不會混進來。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    emp_a = employee_factory()
    emp_b = employee_factory()
    sr = salary_record_factory(emp_a.id, 2026, 5)

    # 屬於 emp_b 的 log
    log_b = UnusedLeavePayoutLog(
        employee_id=emp_b.id,
        source_type="comp_grant_expiry",
        source_ref_id=None,
        hours=4.0,
        hourly_wage=Decimal("200.00"),
        amount=Decimal("800.00"),
        wage_basis_date=date(2026, 4, 1),
        salary_period_year=2026,
        salary_period_month=5,
        meta={},
    )
    session.add(log_b)
    session.commit()

    from services.salary.engine import _pull_pending_payout_logs

    _pull_pending_payout_logs(session, sr)
    session.flush()

    assert sr.unused_leave_payout == Decimal("0")


def test_different_period_log_not_pulled(
    session, employee_factory, salary_record_factory
):
    """不同薪資期間的 pending log 不會混進來。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    emp = employee_factory()
    sr = salary_record_factory(emp.id, 2026, 5)

    # 屬於 2026-04 的 log（期間不符）
    log_wrong_period = UnusedLeavePayoutLog(
        employee_id=emp.id,
        source_type="comp_grant_expiry",
        source_ref_id=None,
        hours=4.0,
        hourly_wage=Decimal("200.00"),
        amount=Decimal("800.00"),
        wage_basis_date=date(2026, 3, 1),
        salary_period_year=2026,
        salary_period_month=4,  # 4 月，不是 5 月
        meta={},
    )
    session.add(log_wrong_period)
    session.commit()

    from services.salary.engine import _pull_pending_payout_logs

    _pull_pending_payout_logs(session, sr)
    session.flush()

    assert sr.unused_leave_payout == Decimal("0")


def test_fill_salary_record_triggers_layer2(
    session, employee_factory, salary_record_factory
):
    """_fill_salary_record(session=session) 自動觸發 layer-2 撈 log + 綁定。"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    emp = employee_factory()
    sr = salary_record_factory(emp.id, 2026, 5)

    log = UnusedLeavePayoutLog(
        employee_id=emp.id,
        source_type="comp_grant_expiry",
        source_ref_id=None,
        hours=4.0,
        hourly_wage=Decimal("200.00"),
        amount=Decimal("800.00"),
        wage_basis_date=date(2026, 4, 1),
        salary_period_year=2026,
        salary_period_month=5,
        meta={},
    )
    session.add(log)
    session.commit()

    _fill_with_session(session, sr)

    assert sr.unused_leave_payout == Decimal("800.00")
    session.refresh(log)
    assert log.salary_record_id == sr.id


def test_no_pending_logs_zero_added(session, employee_factory, salary_record_factory):
    """無 pending log 時 unused_leave_payout 維持為 0（不寫入任何值）。"""
    emp = employee_factory()
    sr = salary_record_factory(emp.id, 2026, 5)

    from services.salary.engine import _pull_pending_payout_logs

    _pull_pending_payout_logs(session, sr)
    session.flush()

    assert sr.unused_leave_payout == Decimal("0")
