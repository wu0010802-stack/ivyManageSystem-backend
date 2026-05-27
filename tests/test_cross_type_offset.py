"""驗證 services.approval.cross_type_offset.resolve_cross_type_offset。

涵蓋：
1. flag on + 同日已核准 OT → 回傳該 OT
2. flag off → 回傳 None（即使有匹配 OT）
3. 補休假單（source_overtime_id 非空）→ 回傳 None（避免雙重抵扣）
4. OT 未核准（status="pending" / False）→ 不匹配
5. OT 用補休（use_comp_leave=True）→ 不匹配
6. 同員工同日無匹配 OT → 回傳 None
7. 多筆匹配時依 id 升序回最舊那筆（決定性）
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, LeaveRecord, OvertimeRecord
from services.approval.cross_type_offset import resolve_cross_type_offset


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "cross-type-offset.sqlite"
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

    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


def _make_employee(session, *, employee_id="E001", name="王小明") -> Employee:
    e = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


def _make_leave(
    session,
    *,
    employee_id: int,
    start: date = date(2026, 3, 10),
    end: date = date(2026, 3, 10),
    source_overtime_id: int | None = None,
) -> LeaveRecord:
    leave = LeaveRecord(
        employee_id=employee_id,
        leave_type="personal",
        start_date=start,
        end_date=end,
        leave_hours=8,
        is_deductible=True,
        deduction_ratio=1.0,
        source_overtime_id=source_overtime_id,
    )
    session.add(leave)
    session.flush()
    return leave


def _make_overtime(
    session,
    *,
    employee_id: int,
    ot_date: date = date(2026, 3, 10),
    status="approved",
    use_comp_leave: bool = False,
    overtime_pay: float = 500,
) -> OvertimeRecord:
    ot = OvertimeRecord(
        employee_id=employee_id,
        overtime_date=ot_date,
        overtime_type="weekday",
        hours=2,
        overtime_pay=overtime_pay,
        status=status,
        use_comp_leave=use_comp_leave,
    )
    session.add(ot)
    session.flush()
    return ot


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────


def test_offset_returns_matching_ot_when_flag_on(db_session, monkeypatch):
    monkeypatch.setenv("ENABLE_LEAVE_OT_OFFSET", "true")
    emp = _make_employee(db_session)
    ot = _make_overtime(db_session, employee_id=emp.id)
    leave = _make_leave(db_session, employee_id=emp.id)

    result = resolve_cross_type_offset(db_session, leave)
    assert result is not None
    assert result.id == ot.id


def test_offset_returns_none_when_flag_off(db_session, monkeypatch):
    # 顯式清空，避免 shell 環境洩漏
    monkeypatch.delenv("ENABLE_LEAVE_OT_OFFSET", raising=False)
    emp = _make_employee(db_session)
    _make_overtime(db_session, employee_id=emp.id)
    leave = _make_leave(db_session, employee_id=emp.id)

    assert resolve_cross_type_offset(db_session, leave) is None


def test_offset_skips_compensatory_leave(db_session, monkeypatch):
    """補休假單已綁定 source OT，不再做跨類 offset。"""
    monkeypatch.setenv("ENABLE_LEAVE_OT_OFFSET", "true")
    emp = _make_employee(db_session)
    source_ot = _make_overtime(db_session, employee_id=emp.id, use_comp_leave=True)
    # 另造一筆「正常」OT 同日
    _make_overtime(db_session, employee_id=emp.id, use_comp_leave=False)
    leave = _make_leave(db_session, employee_id=emp.id, source_overtime_id=source_ot.id)

    assert resolve_cross_type_offset(db_session, leave) is None


def test_offset_skips_unapproved_ot(db_session, monkeypatch):
    monkeypatch.setenv("ENABLE_LEAVE_OT_OFFSET", "true")
    emp = _make_employee(db_session)
    _make_overtime(db_session, employee_id=emp.id, status="pending")
    _make_overtime(db_session, employee_id=emp.id, status="rejected")
    leave = _make_leave(db_session, employee_id=emp.id)

    assert resolve_cross_type_offset(db_session, leave) is None


def test_offset_skips_comp_leave_ot(db_session, monkeypatch):
    """OT 已選擇『以補休代替加班費』，不應再被 leave 抵扣。"""
    monkeypatch.setenv("ENABLE_LEAVE_OT_OFFSET", "true")
    emp = _make_employee(db_session)
    _make_overtime(db_session, employee_id=emp.id, use_comp_leave=True)
    leave = _make_leave(db_session, employee_id=emp.id)

    assert resolve_cross_type_offset(db_session, leave) is None


def test_offset_returns_none_when_no_matching_ot(db_session, monkeypatch):
    monkeypatch.setenv("ENABLE_LEAVE_OT_OFFSET", "true")
    emp = _make_employee(db_session)
    # OT 在另一天
    _make_overtime(db_session, employee_id=emp.id, ot_date=date(2026, 3, 11))
    leave = _make_leave(db_session, employee_id=emp.id, start=date(2026, 3, 10))

    assert resolve_cross_type_offset(db_session, leave) is None


def test_offset_deterministic_order_when_multiple_matches(db_session, monkeypatch):
    """多筆匹配時應依 id 升序回傳最舊那筆。"""
    monkeypatch.setenv("ENABLE_LEAVE_OT_OFFSET", "true")
    emp = _make_employee(db_session)
    ot1 = _make_overtime(db_session, employee_id=emp.id)
    ot2 = _make_overtime(db_session, employee_id=emp.id)
    assert ot1.id < ot2.id
    leave = _make_leave(db_session, employee_id=emp.id)

    result = resolve_cross_type_offset(db_session, leave)
    assert result is not None
    assert result.id == ot1.id
