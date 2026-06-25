"""補休 ledger 一致性回歸（rank 5 / 11 / 12 / 15）。

- rank 5：撤銷/刪除「已被 FIFO 消耗」的補休加班 → CASCADE 刪 grant 使 consumed 帳蒸發、
  surviving grant 到期重複折現。修補：_revoke_comp_leave_grant 對 consumed_hours>0 的
  grant 硬擋 409。
- rank 15：補休發放/撤銷寫的 LeaveQuota 列須與配額檢查讀的列一致（school_year 對齊）。
- rank 11：grant 到期折現後須同步扣減 LeaveQuota.total_hours，否則檢查見幽靈額度。
- rank 12：到期折現須 reserve 待審補休假時數（避免折現後假核准無 active grant 可消耗）。
"""

import itertools
from datetime import date, datetime

import pytest
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# SQLite 相容性修補（對齊 test_offboarding_prefill / test_expire_comp_leave_grants）
_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]

_counter = itertools.count(1)


@pytest.fixture
def session(tmp_path):
    from models.database import Base  # noqa: F401
    import models.overtime_comp_leave_grant  # noqa: F401
    import models.unused_leave_payout_log  # noqa: F401

    db_path = tmp_path / "ledger.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    s = sf()
    yield s
    s.close()
    engine.dispose()


def _emp(session, *, is_active=True):
    from models.employee import Employee

    n = next(_counter)
    e = Employee(
        employee_id=f"LDG{n:04d}",
        name=f"員工{n}",
        base_salary=36000,
        is_active=is_active,
        hire_date=date(2020, 1, 1),
    )
    session.add(e)
    session.flush()
    return e


def _ot(session, emp_id, *, hours=8.0, d=date(2026, 9, 15), granted=True):
    from models.overtime import OvertimeRecord

    ot = OvertimeRecord(
        employee_id=emp_id,
        overtime_date=d,
        overtime_type="weekday",
        hours=hours,
        use_comp_leave=True,
        comp_leave_granted=granted,
        status="approved",
    )
    session.add(ot)
    session.flush()
    return ot


def _grant(
    session,
    emp_id,
    ot_id,
    *,
    granted=8.0,
    consumed=0.0,
    status="active",
    granted_at=date(2026, 9, 15),
    expires_at=date(2027, 9, 15),
):
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    g = OvertimeCompLeaveGrant(
        overtime_record_id=ot_id,
        employee_id=emp_id,
        granted_hours=granted,
        consumed_hours=consumed,
        granted_at=granted_at,
        expires_at=expires_at,
        status=status,
    )
    session.add(g)
    session.flush()
    return g


def _comp_leave(
    session,
    emp_id,
    *,
    hours=8.0,
    status="approved",
    source_ot_id=None,
    start=date(2026, 9, 18),
):
    from models.leave import LeaveRecord

    lv = LeaveRecord(
        employee_id=emp_id,
        leave_type="compensatory",
        start_date=start,
        end_date=start,
        leave_hours=hours,
        status=status,
        is_deductible=False,
        deduction_ratio=0.0,
        source_overtime_id=source_ot_id,
    )
    session.add(lv)
    session.flush()
    return lv


def _quota(session, emp_id, *, total=16.0, year=2026):
    from models.leave import LeaveQuota

    q = LeaveQuota(
        employee_id=emp_id, year=year, leave_type="compensatory", total_hours=total
    )
    session.add(q)
    session.flush()
    return q


# ── rank 5 ──────────────────────────────────────────────────────────────


def test_revoke_consumed_grant_blocked_even_when_leave_unlinked(session):
    """已被 FIFO 消耗的 grant（補休假 source_overtime_id=NULL）撤銷時應 409，
    不可讓 consumed 帳隨 CASCADE 蒸發。"""
    from fastapi import HTTPException
    from api.overtimes import _revoke_comp_leave_grant

    emp = _emp(session)
    ot_a = _ot(session, emp.id, hours=8.0, d=date(2026, 9, 15))
    ot_b = _ot(session, emp.id, hours=8.0, d=date(2026, 9, 22))
    _grant(session, emp.id, ot_a.id, granted=8.0, consumed=8.0)  # 已被 FIFO 消耗
    _grant(session, emp.id, ot_b.id, granted=8.0, consumed=0.0)
    _quota(session, emp.id, total=16.0)
    # source_overtime_id=NULL 的已核准補休假（模擬 admin 建立 + FIFO 消耗 grant_A）
    _comp_leave(session, emp.id, hours=8.0, status="approved", source_ot_id=None)
    session.commit()

    with pytest.raises(HTTPException) as exc:
        _revoke_comp_leave_grant(session, ot_a)
    assert exc.value.status_code == 409
    assert "已被使用" in exc.value.detail or "撤銷" in exc.value.detail


def test_revoke_unconsumed_grant_allowed(session):
    """未被消耗的 grant 撤銷不受影響（控制案例，修前修後皆通過）。"""
    from api.overtimes import _revoke_comp_leave_grant

    emp = _emp(session)
    ot = _ot(session, emp.id, hours=8.0)
    _grant(session, emp.id, ot.id, granted=8.0, consumed=0.0)
    _quota(session, emp.id, total=8.0)
    session.commit()

    _revoke_comp_leave_grant(session, ot)  # 不應 raise
    assert ot.comp_leave_granted is False
