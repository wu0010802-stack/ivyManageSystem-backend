"""P1 回歸：engine 重算路徑改變 YTD 累計獎金 → cascade 標後月 needs_recalc。

二代健保補充保費採 per-payment 增額制（query_ytd_bonus_before 以 salary_month <
month 累計前月已落帳獎金）。manual_adjust 改獎金時已會 cascade（mark_salary_stale_
from_month），但 **engine 重算路徑**（process_salary_calculation /
_compute_and_persist_single_employee）原本零 cascade：上游事件（考勤更正等）令早月
festival/overtime/performance/special/supervisor_dividend 重算成不同值後，同年後月
的 ytd_before 基底失準卻不被標 stale。

與 manual_adjust 的關鍵差異：engine 重算「當月」時已連同當月補充保費一起重算
（_finalize_breakdown 用 ytd_before=sum(1..M-1) + 當月新獎金），當月自身正確；要
傳播的是「之後」月份（from_month = month + 1）。manual_adjust 用 month（不重算當月
補充保費）語意不同。

本測試鎖定 cascade helper 的語意：YTD 獎金有變才標、只標後月、month=12 邊界安全。
"""

from __future__ import annotations

from datetime import date

from models.database import Employee, SalaryRecord
from services.salary.utils import (
    snapshot_ytd_bonus,
    mark_stale_if_ytd_bonus_changed,
)


def _emp(session) -> int:
    emp = Employee(
        employee_id="A001",
        name="員工A",
        base_salary=30000,
        employee_type="regular",
        is_active=True,
        hire_date=date(2025, 1, 1),
    )
    session.add(emp)
    session.commit()
    return emp.id


def _rec(session, emp_id, month, *, needs_recalc=False, is_finalized=False, **fields):
    rec = SalaryRecord(
        employee_id=emp_id,
        salary_year=2026,
        salary_month=month,
        base_salary=30000,
        gross_salary=30000,
        net_salary=30000,
        total_deduction=0,
        needs_recalc=needs_recalc,
        is_finalized=is_finalized,
        **fields,
    )
    session.add(rec)
    session.commit()
    return rec


def _get(session, emp_id, month):
    session.expire_all()
    return (
        session.query(SalaryRecord)
        .filter_by(employee_id=emp_id, salary_year=2026, salary_month=month)
        .first()
    )


def test_marks_subsequent_month_when_bonus_changed(test_db_session):
    s = test_db_session
    emp = _emp(s)
    m6 = _rec(s, emp, 6, festival_bonus=1000)
    _rec(s, emp, 7, needs_recalc=False)

    before = snapshot_ytd_bonus(m6)
    m6.festival_bonus = 2000  # 模擬 _fill_salary_record 重算寫入不同值
    mark_stale_if_ytd_bonus_changed(s, emp, 2026, 6, before, m6)
    s.commit()

    assert _get(s, emp, 7).needs_recalc is True, "後月 ytd_before 失準應標 stale"


def test_does_not_mark_current_month(test_db_session):
    """關鍵差異：engine 重算當月已含補充保費重算 → 當月自身不標（from_month=month+1）。"""
    s = test_db_session
    emp = _emp(s)
    m6 = _rec(s, emp, 6, festival_bonus=1000, needs_recalc=False)

    before = snapshot_ytd_bonus(m6)
    m6.festival_bonus = 2000
    mark_stale_if_ytd_bonus_changed(s, emp, 2026, 6, before, m6)
    s.commit()

    assert _get(s, emp, 6).needs_recalc is False, "當月自身正確、不應被標 stale"


def test_no_mark_when_bonus_unchanged(test_db_session):
    s = test_db_session
    emp = _emp(s)
    m6 = _rec(s, emp, 6, festival_bonus=1000)
    _rec(s, emp, 7, needs_recalc=False)

    before = snapshot_ytd_bonus(m6)
    # 不改任何 YTD 獎金欄位（重算結果與既有相同）
    marked = mark_stale_if_ytd_bonus_changed(s, emp, 2026, 6, before, m6)
    s.commit()

    assert marked == 0
    assert _get(s, emp, 7).needs_recalc is False, "無變動不應產生 spurious cascade"


def test_non_ytd_field_change_does_not_cascade(test_db_session):
    """只有 BONUS_FIELDS_FOR_YTD 變動才 cascade；改別的欄位（如 late_deduction）不算。"""
    s = test_db_session
    emp = _emp(s)
    m6 = _rec(s, emp, 6, festival_bonus=1000, late_deduction=0)
    _rec(s, emp, 7, needs_recalc=False)

    before = snapshot_ytd_bonus(m6)
    m6.late_deduction = 500  # 非 YTD 累計欄位
    marked = mark_stale_if_ytd_bonus_changed(s, emp, 2026, 6, before, m6)
    s.commit()

    assert marked == 0
    assert _get(s, emp, 7).needs_recalc is False


def test_month_12_boundary_no_crash(test_db_session):
    """month=12 → from_month=13，mark 為乾淨 no-op，不崩。"""
    s = test_db_session
    emp = _emp(s)
    m12 = _rec(s, emp, 12, festival_bonus=1000)

    before = snapshot_ytd_bonus(m12)
    m12.festival_bonus = 5000
    marked = mark_stale_if_ytd_bonus_changed(s, emp, 2026, 12, before, m12)
    s.commit()

    assert marked == 0  # 無 month >= 13 的 record


def test_new_record_zero_to_bonus_cascades(test_db_session):
    """新建 record（before 全 0）算出獎金 → 仍 cascade（不 special-case 新 record）。

    若後月已亂序落帳，它們的 ytd_before 未含本月、確實 stale，應被標。
    """
    s = test_db_session
    emp = _emp(s)
    # 模擬「先算了 7 月、現在才新建/重算 6 月」的亂序場景
    _rec(s, emp, 7, needs_recalc=False)
    m6 = _rec(s, emp, 6, festival_bonus=0)

    before = snapshot_ytd_bonus(m6)  # festival=0
    m6.festival_bonus = 8000
    mark_stale_if_ytd_bonus_changed(s, emp, 2026, 6, before, m6)
    s.commit()

    assert _get(s, emp, 7).needs_recalc is True


def test_finalized_later_month_not_marked(test_db_session):
    """繼承 mark_salary_stale_from_month 語意：後月已封存則不標（封存月凍結）。"""
    s = test_db_session
    emp = _emp(s)
    m6 = _rec(s, emp, 6, festival_bonus=1000)
    _rec(s, emp, 7, needs_recalc=False, is_finalized=True)

    before = snapshot_ytd_bonus(m6)
    m6.festival_bonus = 2000
    mark_stale_if_ytd_bonus_changed(s, emp, 2026, 6, before, m6)
    s.commit()

    assert _get(s, emp, 7).needs_recalc is False, "封存後月不可被標 stale"
