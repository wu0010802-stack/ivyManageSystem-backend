"""驗證 _revoke_comp_leave_grant 同步標記 grant status='revoked'。"""

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

    db_path = tmp_path / "test_revoke_ledger.sqlite"
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


def test_revoke_marks_grant_revoked_not_deleted(session, employee_factory):
    """撤銷加班時，關聯 grant status 標記 'revoked'，不刪除（保留 audit trail）"""
    from api.overtimes import _revoke_comp_leave_grant
    from models.overtime import OvertimeRecord
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    from models.leave import LeaveQuota

    emp = employee_factory()

    # 建立年度補休配額
    quota = LeaveQuota(
        employee_id=emp.id,
        year=2026,
        leave_type="compensatory",
        total_hours=4.0,
    )
    session.add(quota)
    session.flush()

    # 建立核准的加班記錄（comp_leave_granted=True）
    ot = OvertimeRecord(
        employee_id=emp.id,
        overtime_date=date(2026, 4, 1),
        overtime_type="weekday",
        hours=4.0,
        use_comp_leave=True,
        comp_leave_granted=True,
        status="approved",
    )
    session.add(ot)
    session.flush()

    # 建立 grant ledger row（status='active'）
    grant = OvertimeCompLeaveGrant(
        overtime_record_id=ot.id,
        employee_id=emp.id,
        granted_hours=4.0,
        granted_at=date(2026, 4, 1),
        expires_at=date(2027, 4, 1),
        status="active",
    )
    session.add(grant)
    session.flush()

    # 撤銷加班
    _revoke_comp_leave_grant(session, ot)
    session.flush()  # 確保變更寫入 session

    # 驗證：grant status 為 'revoked'，未被刪除
    session.refresh(grant)
    assert grant.status == "revoked", f"grant.status 應為 'revoked' 但為 {grant.status}"
    assert ot.comp_leave_granted is False

    # 驗證配額已調整
    session.refresh(quota)
    assert quota.total_hours == 0.0


def test_revoke_no_grant_found(session, employee_factory):
    """加班配額不存在時（quotas table 缺項），revoke 應安全返回而不拋錯"""
    from api.overtimes import _revoke_comp_leave_grant
    from models.overtime import OvertimeRecord

    emp = employee_factory()

    # 建立加班記錄但不建配額（故意缺項）
    ot = OvertimeRecord(
        employee_id=emp.id,
        overtime_date=date(2026, 4, 1),
        overtime_type="weekday",
        hours=4.0,
        use_comp_leave=True,
        comp_leave_granted=True,
        status="approved",
    )
    session.add(ot)
    session.flush()

    # revoke 應因找不到 quota 而早期返回
    _revoke_comp_leave_grant(session, ot)

    # 驗證：ot 狀態被修改為 not granted
    assert ot.comp_leave_granted is False


def test_revoke_idempotent_already_revoked(session, employee_factory):
    """加班已撤銷（comp_leave_granted=False），呼叫 revoke 應早期返回，不重複標記"""
    from api.overtimes import _revoke_comp_leave_grant
    from models.overtime import OvertimeRecord
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    from models.leave import LeaveQuota

    emp = employee_factory()

    # 建立年度補休配額
    quota = LeaveQuota(
        employee_id=emp.id,
        year=2026,
        leave_type="compensatory",
        total_hours=0.0,
    )
    session.add(quota)
    session.flush()

    # 建立已撤銷的加班記錄（comp_leave_granted=False）
    ot = OvertimeRecord(
        employee_id=emp.id,
        overtime_date=date(2026, 4, 1),
        overtime_type="weekday",
        hours=4.0,
        use_comp_leave=True,
        comp_leave_granted=False,
        status="approved",
    )
    session.add(ot)
    session.flush()

    # 不會有 grant row（early return）
    _revoke_comp_leave_grant(session, ot)

    # 驗證：配額不變，流程正常返回
    session.refresh(quota)
    assert quota.total_hours == 0.0
