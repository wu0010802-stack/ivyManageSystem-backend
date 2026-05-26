"""驗證 _grant_comp_leave_quota 同步建 OvertimeCompLeaveGrant row。"""

import itertools
from datetime import date

import pytest
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

# 用於產生唯一員工工號
_emp_id_counter = itertools.count(1)


@pytest.fixture
def session(tmp_path):
    """SQLite in-memory 測試 DB，建立全 schema。

    明確 import 所有本測試依賴的 model，確保 Base.metadata 包含對應表。
    """
    from models.database import Base  # noqa: F401 — 觸發核心 model 的 metadata 注冊

    # 明確注冊本測試依賴的 model（models.database 未涵蓋）
    import models.overtime_comp_leave_grant  # noqa: F401
    import models.unused_leave_payout_log  # noqa: F401

    db_path = tmp_path / "test_grant_ledger.sqlite"
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


@pytest.fixture
def employee_factory(session):
    """輕量員工 factory。"""
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


def test_grant_comp_leave_creates_ledger_row(session, employee_factory):
    from api.overtimes import _grant_comp_leave_quota
    from models.overtime import OvertimeRecord
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()
    ot = OvertimeRecord(
        employee_id=emp.id,
        overtime_date=date(2026, 4, 1),
        overtime_type="weekday",
        hours=4.0,
        use_comp_leave=True,
        comp_leave_granted=False,
        is_approved=True,
    )
    session.add(ot)
    session.flush()

    result = {}
    _grant_comp_leave_quota(session, ot, result)

    assert ot.comp_leave_granted is True
    assert result["comp_leave_hours_granted"] == 4.0

    grant = (
        session.query(OvertimeCompLeaveGrant)
        .filter_by(overtime_record_id=ot.id)
        .first()
    )
    assert grant is not None
    assert grant.granted_hours == 4.0
    assert grant.granted_at == date(2026, 4, 1)
    assert grant.expires_at == date(2027, 4, 1)
    assert grant.status == "active"


def test_grant_comp_leave_idempotent_does_not_duplicate(session, employee_factory):
    """重複 call 不會建第二筆 grant（comp_leave_granted=True early return）"""
    from api.overtimes import _grant_comp_leave_quota
    from models.overtime import OvertimeRecord
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()
    ot = OvertimeRecord(
        employee_id=emp.id,
        overtime_date=date(2026, 4, 1),
        overtime_type="weekday",
        hours=4.0,
        use_comp_leave=True,
        comp_leave_granted=False,
        is_approved=True,
    )
    session.add(ot)
    session.flush()

    _grant_comp_leave_quota(session, ot, {})
    _grant_comp_leave_quota(session, ot, {})

    grants = (
        session.query(OvertimeCompLeaveGrant).filter_by(overtime_record_id=ot.id).all()
    )
    assert len(grants) == 1
