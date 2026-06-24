"""MF-1（金流深掃 F1, P1）回歸：批次薪資須與單筆一致載入才藝鐘點明細。

才藝老師薪資唯一真實來源為 ArtTeacherPayrollEntry（引擎以 sum(total_amount)
覆寫 hourly_total）。single 路徑 _build_breakdown_for_month 會載入並設
emp_dict['art_teacher_entries_total']；但 bulk 路徑 _compute_and_persist_single_employee
原本未載入 → consumer 退回考勤打卡 hourly_calculated_pay（才藝老師多不打卡 → 0），
HR 按「計算全部」即把 hourly_total/gross/net 抹成 0 並 persist。

本測試以同一 hourly 員工 + 數筆 entries（無考勤）跑 single 與 bulk，斷言兩路徑
gross_salary / net_salary 相同且等於 sum(entries.total_amount)。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import ArtTeacherPayrollEntry, Base, Employee, SalaryRecord
from services.salary_engine import SalaryEngine

_YEAR, _MONTH = 2026, 2
_ENTRY_TOTAL = 18000.0  # 三筆 entries 合計


@pytest.fixture
def salary_engine_db(tmp_path):
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'art-bulk-parity.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)
    yield SalaryEngine(load_from_db=False), session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _hourly_art_teacher(session):
    emp = Employee(
        employee_id="ART_T",
        name="才藝老師",
        title="才藝老師",
        position="才藝老師",
        employee_type="hourly",
        base_salary=0,
        hourly_rate=500,
        insurance_salary_level=0,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    # 三筆當月鐘點明細，合計 _ENTRY_TOTAL；不建任何考勤（hourly_calculated_pay=0）
    for i, amt in enumerate((8000, 6000, 4000)):
        session.add(
            ArtTeacherPayrollEntry(
                employee_id=emp.id,
                salary_year=_YEAR,
                salary_month=_MONTH,
                subject=f"才藝{i}",
                total_amount=amt,
            )
        )
    session.flush()
    return emp.id


def _snap(session_factory, emp_id):
    with session_factory() as s:
        r = (
            s.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == emp_id,
                SalaryRecord.salary_year == _YEAR,
                SalaryRecord.salary_month == _MONTH,
            )
            .one()
        )
        return {
            "gross_salary": float(r.gross_salary or 0),
            "net_salary": float(r.net_salary or 0),
        }


def _delete_month(session_factory):
    with session_factory() as s:
        s.query(SalaryRecord).filter(
            SalaryRecord.salary_year == _YEAR, SalaryRecord.salary_month == _MONTH
        ).delete()
        s.commit()


def test_art_teacher_hourly_total_parity_single_vs_bulk(salary_engine_db):
    engine, session_factory = salary_engine_db
    with session_factory() as s:
        emp_id = _hourly_art_teacher(s)
        s.commit()

    # 單筆路徑（會載入 art entries）
    engine.process_salary_calculation(emp_id, _YEAR, _MONTH)
    single = _snap(session_factory, emp_id)
    _delete_month(session_factory)

    # 批次路徑（修正前未載入 art entries → gross 退回打卡 0）
    engine.process_bulk_salary_calculation([emp_id], _YEAR, _MONTH)
    bulk = _snap(session_factory, emp_id)

    assert (
        single["gross_salary"] == _ENTRY_TOTAL
    ), f"single gross 應為鐘點合計 {_ENTRY_TOTAL}，得 {single['gross_salary']}"
    assert (
        bulk == single
    ), f"bulk 須與 single 一致載入才藝鐘點明細\nsingle={single}\nbulk={bulk}"
