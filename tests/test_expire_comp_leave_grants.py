"""scheduler 撈到期 grant 結算 + 寫 log + 寫 SalaryRecord 行為驗證。"""

import itertools
import pytest
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON

# SQLite 相容性修補（與 conftest.py 同步）
_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    """SQLite 相容的 BigInteger 替代型別。"""

    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]


@pytest.fixture
def session(tmp_path):
    """SQLite in-memory 測試 DB，建立全 schema。

    明確 import 所有本測試依賴的 model，確保 Base.metadata 包含對應表。
    """
    from models.database import Base  # noqa: F401 — 觸發核心 model 的 metadata 注冊

    # 明確注冊本測試依賴的 model（models.database 未涵蓋）
    import models.overtime_comp_leave_grant  # noqa: F401
    import models.unused_leave_payout_log  # noqa: F401

    db_path = tmp_path / "test_expire.sqlite"
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


# 用於產生唯一員工工號
_emp_id_counter = itertools.count(1)
# 用於產生唯一 OT record id（SQLite 不強制 FK 但 UNIQUE 約束仍生效）
_ot_record_counter = itertools.count(1)


@pytest.fixture
def employee_factory(session):
    """輕量員工 factory。

    預設值：is_active=True, employee_type='regular', base_salary=30000。
    時薪員工：employee_type='hourly', hourly_rate=<指定>。
    """
    from models.employee import Employee

    def _make(
        employee_type="regular",
        base_salary=30000,
        hourly_rate=0,
        is_active=True,
        hire_date=None,
    ):
        n = next(_emp_id_counter)
        emp = Employee(
            employee_id=f"E{n:04d}",
            name=f"測試員工{n}",
            employee_type=employee_type,
            base_salary=base_salary,
            hourly_rate=hourly_rate,
            is_active=is_active,
            hire_date=hire_date or date(2020, 1, 1),
        )
        session.add(emp)
        session.flush()
        return emp

    return _make


@pytest.fixture
def ot_grant_factory(session):
    """輕量 OvertimeCompLeaveGrant factory。

    每次呼叫都會先建一筆 OvertimeRecord（因 overtime_record_id 為 NOT NULL UNIQUE FK）。
    """
    from models.overtime import OvertimeRecord
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    def _make(
        emp_id,
        granted_hours,
        expires_at,
        status="active",
        consumed=0.0,
        granted_at=None,
    ):
        ot = OvertimeRecord(
            employee_id=emp_id,
            overtime_date=granted_at or date(2025, 1, 1),
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
            granted_at=granted_at or date(2025, 1, 1),
            expires_at=expires_at,
            consumed_hours=consumed,
            status=status,
        )
        session.add(grant)
        session.flush()
        return grant

    return _make


@pytest.fixture
def salary_record_factory(session):
    """輕量 SalaryRecord factory。

    僅填必要欄位；unused_leave_payout / is_finalized 由測試控制。
    """
    from models.salary import SalaryRecord

    def _make(emp_id, year, month, is_finalized=False, unused_leave_payout=0):
        sr = SalaryRecord(
            employee_id=emp_id,
            salary_year=year,
            salary_month=month,
            base_salary=30000,
            is_finalized=is_finalized,
            unused_leave_payout=Decimal(str(unused_leave_payout)),
        )
        session.add(sr)
        session.flush()
        return sr

    return _make


# ──────────────────────────────────────────────────────────────────────────────
# 測試案例
# ──────────────────────────────────────────────────────────────────────────────


def test_expire_no_active_grants_no_op(session):
    """無 expired grant 不會建 log。"""
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    summary = expire_comp_leave_grants(date(2026, 4, 1), session)
    assert summary == {
        "paid_employees": 0,
        "total_amount": 0.0,
        "expired_grant_count": 0,
    }


def test_expire_two_grants_one_employee_writes_one_log(
    session, employee_factory, ot_grant_factory
):
    """同員工兩筆到期 grant → 一筆 log 加總 + grant status='expired'。

    unexpired = (4-0) + (8-2) = 10 → amount = 10 × 200 = 2000
    """
    emp = employee_factory(employee_type="hourly", hourly_rate=200.0)
    g1 = ot_grant_factory(
        emp_id=emp.id,
        granted_hours=4.0,
        consumed=0.0,
        expires_at=date(2026, 3, 31),
        status="active",
    )
    g2 = ot_grant_factory(
        emp_id=emp.id,
        granted_hours=8.0,
        consumed=2.0,
        expires_at=date(2026, 3, 30),
        status="active",
    )

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    summary = expire_comp_leave_grants(date(2026, 4, 1), session)

    assert summary["paid_employees"] == 1
    assert summary["expired_grant_count"] == 2
    assert summary["total_amount"] == 2000.0

    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    logs = session.query(UnusedLeavePayoutLog).all()
    assert len(logs) == 1
    assert logs[0].source_type == "comp_grant_expiry"
    assert logs[0].amount == Decimal("2000.00")
    assert logs[0].meta["expired_grant_ids"] == [g1.id, g2.id]

    session.refresh(g1)
    session.refresh(g2)
    assert g1.status == "expired"
    assert g2.status == "expired"
    assert g1.payout_log_id == logs[0].id


def test_expire_skips_inactive_employee(session, employee_factory, ot_grant_factory):
    """is_active=False 員工跳過（由 offboarding path 處理）。"""
    emp = employee_factory(is_active=False)
    ot_grant_factory(
        emp_id=emp.id,
        granted_hours=4.0,
        expires_at=date(2026, 3, 31),
        status="active",
    )

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    summary = expire_comp_leave_grants(date(2026, 4, 1), session)
    assert summary["expired_grant_count"] == 0


def test_expire_fully_consumed_grant_marked_expired_no_log(
    session, employee_factory, ot_grant_factory
):
    """全用完的 grant 不建 log 但仍 mark expired。"""
    emp = employee_factory()
    g = ot_grant_factory(
        emp_id=emp.id,
        granted_hours=4.0,
        consumed=4.0,
        expires_at=date(2026, 3, 31),
        status="active",
    )

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    summary = expire_comp_leave_grants(date(2026, 4, 1), session)

    assert summary["paid_employees"] == 0

    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    assert session.query(UnusedLeavePayoutLog).count() == 0

    session.refresh(g)
    assert g.status == "expired"


def test_expire_writes_to_existing_unfinalized_salary_record(
    session, employee_factory, ot_grant_factory, salary_record_factory
):
    """目標月 SalaryRecord 已存在且未 finalize → 直寫 + 綁定 log。

    _next_month(2026-04-01) = (2026, 5)
    """
    emp = employee_factory(employee_type="hourly", hourly_rate=200.0)
    ot_grant_factory(
        emp_id=emp.id,
        granted_hours=4.0,
        expires_at=date(2026, 3, 31),
        status="active",
    )
    # _next_month(2026-04-01) = (2026, 5)
    sr = salary_record_factory(
        emp_id=emp.id, year=2026, month=5, is_finalized=False, unused_leave_payout=0
    )

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    expire_comp_leave_grants(date(2026, 4, 1), session)

    session.refresh(sr)
    assert sr.unused_leave_payout == Decimal("800.00")

    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    log = session.query(UnusedLeavePayoutLog).first()
    assert log.salary_record_id == sr.id


def test_expire_does_not_write_to_finalized_salary_record(
    session, employee_factory, ot_grant_factory, salary_record_factory
):
    """目標月 SalaryRecord 已 finalize → log.salary_record_id=NULL 由 layer 2 接手。"""
    emp = employee_factory(employee_type="hourly", hourly_rate=200.0)
    ot_grant_factory(
        emp_id=emp.id,
        granted_hours=4.0,
        expires_at=date(2026, 3, 31),
        status="active",
    )
    sr = salary_record_factory(
        emp_id=emp.id, year=2026, month=5, is_finalized=True, unused_leave_payout=0
    )

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    expire_comp_leave_grants(date(2026, 4, 1), session)

    session.refresh(sr)
    assert sr.unused_leave_payout == Decimal("0")  # 不動

    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    log = session.query(UnusedLeavePayoutLog).first()
    assert log.salary_record_id is None


def test_expire_accumulates_on_existing_nonzero_payout(
    session, employee_factory, ot_grant_factory, salary_record_factory
):
    """SR 已有 250 未休折現 → 累加不覆蓋（防 float+Decimal 回歸）。

    hourly_rate=200, granted_hours=4 → amount=800
    existing unused_leave_payout=250 → result should be 250+800=1050

    session.expire(sr) 強制 DB roundtrip，使 Money.process_result_value 回傳 float，
    重現 float + Decimal TypeError bug。
    """
    emp = employee_factory(employee_type="hourly", hourly_rate=200.0)
    ot_grant_factory(
        emp_id=emp.id,
        granted_hours=4.0,
        expires_at=date(2026, 3, 31),
        status="active",
    )
    sr = salary_record_factory(
        emp_id=emp.id, year=2026, month=5, unused_leave_payout=250
    )

    # 強制讓 SQLAlchemy 從 DB 重讀屬性，觸發 Money.process_result_value → float
    # 這重現了 production 路徑：SR 是別的 request 寫入後被此 session 查到
    session.expire(sr)

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    expire_comp_leave_grants(date(2026, 4, 1), session)

    session.refresh(sr)
    assert sr.unused_leave_payout == Decimal("1050")  # 250 + 800
