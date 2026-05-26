"""補休假單核准時 FIFO 從最早 expires_at 扣 consumed_hours。"""

import itertools
from datetime import date

import pytest
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
    """SQLite in-memory 測試 DB，建立全 schema。"""
    from models.database import Base  # noqa: F401 — 觸發核心 model 的 metadata 注冊

    # 明確注冊本測試依賴的 model
    import models.overtime_comp_leave_grant  # noqa: F401
    import models.unused_leave_payout_log  # noqa: F401

    db_path = tmp_path / "test_fifo.sqlite"
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


def _make_overtime(session, employee_id: int, ot_date=None, hours=8.0):
    """建立 OvertimeRecord（確保 FK 完整性）。"""
    from models.overtime import OvertimeRecord

    ot = OvertimeRecord(
        employee_id=employee_id,
        overtime_date=ot_date or date(2025, 4, 1),
        overtime_type="weekday",
        hours=hours,
        use_comp_leave=True,
        comp_leave_granted=True,
        is_approved=True,
    )
    session.add(ot)
    session.flush()
    return ot


def test_consume_fifo_two_grants(session, employee_factory):
    """FIFO 從最早 expires_at 扣 consumed_hours，跨兩筆 grant。"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()

    ot1 = _make_overtime(session, emp.id, date(2025, 4, 1))
    ot2 = _make_overtime(session, emp.id, date(2025, 5, 1))

    # g1 expires 較早，g2 較晚
    g1 = OvertimeCompLeaveGrant(
        overtime_record_id=ot1.id,
        employee_id=emp.id,
        granted_hours=4.0,
        granted_at=date(2025, 4, 1),
        expires_at=date(2026, 4, 1),
        consumed_hours=0,
        status="active",
    )
    g2 = OvertimeCompLeaveGrant(
        overtime_record_id=ot2.id,
        employee_id=emp.id,
        granted_hours=8.0,
        granted_at=date(2025, 5, 1),
        expires_at=date(2026, 5, 1),
        consumed_hours=0,
        status="active",
    )
    session.add_all([g1, g2])
    session.flush()

    from api.leaves import _consume_compensatory_grants_fifo

    _consume_compensatory_grants_fifo(session, emp.id, hours=6.0)
    session.flush()

    session.refresh(g1)
    session.refresh(g2)
    assert g1.consumed_hours == 4.0  # g1 全用完
    assert g2.consumed_hours == 2.0  # g2 補 2 小時


def test_consume_fifo_exact_amount(session, employee_factory):
    """精確扣完單筆 grant 不動另一筆。"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()

    ot1 = _make_overtime(session, emp.id, date(2025, 4, 1))
    ot2 = _make_overtime(session, emp.id, date(2025, 5, 1))

    g1 = OvertimeCompLeaveGrant(
        overtime_record_id=ot1.id,
        employee_id=emp.id,
        granted_hours=4.0,
        granted_at=date(2025, 4, 1),
        expires_at=date(2026, 4, 1),
        consumed_hours=0,
        status="active",
    )
    g2 = OvertimeCompLeaveGrant(
        overtime_record_id=ot2.id,
        employee_id=emp.id,
        granted_hours=8.0,
        granted_at=date(2025, 5, 1),
        expires_at=date(2026, 5, 1),
        consumed_hours=0,
        status="active",
    )
    session.add_all([g1, g2])
    session.flush()

    from api.leaves import _consume_compensatory_grants_fifo

    _consume_compensatory_grants_fifo(session, emp.id, hours=4.0)
    session.flush()

    session.refresh(g1)
    session.refresh(g2)
    assert g1.consumed_hours == 4.0
    assert g2.consumed_hours == 0.0  # 未動


def test_consume_fifo_insufficient_raises(session, employee_factory):
    """總 grant 不足時 raise ValueError。"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()

    ot1 = _make_overtime(session, emp.id, date(2025, 4, 1))

    g1 = OvertimeCompLeaveGrant(
        overtime_record_id=ot1.id,
        employee_id=emp.id,
        granted_hours=2.0,
        granted_at=date(2025, 4, 1),
        expires_at=date(2026, 4, 1),
        consumed_hours=0,
        status="active",
    )
    session.add(g1)
    session.flush()

    from api.leaves import _consume_compensatory_grants_fifo

    with pytest.raises(ValueError, match="補休 grant 不足扣抵"):
        _consume_compensatory_grants_fifo(session, emp.id, hours=5.0)


def test_release_compensatory_leave_releases_consumed(session, employee_factory):
    """退回 consumed_hours：從最早 expires_at 的 grant 退。"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()

    ot1 = _make_overtime(session, emp.id, date(2025, 4, 1))

    g1 = OvertimeCompLeaveGrant(
        overtime_record_id=ot1.id,
        employee_id=emp.id,
        granted_hours=4.0,
        granted_at=date(2025, 4, 1),
        expires_at=date(2026, 4, 1),
        consumed_hours=3.0,
        status="active",
    )
    session.add(g1)
    session.flush()

    from api.leaves import _release_compensatory_grants_fifo

    _release_compensatory_grants_fifo(session, emp.id, hours=3.0)
    session.flush()

    session.refresh(g1)
    assert g1.consumed_hours == 0.0


def test_release_fifo_partial_across_two_grants(session, employee_factory):
    """退回時跨多筆 grant 按 FIFO 退（最早優先退完）。"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()

    ot1 = _make_overtime(session, emp.id, date(2025, 4, 1))
    ot2 = _make_overtime(session, emp.id, date(2025, 5, 1))

    g1 = OvertimeCompLeaveGrant(
        overtime_record_id=ot1.id,
        employee_id=emp.id,
        granted_hours=4.0,
        granted_at=date(2025, 4, 1),
        expires_at=date(2026, 4, 1),
        consumed_hours=4.0,
        status="active",
    )
    g2 = OvertimeCompLeaveGrant(
        overtime_record_id=ot2.id,
        employee_id=emp.id,
        granted_hours=8.0,
        granted_at=date(2025, 5, 1),
        expires_at=date(2026, 5, 1),
        consumed_hours=2.0,
        status="active",
    )
    session.add_all([g1, g2])
    session.flush()

    from api.leaves import _release_compensatory_grants_fifo

    _release_compensatory_grants_fifo(session, emp.id, hours=5.0)
    session.flush()

    session.refresh(g1)
    session.refresh(g2)
    assert g1.consumed_hours == 0.0  # 全退
    assert g2.consumed_hours == 1.0  # 退了 1


def test_consume_skips_fully_consumed_grant(session, employee_factory):
    """已全部 consumed 的 grant 跳過，從下一筆扣。"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()

    ot1 = _make_overtime(session, emp.id, date(2025, 4, 1))
    ot2 = _make_overtime(session, emp.id, date(2025, 5, 1))

    g1 = OvertimeCompLeaveGrant(
        overtime_record_id=ot1.id,
        employee_id=emp.id,
        granted_hours=4.0,
        granted_at=date(2025, 4, 1),
        expires_at=date(2026, 4, 1),
        consumed_hours=4.0,  # 已全部用完
        status="active",
    )
    g2 = OvertimeCompLeaveGrant(
        overtime_record_id=ot2.id,
        employee_id=emp.id,
        granted_hours=8.0,
        granted_at=date(2025, 5, 1),
        expires_at=date(2026, 5, 1),
        consumed_hours=0,
        status="active",
    )
    session.add_all([g1, g2])
    session.flush()

    from api.leaves import _consume_compensatory_grants_fifo

    _consume_compensatory_grants_fifo(session, emp.id, hours=3.0)
    session.flush()

    session.refresh(g1)
    session.refresh(g2)
    assert g1.consumed_hours == 4.0  # 未動
    assert g2.consumed_hours == 3.0  # 從 g2 扣
