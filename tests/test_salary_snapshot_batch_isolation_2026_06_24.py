"""崩潰防護 P2：月底薪資快照批次須 per-row 隔離，單筆壞資料不可中斷整月所有員工快照。

問題：create_month_end_snapshots 的 per-record try 只接 IntegrityError，且 row-copy
（_copy_record_to_snapshot）在 try 外。若某筆 SalaryRecord 在 copy 或 flush 觸發
非 Integrity 例外（DataError / 欄位層異常），會逸出迴圈 → 整月所有員工的快照全失敗。

修法：row-copy 移進 try；except 增廣為「非 Integrity 例外 → log + 跳過該筆續跑」，
其餘員工照常快照。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import services.finance.salary_snapshot_service as snap_svc
from models.base import Base
from models.database import Employee, SalaryRecord
from models.salary import SalarySnapshot


@pytest.fixture
def sf(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'snap-iso.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(bind=engine)
    old_e, old_s = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, factory
    Base.metadata.create_all(engine)
    yield factory
    base_module._engine, base_module._SessionFactory = old_e, old_s
    engine.dispose()


def _make_record(session, name, year=2026, month=3):
    emp = Employee(
        employee_id=f"E_{name}",
        name=name,
        base_salary=30000,
        employee_type="regular",
        is_active=True,
    )
    session.add(emp)
    session.flush()
    session.add(
        SalaryRecord(
            employee_id=emp.id,
            salary_year=year,
            salary_month=month,
            base_salary=30000,
            health_insurance_employee=500,
            labor_insurance_employee=300,
            gross_salary=30000,
            total_deduction=800,
            net_salary=29200,
            version=1,
            is_finalized=False,
        )
    )
    return emp.id


def test_one_bad_row_does_not_abort_whole_month(sf, monkeypatch):
    """中間一筆觸發非 IntegrityError，其餘員工仍應完成快照（不整月失敗）。"""
    with sf() as session:
        _make_record(session, "good1")
        bad_emp_id = _make_record(session, "bad")
        _make_record(session, "good3")
        session.commit()

    real_copy = snap_svc._copy_record_to_snapshot

    def flaky_copy(r, *args, **kwargs):
        if r.employee_id == bad_emp_id:
            raise ValueError("simulated DataError on legacy field")
        return real_copy(r, *args, **kwargs)

    monkeypatch.setattr(snap_svc, "_copy_record_to_snapshot", flaky_copy)

    with sf() as session:
        created = snap_svc.create_month_end_snapshots(session, 2026, 3)
        session.commit()

    # 壞那筆跳過，其餘 2 筆完成
    assert created == 2, f"應建立 2 筆（壞那筆跳過），實得 {created}"
    with sf() as session:
        rows = (
            session.query(SalarySnapshot)
            .filter(
                SalarySnapshot.salary_year == 2026,
                SalarySnapshot.salary_month == 3,
                SalarySnapshot.snapshot_type == "month_end",
            )
            .all()
        )
    assert len(rows) == 2
    assert bad_emp_id not in {r.employee_id for r in rows}


def test_all_good_rows_still_snapshot(sf):
    """無壞資料時行為不變：全部建立。"""
    with sf() as session:
        _make_record(session, "a")
        _make_record(session, "b")
        session.commit()
    with sf() as session:
        created = snap_svc.create_month_end_snapshots(session, 2026, 3)
        session.commit()
    assert created == 2
