"""tests/test_leave_quota_lifecycle_integration.py — 端對端整合 + scheduler idempotency。

兩個 integration test：

1. test_full_lifecycle_ot_approve_then_partial_consume_then_expire_writes_salary
   - 模擬 OT 核准 → 補休 FIFO 消耗部分 → 到期 scheduler 結算 → SalaryRecord 寫入
   - 驗證：未消耗 5h × 時薪 200 = 1000 寫進 SR.unused_leave_payout 與 payout log

2. test_scheduler_idempotent_double_run_same_day_no_duplicate
   - 同日重跑 scheduler 不產生第二筆 log，grant 狀態不異動

設計決策（per plan §Task 18 permissible simplification）：
  - 不使用 SalaryEngine（cost 太高，Layer 1 直寫已足夠涵蓋）
  - 不使用 pytest-freezer；直接把 today 日期作為函式參數注入
  - SalaryRecord 於 scheduler 執行前預建（layer 1 path）
"""

import itertools
from datetime import date
from decimal import Decimal

import pytest
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ──────────────────────────────────────────────────────────────────────────────
# SQLite 相容性修補（與其他 T9/T12/T14 test 同步）
# ──────────────────────────────────────────────────────────────────────────────
_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    """SQLite 相容的 BigInteger 替代型別。"""

    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]

# 用於產生唯一員工工號
_emp_id_counter = itertools.count(1)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def session(tmp_path):
    """SQLite 測試 DB，建立所有相關 model schema。

    明確 import 所有本測試依賴的 model，確保 Base.metadata 包含對應表。
    """
    from models.database import Base  # noqa: F401 — 觸發核心 model 的 metadata 注冊

    # 明確注冊本測試依賴的 model（models.database 未涵蓋）
    import models.overtime_comp_leave_grant  # noqa: F401
    import models.unused_leave_payout_log  # noqa: F401
    import models.salary  # noqa: F401

    db_path = tmp_path / "test_lifecycle_integration.sqlite"
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


def _make_employee(session, *, hourly_rate: float = 200.0) -> "Employee":
    """建立 hourly 員工（hourly_rate=200 → 時薪即為 200，無需除法）。"""
    from models.employee import Employee

    n = next(_emp_id_counter)
    emp = Employee(
        employee_id=f"LC{n:04d}",
        name=f"整合測試員工{n}",
        employee_type="hourly",
        hourly_rate=hourly_rate,
        is_active=True,
        hire_date=date(2020, 1, 1),
    )
    session.add(emp)
    session.flush()
    return emp


def _make_overtime_record(session, employee_id: int, *, ot_date: date, hours: float):
    """建立已核准的補休模式加班記錄。"""
    from models.overtime import OvertimeRecord

    ot = OvertimeRecord(
        employee_id=employee_id,
        overtime_date=ot_date,
        overtime_type="weekday",
        hours=hours,
        use_comp_leave=True,
        comp_leave_granted=False,
        status="approved",
    )
    session.add(ot)
    session.flush()
    return ot


def _make_salary_record(
    session, employee_id: int, *, year: int, month: int, is_finalized: bool = False
):
    """建立薪資記錄（layer 1 直寫的前提：SR 存在且 is_finalized=False）。"""
    from models.salary import SalaryRecord

    sr = SalaryRecord(
        employee_id=employee_id,
        salary_year=year,
        salary_month=month,
        is_finalized=is_finalized,
        unused_leave_payout=Decimal("0"),
    )
    session.add(sr)
    session.flush()
    return sr


# ──────────────────────────────────────────────────────────────────────────────
# Test 1：完整生命週期
# ──────────────────────────────────────────────────────────────────────────────


def test_full_lifecycle_ot_approve_then_partial_consume_then_expire_writes_salary(
    session,
):
    """完整生命週期：OT 核准 → 補休 FIFO 消耗 3h → 到期 → SR 寫入 1000。

    時間軸（注入 today 日期，不依賴 freezer）：
      - T0（ot_date）: 2025-04-14
      - expires_at: 2025-04-14 + 365 = 2026-04-14
      - T+6 個月消耗（3h）：不依賴日期，只需呼叫 FIFO
      - scheduler today: 2026-04-15（> expires_at 2026-04-14 ✓）
      - _next_month(2026-04-15) = (2026, 5) → 目標薪資月

    數學：
      - granted_hours = 8, consumed_hours = 3 → unexpired = 5
      - amount = round_half_up(5 × 200) = 1000
      - SR.unused_leave_payout: 0 + 1000 = 1000
    """
    from api.overtimes import _grant_comp_leave_quota
    from api.leaves import _consume_compensatory_grants_fifo
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    # ── Step 1: 建員工 ──────────────────────────────────────────────────────
    emp = _make_employee(session, hourly_rate=200.0)

    # ── Step 2: T0 — OT 核准，建 grant（8h，expires 2026-04-14）──────────
    ot_date = date(2025, 4, 14)
    ot = _make_overtime_record(session, emp.id, ot_date=ot_date, hours=8.0)

    result: dict = {}
    _grant_comp_leave_quota(session, ot, result)
    session.flush()

    assert result.get("comp_leave_hours_granted") == 8.0
    grant = (
        session.query(OvertimeCompLeaveGrant)
        .filter_by(overtime_record_id=ot.id)
        .first()
    )
    assert grant is not None
    assert grant.status == "active"
    assert grant.granted_hours == pytest.approx(8.0)
    assert grant.expires_at == date(2026, 4, 14)  # 365 days later

    # ── Step 3: T+6 個月 — 員工請 3h 補休（FIFO 扣抵）────────────────────
    _consume_compensatory_grants_fifo(session, emp.id, hours=3.0)
    session.flush()

    session.refresh(grant)
    assert grant.consumed_hours == pytest.approx(3.0)

    # ── Step 4: 在 scheduler 執行前預建目標月 SalaryRecord（layer 1 path）
    #    目標月 = _next_month(today=2026-04-15) = (2026, 5)
    sr = _make_salary_record(session, emp.id, year=2026, month=5, is_finalized=False)
    session.commit()

    # ── Step 5: scheduler today=2026-04-15（> expires_at 2026-04-14）────
    today = date(2026, 4, 15)
    summary = expire_comp_leave_grants(today, session)
    session.commit()

    # ── Step 6: 驗證 scheduler 回傳摘要 ─────────────────────────────────
    assert summary["paid_employees"] == 1
    assert summary["total_amount"] == pytest.approx(1000.0)
    assert summary["expired_grant_count"] == 1

    # ── Step 7: 驗證 grant 狀態更新 ──────────────────────────────────────
    session.refresh(grant)
    assert grant.status == "expired"
    assert grant.expired_at is not None

    # ── Step 8: 驗證 payout log 建立 ─────────────────────────────────────
    log = (
        session.query(UnusedLeavePayoutLog)
        .filter_by(employee_id=emp.id, source_type="comp_grant_expiry")
        .first()
    )
    assert log is not None
    assert log.hours == pytest.approx(5.0)  # 8 - 3
    assert log.amount == Decimal("1000")
    assert log.salary_period_year == 2026
    assert log.salary_period_month == 5

    # ── Step 9: 驗證 SR.unused_leave_payout 直寫（layer 1）──────────────
    session.refresh(sr)
    assert sr.unused_leave_payout == Decimal("1000")
    assert log.salary_record_id == sr.id  # 反向綁定確認


# ──────────────────────────────────────────────────────────────────────────────
# Test 2：scheduler idempotency — 同日重跑不產生第二筆 log
# ──────────────────────────────────────────────────────────────────────────────


def test_scheduler_idempotent_double_run_same_day_no_duplicate(session):
    """同日重跑 scheduler 不建第二筆 log，grant 不再被選中（status='expired'）。

    第一次跑：grant active → mark expired，建 payout log
    第二次跑（同 today）：
      - query 過濾 status='active' → 撈不到任何 grant
      - 無新 log 建立
      - 無任何 grant 狀態改動
    """
    from api.overtimes import _grant_comp_leave_quota
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants

    # ── 建員工 + OT + grant ───────────────────────────────────────────────
    emp = _make_employee(session, hourly_rate=200.0)

    ot_date = date(2025, 6, 1)
    ot = _make_overtime_record(session, emp.id, ot_date=ot_date, hours=4.0)

    _grant_comp_leave_quota(session, ot, {})
    session.flush()

    grant = (
        session.query(OvertimeCompLeaveGrant)
        .filter_by(overtime_record_id=ot.id)
        .first()
    )
    assert grant.status == "active"
    assert grant.expires_at == date(2026, 6, 1)  # 365 days from 2025-06-01

    # ── 預建目標月 SR（防止 layer 1 miss 變成 layer 2 僅驗 log）────────────
    sr = _make_salary_record(session, emp.id, year=2026, month=7, is_finalized=False)
    session.commit()

    # ── 第一次跑：today = 2026-06-02（> expires_at 2026-06-01）──────────
    today = date(2026, 6, 2)
    summary1 = expire_comp_leave_grants(today, session)
    session.commit()

    assert summary1["paid_employees"] == 1
    assert summary1["expired_grant_count"] == 1

    log_count_after_first = (
        session.query(UnusedLeavePayoutLog)
        .filter_by(employee_id=emp.id, source_type="comp_grant_expiry")
        .count()
    )
    assert log_count_after_first == 1

    # ── 第二次跑：同一個 today ─────────────────────────────────────────────
    summary2 = expire_comp_leave_grants(today, session)
    session.commit()

    # 沒有任何 active grant 可撈 → 無 paid_employees
    assert summary2["paid_employees"] == 0
    assert summary2["expired_grant_count"] == 0
    assert summary2["total_amount"] == pytest.approx(0.0)

    # log 數量不變
    log_count_after_second = (
        session.query(UnusedLeavePayoutLog)
        .filter_by(employee_id=emp.id, source_type="comp_grant_expiry")
        .count()
    )
    assert log_count_after_second == 1  # 不得新增第二筆

    # grant 狀態不變（仍是 expired，不會被翻回 active 或產生副作用）
    session.refresh(grant)
    assert grant.status == "expired"

    # SR.unused_leave_payout 保持第一次的值，不重複累加
    session.refresh(sr)
    expected_amount = Decimal("800")  # 4h × 200
    assert sr.unused_leave_payout == expected_amount
