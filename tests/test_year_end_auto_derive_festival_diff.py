"""B3：③ 節慶差額 festival_diff 自動推導測試。

Excel「年終獎金總表」FESTIVAL_DIFF（N.8 ~ N+1.01 共 6 個月，上學期）：
  逐月差額 = 應領_m − 已發_m，6 個月加總（多退少補，**可為負**）。
  - 應領_m = festival_base_for_role(role) × (在園_m / 目標)
  - 已發_m = SalaryRecord(salary_year=d.year, salary_month=d.month).festival_bonus

seed（蔡宜倩，班導，festival 基數 2000，目標 25，在園固定 20 → 應領=1600/月）：
  逐月已發 [8:1500, 9:1400, 10:1700, 11:1300, 12:1500, 1:225]
  逐月差額 [+100, +200, −100, +300, +100, +1375] → 加總 = 1975（含一個負月）。
在園固定 20（全期已入學、無人退學）以避免 count_enrolled_on 受日期分布干擾；
1975 由逐月已發變動 + 0.8 比例純粹產生。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from models.classroom import Classroom, Student
from models.config import BonusConfig
from models.employee import Employee
from models.salary import SalaryRecord
from models.year_end import (
    ClassEnrollmentTarget,
    OrgYearSettings,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
)
from services.year_end.auto_derive import festival_diff as fd


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _special_items(db, cycle, bonus_type):
    return list(
        db.scalars(
            select(SpecialBonusItem).where(
                SpecialBonusItem.year_end_cycle_id == cycle.id,
                SpecialBonusItem.bonus_type == bonus_type,
            )
        )
    )


def _amount_for(items, employee_id):
    for it in items:
        if it.employee_id == employee_id:
            return it.amount
    raise AssertionError(f"no SpecialBonusItem for employee_id={employee_id}")


def _mk_employee(db, code, name, *, position, title):
    emp = Employee(
        employee_id=code,
        name=name,
        id_number=f"A{code[-9:].rjust(9, '0')}",
        position=position,
        title=title,
        hire_date=date(2024, 8, 1),
        is_active=True,
    )
    db.add(emp)
    db.flush()
    return emp


def _mk_student(db, name, *, classroom_id, enrollment_date):
    s = Student(
        student_id=name,
        name=name,
        classroom_id=classroom_id,
        enrollment_date=enrollment_date,
        is_active=True,
    )
    db.add(s)
    db.flush()
    return s


def _mk_salary(db, emp_id, year, month, festival_bonus):
    sr = SalaryRecord(
        employee_id=emp_id,
        salary_year=year,
        salary_month=month,
        festival_bonus=festival_bonus,
    )
    db.add(sr)
    db.flush()
    return sr


# 上學期 6 個月底（AY114 = 西元 2025/8 ~ 2026/1），(salary_year, salary_month)
_MONTHS = [(2025, 8), (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1)]


# --------------------------------------------------------------------------- #
# fixtures                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def seed(test_db_session):
    """seed 蔡宜倩（班導，加總 1975）+ 一位非帶班行政（全校在園/全校目標）。"""
    db = test_db_session
    sy, sem = 114, 1

    cycle = YearEndCycle(
        academic_year=114,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=date(2026, 1, 15),
    )
    db.add(cycle)
    db.flush()

    # 蔡宜倩：班導（grade a → head_teacher_ab → festival 基數 2000）
    emp_tsai = _mk_employee(
        db, "E_TSAI_YC", "蔡宜倩", position="班導", title="幼兒園教師"
    )
    # 一位非帶班行政（admin → admin_festival 基數 2000）
    emp_admin = _mk_employee(
        db, "E_ADMIN_1", "行政小美", position="行政", title="行政人員"
    )

    cls = Classroom(name="百合", school_year=sy, semester=sem)
    db.add(cls)
    db.flush()

    # 蔡宜倩帶班：目標 25
    db.add(
        ClassEnrollmentTarget(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            classroom_id=cls.id,
            head_teacher_employee_id=emp_tsai.id,
            head_count_target=25,
        )
    )
    db.flush()

    # 全校目標（非帶班用）：50
    db.add(
        OrgYearSettings(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            enrollment_target=50,
        )
    )
    db.flush()

    # BonusConfig：head_teacher_ab=2000、admin_festival=2000
    cfg = BonusConfig(
        config_year=114,
        is_active=True,
        head_teacher_ab=2000,
        admin_festival=2000,
    )
    db.add(cfg)
    db.flush()

    # 班級在園固定 20（全期已入學、無退學）→ 應領 = 2000 × 20/25 = 1600/月
    for i in range(20):
        _mk_student(
            db, f"百合生{i}", classroom_id=cls.id, enrollment_date=date(2025, 7, 1)
        )

    # 蔡宜倩逐月已發 festival_bonus → 差額加總 1975
    tsai_paid = [1500, 1400, 1700, 1300, 1500, 225]
    for (yr, mo), paid in zip(_MONTHS, tsai_paid):
        _mk_salary(db, emp_tsai.id, yr, mo, paid)

    db.commit()
    return {
        "db": db,
        "cycle": cycle,
        "cls": cls,
        "emp_tsai": emp_tsai,
        "emp_admin": emp_admin,
    }


# --------------------------------------------------------------------------- #
# tests                                                                        #
# --------------------------------------------------------------------------- #
def test_festival_diff_sum_over_six_months(seed):
    """蔡宜倩：逐月(應領−已發) 8月~1月加總 = 1975（含一個負月 multi-退少補）。"""
    db = seed["db"]
    cycle = seed["cycle"]

    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    assert _amount_for(items, seed["emp_tsai"].id) == Decimal("1975")

    # 逐月明細存於 calc_meta（6 筆）；其一為負（10 月 −100）
    tsai_item = next(it for it in items if it.employee_id == seed["emp_tsai"].id)
    months = tsai_item.calc_meta["months"]
    assert len(months) == 6
    negative_months = [m for m in months if Decimal(str(m["diff"])) < 0]
    assert len(negative_months) == 1
    assert Decimal(str(negative_months[0]["diff"])) == Decimal("-100")


def test_festival_diff_non_class_uses_schoolwide(seed):
    """非帶班行政：在園/目標走全校（OrgYearSettings.enrollment_target）。

    全校在園 = 20（百合班學生全校可見），全校目標 50 → 比例 0.4。
    應領 = admin_festival(2000) × 0.4 = 800/月；已發全 0（未建 SalaryRecord）
    → 差額 = 800 × 6 = 4800。
    """
    db = seed["db"]
    cycle = seed["cycle"]

    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    assert _amount_for(items, seed["emp_admin"].id) == Decimal("4800")

    admin_item = next(it for it in items if it.employee_id == seed["emp_admin"].id)
    assert admin_item.classroom_id is None  # 非帶班，無班級歸屬


def test_festival_diff_skips_manual(seed):
    """已有手動筆（source_ref 非 auto:）→ 不被覆寫。"""
    db = seed["db"]
    cycle = seed["cycle"]

    manual_label = fd.period_label(cycle)
    manual = SpecialBonusItem(
        year_end_cycle_id=cycle.id,
        employee_id=seed["emp_tsai"].id,
        bonus_type=SpecialBonusType.FESTIVAL_DIFF,
        period_label=manual_label,
        amount=Decimal("8888"),
        source_ref=None,  # 手動筆
        calc_meta={},
    )
    db.add(manual)
    db.commit()

    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    tsai_items = [it for it in items if it.employee_id == seed["emp_tsai"].id]
    assert len(tsai_items) == 1
    assert tsai_items[0].amount == Decimal("8888")
    assert tsai_items[0].source_ref is None
    # 行政（無 manual）正常自動寫入
    assert _amount_for(items, seed["emp_admin"].id) == Decimal("4800")


def test_festival_diff_reupsert_is_idempotent(seed):
    """連跑兩次：auto 筆 UPDATE 而非新增重複筆。"""
    db = seed["db"]
    cycle = seed["cycle"]

    fd.derive_festival_diff(db, cycle)
    db.flush()
    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    tsai_items = [it for it in items if it.employee_id == seed["emp_tsai"].id]
    assert len(tsai_items) == 1
    assert tsai_items[0].amount == Decimal("1975")
    assert tsai_items[0].source_ref == "auto:festival_diff"
