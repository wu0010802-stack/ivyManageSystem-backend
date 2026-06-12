"""tests/test_cascade_restrict_guards.py — cascade 地雷拆除回歸（設計體檢 2026-06-12 Finding 4）

Why:
    - Employee.attendances/leaves/salaries 帶 ORM cascade="all, delete-orphan"：
      任何 session.delete(employee)（runtime 僅軟刪除，但 ad-hoc script / 未來程式
      可能誤用）會把考勤/假單/薪資紀錄整批靜默刪光——薪資是法定保存資料。
    - year_end 5 個子表 FK ondelete="CASCADE"：刪 year_end_cycles 一列即抹掉
      該年度全部結算/快照/特獎，無任何守衛。
    改為：ORM 留 save-update（不再 delete 子列）+ passive_deletes="all"（flush 時
    不碰子列），DB FK 改 RESTRICT —— 有子列時刪父列必須被資料庫拒絕。

    SQLite FK enforcement 需 PRAGMA foreign_keys=ON，故用 fk_session
    （照 tests/test_appraisal_scoring_rule_model.py 慣例）。PG 端行為由
    migration cascfx01 後的 dev DB 實測補強。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from models.employee import Employee
from models.salary import SalaryRecord
from models.year_end import OrgYearSettings, YearEndCycle, YearEndSettlement
from models.year_end import EmployeeYearEndSnapshot


@pytest.fixture
def fk_session(test_db_session):
    """test_db_session + 啟用 SQLite FK 強制（讓 RESTRICT 生效）。"""
    engine = test_db_session.get_bind()

    def _pragma_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "connect", _pragma_fk)
    test_db_session.execute(text("PRAGMA foreign_keys=ON"))
    yield test_db_session
    event.remove(engine, "connect", _pragma_fk)


def _make_cycle(s) -> YearEndCycle:
    cycle = YearEndCycle(
        academic_year=114,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=date(2026, 1, 15),
    )
    s.add(cycle)
    s.flush()
    return cycle


def _make_employee(s, eid: str = "CASC01") -> Employee:
    emp = Employee(employee_id=eid, name="守衛測試員", is_active=True)
    s.add(emp)
    s.flush()
    return emp


def test_delete_cycle_with_org_settings_rejected(fk_session):
    """刪有 org_year_settings 子列的 cycle 必須被 DB RESTRICT 拒絕（原 CASCADE 會靜默清掉）。"""
    s = fk_session
    cycle = _make_cycle(s)
    s.add(
        OrgYearSettings(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            enrollment_target=176,
        )
    )
    s.commit()
    s.delete(cycle)
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()
    assert s.query(OrgYearSettings).count() == 1


def test_delete_cycle_with_settlement_rejected(fk_session):
    """刪有 settlement 子列的 cycle 必須被拒——原本 ORM delete-orphan 會先把
    settlements 整批刪掉再刪 cycle，DB RESTRICT 根本攔不到。"""
    s = fk_session
    cycle = _make_cycle(s)
    emp = _make_employee(s)
    snap = EmployeeYearEndSnapshot(year_end_cycle_id=cycle.id, employee_id=emp.id)
    s.add(snap)
    s.flush()
    s.add(
        YearEndSettlement(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            snapshot_id=snap.id,
        )
    )
    s.commit()
    s.delete(cycle)
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()
    assert s.query(YearEndSettlement).count() == 1


def test_delete_employee_with_salary_records_rejected(fk_session):
    """刪有薪資紀錄的員工必須被拒——原本 cascade="all, delete-orphan" 會把
    法定保存的薪資紀錄整批靜默刪光。"""
    s = fk_session
    emp = _make_employee(s, "CASC02")
    s.add(SalaryRecord(employee_id=emp.id, salary_year=2026, salary_month=5))
    s.commit()
    s.delete(emp)
    with pytest.raises(IntegrityError):
        s.flush()
    s.rollback()
    assert s.query(SalaryRecord).count() == 1
