"""B3：③ 節慶差額 festival_diff 自動推導測試（重做版）。

舊版前提（已發_m = SalaryRecord.festival_bonus 逐月加總）被證偽：payroll 只在
發放月寫 SalaryRecord.festival_bonus，其餘月為 0。本版改用 payroll 引擎逐月
accrual（calculate_period_accrual_row）作為「已發」，並以「年終目標」重算「應領」。

  差額_m = 應領_m − 已發_m，6 個上學期月加總（可正可負）。
  - 已發_m = SalaryEngine.calculate_period_accrual_row(...).festival_bonus
             （payroll 逐月 accrual：學期反查班級 + payroll config 目標 +
              round_half_up + 副班導/美師 per-class 加權）
  - 應領_m = festival_base_for_role × 年終人數_m / 年終目標（未封頂）

seed 設計（payroll 端走 hardcode 常數 TARGET_ENROLLMENT / OFFICE_FESTIVAL_BONUS_BASE /
SUPERVISOR_FESTIVAL_BONUS / _school_wide_target=160，seed 未覆寫故全用預設 → 可手算
「已發」鎖死總額；班上固定 20 生，全期已入學、無退學 → 各月 count_enrolled_on 恆 20）：

  - 蔡（班導 head_teacher grade A，festival_base=2000）帶「大班」**雙師班**
    （阿芬為 assistant_teacher → has_assistant=True）→ payroll 目標 =
    TARGET_ENROLLMENT[大班][2_teachers]=24。
      已發/月 = round_half_up(2000 × 20/24) = 1667
    年終目標 head_count_target=10（**故意 ≠ payroll 24**，模擬 Excel 目標差 → true-up）
      應領/月 = 2000 × 20/10 = 4000（未封頂，比例 2.0）
    差額/月 = 4000 − 1667 = 2333 → 6 月總 = 13998（全正 true-up）。
  - 副班導阿芬（assistant_teacher grade A，festival_base=1200）副帶同班
      已發/月 = round_half_up(1200 × 20/24) = 1000   ← payroll **當班** 比例（target 24）
    年終目標（同班 ClassEnrollmentTarget）=10 → 應領/月 = 1200 × 20/10 = 2400
    差額/月 = 2400 − 1000 = 1400 → 6 月總 = 8400。
    **per-class 驗證（舊版 P0）**：若誤走全校（目標 40），應領=600 且 paid 也會走全校
    → 8400 ≠ 全校金額，本斷言鎖死 per-class。
  - 行政小美（admin，festival_base=2000，非帶班）→ 應領走全校（目標 40）。
    payroll：「行政」屬辦公室 → OFFICE_FESTIVAL_BONUS_BASE[行政]=2000，全校比例
    20/_school_wide_target(160) → 已發/月 = round_half_up(2000 × 20/160) = 250。
    應領/月 = 2000 × 20/40 = 1000 → 差額/月 = 750 → 6 月總 = 4500（全正）。
  - 主任大華（director，festival_base=3500，主管）→ 應領走全校（目標 40）。
    payroll：「主任」屬主管 → SUPERVISOR_FESTIVAL_BONUS[主任]=3500，全校比例
    20/160 → 已發/月 = round_half_up(3500 × 20/160) = 438。
    應領/月 = 3500 × 20/40 = 1750 → 差額/月 = 1312 → 6 月總 = 7872（驗證主管走全校非班級）。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from models.classroom import ClassGrade, Classroom, Student
from models.config import BonusConfig
from models.employee import Employee
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


def _item_for(items, employee_id):
    for it in items:
        if it.employee_id == employee_id:
            return it
    raise AssertionError(f"no SpecialBonusItem for employee_id={employee_id}")


def _amount_for(items, employee_id):
    return _item_for(items, employee_id).amount


def _mk_employee(db, code, name, *, position, title):
    emp = Employee(
        employee_id=code,
        name=name,
        id_number=f"A{code[-9:].rjust(9, '0')}",
        position=position,
        title=title,
        base_salary=35000,
        hire_date=date(2024, 8, 1),  # 遠早於本期 → 必滿 3 個月 eligibility
        is_active=True,
    )
    db.add(emp)
    db.flush()
    return emp


def _mk_student(db, sid, *, classroom_id, enrollment_date):
    s = Student(
        student_id=sid,
        name=sid,
        classroom_id=classroom_id,
        enrollment_date=enrollment_date,
        is_active=True,
    )
    db.add(s)
    db.flush()
    return s


# 上學期 6 個月底（AY114 = 西元 2025/8 ~ 2026/1）
_MONTHS = [(2025, 8), (2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1)]


# --------------------------------------------------------------------------- #
# fixtures                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def seed(test_db_session):
    """seed 班導(蔡)+副班導(阿芬,同班)+非帶班行政(小美)+主管(主任)。"""
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

    grade = ClassGrade(name="大班")
    db.add(grade)
    db.flush()

    emp_tsai = _mk_employee(
        db, "E_TSAI_YC", "蔡宜倩", position="班導", title="幼兒園教師"
    )
    emp_fen = _mk_employee(
        db, "E_FEN_001", "阿芬", position="副班導", title="幼兒園教師"
    )
    emp_admin = _mk_employee(
        db, "E_ADMIN_1", "行政小美", position="行政", title="行政人員"
    )
    emp_dir = _mk_employee(db, "E_DIR_001", "主任大華", position="主任", title="主任")

    # 大班，蔡為班導、阿芬為副班導 → has_assistant=True（payroll 2_teachers 目標）。
    cls = Classroom(
        name="茉莉",
        grade_id=grade.id,
        head_teacher_id=emp_tsai.id,
        assistant_teacher_id=emp_fen.id,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    db.add(cls)
    db.flush()

    # 年終班級目標：head_count_target=10（故意 ≠ payroll 大班目標，製造 true-up）。
    db.add(
        ClassEnrollmentTarget(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            classroom_id=cls.id,
            head_teacher_employee_id=emp_tsai.id,
            assistant_employee_id=emp_fen.id,
            head_count_target=10,
        )
    )
    db.flush()

    # 全校目標（非帶班/主管用）：40。
    db.add(
        OrgYearSettings(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            enrollment_target=40,
        )
    )
    db.flush()

    # BonusConfig：角色節慶基數（festival_base_for_role 查表）。
    # config_year = 民國曆年（學年114→西元2025 = academic_year+1911）。
    cfg = BonusConfig(
        config_year=2025,
        is_active=True,
        head_teacher_ab=2000,
        assistant_teacher_ab=1200,
        admin_festival=2000,
        director_festival=3500,
    )
    db.add(cfg)
    db.flush()

    # 班上固定 20 生（全期已入學、無退學）→ count_enrolled_on 各月恆 20。
    for i in range(20):
        _mk_student(
            db, f"茉莉生{i}", classroom_id=cls.id, enrollment_date=date(2025, 7, 1)
        )

    db.commit()
    return {
        "db": db,
        "cycle": cycle,
        "cls": cls,
        "emp_tsai": emp_tsai,
        "emp_fen": emp_fen,
        "emp_admin": emp_admin,
        "emp_dir": emp_dir,
    }


# --------------------------------------------------------------------------- #
# tests                                                                        #
# --------------------------------------------------------------------------- #
def test_head_teacher_true_up_exact_total(seed):
    """班導蔡（雙師班，payroll target 24）：已發=round(2000×20/24)=1667/月，
    應領=2000×20/10=4000/月，差額 2333/月 × 6 = 13998。"""
    db, cycle = seed["db"], seed["cycle"]
    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, seed["emp_tsai"].id)
    assert item.amount == Decimal("13998.00")
    assert item.classroom_id == seed["cls"].id  # 帶班 → 班級歸屬

    months = item.calc_meta["months"]
    assert len(months) == 6
    for mrow in months:
        assert Decimal(mrow["due"]) == Decimal("4000.00")
        assert Decimal(mrow["paid"]) == Decimal("1667.00")
        assert mrow["enrolled"] == 20
        assert mrow["target"] == 10


def test_assistant_teacher_per_class_not_schoolwide(seed):
    """副班導阿芬：已發=round(1200×20/24)=1000/月（payroll 用『當班』比例，非全校），
    應領=1200×20/10=2400/月，差額 1400/月 × 6 = 8400。

    這是舊版 P0：舊版讓副班導落入全校 else → 系統性錯。本斷言驗證 per-class。
    """
    db, cycle = seed["db"], seed["cycle"]
    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, seed["emp_fen"].id)
    assert item.amount == Decimal("8400.00")
    assert item.classroom_id == seed["cls"].id  # per-class，非全校 None

    months = item.calc_meta["months"]
    for mrow in months:
        assert Decimal(mrow["due"]) == Decimal("2400.00")
        assert Decimal(mrow["paid"]) == Decimal("1000.00")
        # 若副班導誤用全校（target 40）：應領=1200×20/40=600，paid 也會走全校→錯。
        assert (
            mrow["enrolled"] == 20
        )  # 當班 20，非全校（恰巧亦 20，但 target=10 鎖死 per-class）


def test_non_class_admin_uses_schoolwide(seed):
    """非帶班行政小美：應領走全校（在園 20 / 全校目標 40）= 1000/月，
    已發走 payroll 辦公室全校比例 round(2000×20/160)=250/月 → 差額 750/月 × 6 = 4500。"""
    db, cycle = seed["db"], seed["cycle"]
    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, seed["emp_admin"].id)
    assert item.amount == Decimal("4500.00")
    assert item.classroom_id is None  # 非帶班 → 無班級歸屬

    for mrow in item.calc_meta["months"]:
        assert Decimal(mrow["due"]) == Decimal("1000.00")
        assert Decimal(mrow["paid"]) == Decimal("250.00")
        assert mrow["target"] == 40


def test_supervisor_uses_schoolwide(seed):
    """主管(主任)：應領走全校年終目標(40) = 3500×20/40 = 1750/月，已發走 payroll 主管
    全校比例 round(3500×20/160)=438/月 → 差額 1312/月 × 6 = 7872（走全校非班級）。"""
    db, cycle = seed["db"], seed["cycle"]
    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, seed["emp_dir"].id)
    assert item.classroom_id is None  # 主管走全校，非班級
    assert item.amount == Decimal("7872.00")
    months = item.calc_meta["months"]
    assert len(months) == 6
    for mrow in months:
        assert Decimal(mrow["due"]) == Decimal("1750.00")
        assert Decimal(mrow["paid"]) == Decimal("438.00")
        assert mrow["target"] == 40


def test_paid_mirrors_payroll_count_semantics(seed):
    """**已發鏡像 payroll**：已發走 payroll 的 count_students_active_on，應領走
    count_enrolled_on。自 2026-06-13 L1a 起 payroll filter 也含 withdrawal_date
    （兩 filter 收斂），退學學生同時從「應領」與「已發」消失；true-up 剩餘成分
    為分母差（應領用編制 head_count_target=10，已發用 payroll 目標 24）。

    同時兼作回歸守衛（非循環）：翻轉人數使總額確實改變（13998 → 7002）。

    seed 後讓 10 位學生在 8/1 前退學（withdrawal_date）：
      - 應領：count_enrolled_on=10 → 2000 × 10/10 = 2000/月
      - 已發：count_students_active_on=10（L1a 後同樣排除退學生）→
              round(2000 × 10/24)=833/月
      - 差額/月 = 2000 − 833 = 1167 → 6 月總 = 7002。
      （L1a 前已發人數仍是 20 → 1667/月、總額 1998；該行為已隨 payroll 修正走入歷史）
    """
    db, cycle = seed["db"], seed["cycle"]

    fd.derive_festival_diff(db, cycle)
    db.flush()
    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    before = _amount_for(items, seed["emp_tsai"].id)
    assert before == Decimal("13998.00")

    # 10 位學生在 8/1 之前退學 → 應領與已發（L1a 後）全期都見 10。
    students = list(
        db.scalars(select(Student).where(Student.classroom_id == seed["cls"].id)).all()
    )
    for s in students[:10]:
        s.withdrawal_date = date(2025, 7, 15)
    db.commit()

    fd.derive_festival_diff(db, cycle)
    db.flush()
    items2 = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items2, seed["emp_tsai"].id)
    after = item.amount
    assert after == Decimal("7002.00")
    assert after != before  # 翻轉人數確實改變總額（非循環）

    # per-month 鐵證：兩側人數收斂為 10，差額純粹來自分母（10 編制 vs 24 目標）。
    for mrow in item.calc_meta["months"]:
        assert mrow["enrolled"] == 10  # count_enrolled_on（應領）
        assert Decimal(mrow["due"]) == Decimal("2000.00")
        assert Decimal(mrow["paid"]) == Decimal("833.00")  # round(2000×10/24)


def test_skips_manual_item(seed):
    """已有手動筆（source_ref 非 auto:）→ 不被覆寫。"""
    db, cycle = seed["db"], seed["cycle"]

    manual = SpecialBonusItem(
        year_end_cycle_id=cycle.id,
        employee_id=seed["emp_tsai"].id,
        bonus_type=SpecialBonusType.FESTIVAL_DIFF,
        period_label=fd.period_label(cycle),
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
    # 其他人正常自動寫入
    assert _amount_for(items, seed["emp_admin"].id) == Decimal("4500.00")


def test_reupsert_is_idempotent(seed):
    """連跑兩次：auto 筆 UPDATE 而非新增重複筆。"""
    db, cycle = seed["db"], seed["cycle"]

    fd.derive_festival_diff(db, cycle)
    db.flush()
    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    tsai_items = [it for it in items if it.employee_id == seed["emp_tsai"].id]
    assert len(tsai_items) == 1
    assert tsai_items[0].amount == Decimal("13998.00")
    assert tsai_items[0].source_ref == "auto:festival_diff"


def test_cross_term_classroom_fallback_stays_per_class(seed):
    """legacy-data：班導的 active 班 **未** 標記本 cycle 的 (school_year, semester)。

    payroll `_resolve_classroom_for_employee_in_month` 有 cross-term fallback → 仍解析
    到班 → 已發走 per-class（payroll category '帶班老師'）。本「應領」端的
    `_resolve_classroom_for_emp` 鏡像此 fallback → 同樣 per-class，兩側一致。
    若 fallback 缺失，應領會落全校（target 40）而已發 per-class → 差額變 garbage。

    seed：新班導小薇，帶「中班」單師班但標記 school_year=113（**非** 114）；
    年終 ClassEnrollmentTarget(該班)=10；班上 18 生。
      已發/月 = round(2000×18/12)=3000（中班 1_teacher payroll 目標 12，無副班導）
      應領/月 = 2000×18/10 = 3600（per-class，target 10）
      差額/月 = 600 → 6 月總 = 3600（per-class，非全校 target 40 的 600）。
    """
    db, cycle = seed["db"], seed["cycle"]

    grade_mid = ClassGrade(name="中班")
    db.add(grade_mid)
    db.flush()
    emp_wei = _mk_employee(db, "E_WEI_001", "小薇", position="班導", title="幼兒園教師")
    # **故意 school_year=113**（非本 cycle 的 114）→ term 篩選不中，逼 fallback。
    cls_old = Classroom(
        name="桔梗",
        grade_id=grade_mid.id,
        head_teacher_id=emp_wei.id,
        assistant_teacher_id=0,  # 單師 → payroll 目標 12
        school_year=113,
        semester=1,
        is_active=True,
    )
    db.add(cls_old)
    db.flush()
    db.add(
        ClassEnrollmentTarget(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            classroom_id=cls_old.id,
            head_teacher_employee_id=emp_wei.id,
            head_count_target=10,
        )
    )
    db.flush()
    for i in range(18):
        _mk_student(
            db, f"桔梗生{i}", classroom_id=cls_old.id, enrollment_date=date(2025, 7, 1)
        )
    db.commit()

    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, emp_wei.id)
    assert item.classroom_id == cls_old.id  # fallback 仍解析到班 → per-class
    assert item.amount == Decimal("3600.00")
    for mrow in item.calc_meta["months"]:
        assert mrow["category"] == "帶班老師"
        assert Decimal(mrow["due"]) == Decimal("3600.00")
        assert Decimal(mrow["paid"]) == Decimal("3000.00")
        assert mrow["target"] == 10  # per-class target，非全校 40


def test_supervisor_on_class_roster_gated_to_schoolwide(seed):
    """主管同時掛班導：payroll 優先序 主管 > 帶班 → category '主管' 全校比例。

    應領以 payroll category gate：category '主管' → 全校（非 per-class），兩側一致。
    若以「是否在班級 roster」判定，應領會誤走 per-class → 與已發（主管全校）對不上。

    seed：組長小組（supervisor_role 主任 → leader_festival? 用 director；簡化用主任）
    同時為「小班」班導。payroll：主任 festival 3500，全校比例 20/160 → 已發=438/月。
    應領：category 主管 → 全校 target 40 → 3500×20/40=1750/月 → 差額 1312/月 × 6 = 7872。
    （即使該主任掛班導 roster，應領仍走全校，與已發一致。）
    """
    db, cycle = seed["db"], seed["cycle"]

    grade_small = ClassGrade(name="小班")
    db.add(grade_small)
    db.flush()
    emp_sup = _mk_employee(db, "E_SUP_CLS", "主任兼班導", position="主任", title="主任")
    # 主任同時掛班導 roster（製造 payroll 主管 vs 應領 per-class 的不一致風險）。
    cls_sup = Classroom(
        name="鈴蘭",
        grade_id=grade_small.id,
        head_teacher_id=emp_sup.id,
        assistant_teacher_id=0,
        school_year=114,
        semester=1,
        is_active=True,
    )
    db.add(cls_sup)
    db.flush()
    db.add(
        ClassEnrollmentTarget(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            classroom_id=cls_sup.id,
            head_teacher_employee_id=emp_sup.id,
            head_count_target=10,  # 若誤走 per-class 會用此 → 金額會不同
        )
    )
    db.flush()
    # 鈴蘭班 5 生（與全校 20 不同 → per-class 誤判金額會明顯偏離全校金額）
    for i in range(5):
        _mk_student(
            db, f"鈴蘭生{i}", classroom_id=cls_sup.id, enrollment_date=date(2025, 7, 1)
        )
    db.commit()

    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, emp_sup.id)
    assert item.classroom_id is None  # 主管 → 全校，非 per-class
    for mrow in item.calc_meta["months"]:
        assert mrow["category"] == "主管"
        # 全校在園 = 20(茉莉) + 5(鈴蘭) = 25；全校 target 40 → 應領=3500×25/40=2187.5
        assert Decimal(mrow["due"]) == Decimal("2187.50")
        assert mrow["target"] == 40  # 全校 target，非 per-class 10


def test_empty_position_no_windfall(seed):
    """**position gate 對稱（修 windfall）**：position 為空的 司機/美師/美編 應領全 0。

    payroll「已發」對 `not position`（None 或空字串）的員工 gate festival=0
    （engine.py:2004 `if not position: is_eligible = False`，「無職位資料(不發放)」）。
    但 role_key_of 對 司機/美師/美編 只憑 *title* 關鍵字即回非-None（driver_festival
    default=1000 > 0，不被 festival_base<=0 guard 排除）。若「應領」不套相同 position
    gate → 一名 position 為空的司機會算出 due>0 而 payroll「已發」=0 → 正向 windfall。

    seed 司機老王 position=None、title="司機"（role_key driver，festival_base=1000）；
    非帶班 → 走全校（在園 20 / 全校目標 40）。hire_date 遠早於本期（_mk_employee 預設
    2024-08-01）故 hire 資格恆滿，隔離出 position gate 為唯一變因。
      - 有 gate（正確）：position 空 → 各月應領 due=0、已發 paid=0 → diff=0 → 總額 0。
      - sabotage（移掉 position gate）：應領=1000×20/40=500/月、payroll 仍 0 → diff 500/月
        × 6 = 3000 windfall → 總額 3000 ≠ 0。本斷言（amount==0 + 各月 due/paid/diff 皆 0）
        鎖死 gate；移掉 `not position_eligible` 條件會使 amount==0 斷言 FAIL（變 3000）。

    Option A：仍寫一筆 0 額 item（idempotent cleanup + 反映 payroll 不一致），故 item
    必存在；per-month 斷言 paid==0 同時實證 payroll 確實對 not-position gate festival=0。
    """
    db, cycle = seed["db"], seed["cycle"]

    # position=None（非帶班、無職位）但 title 含「司機」→ role_key driver、base 1000>0。
    emp_drv = _mk_employee(db, "E_DRV_001", "司機老王", position=None, title="校車司機")
    db.commit()

    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, emp_drv.id)  # Option A：0 額 item 仍寫入，必存在
    assert item.amount == Decimal("0")  # 無 windfall（移 gate 會變 3000 → FAIL）
    assert item.classroom_id is None  # 非帶班

    months = item.calc_meta["months"]
    assert len(months) == 6
    for mrow in months:
        assert mrow["eligible"] is False
        assert mrow["eligible_reason"] == "no_position"
        assert Decimal(mrow["due"]) == Decimal("0")
        assert Decimal(mrow["paid"]) == Decimal("0")  # 實證 payroll not-position gate
        assert Decimal(mrow["diff"]) == Decimal("0")  # 無 windfall
        assert mrow["enrolled"] is None  # gate 前 short-circuit，未查 count


def test_new_hire_early_months_gated_no_windfall(seed):
    """**新人 eligibility 對稱（修 P1 windfall）**：未滿 3 個月的月份「應領」=0。

    payroll「已發」對未滿 festival_bonus_months（預設 3）個月的新人 gate festival=0
    （calculate_period_accrual_row）；本「應領」端必須套用**完全相同**的判定（同一
    reference_date = 該月月底），否則早月 已發=0 但 應領>0 → diff 變正向 windfall
    （憑空把資格未到不該領的節慶獎金 true-up 給新人）。

    seed 新人小新 hire_date=2025-08-15 → +3 個月 = 2025-11-15：
      - 8/9/10 月底（08-31 / 09-30 / 10-31）< 11-15 → **未滿資格**：應領=0、已發=0、diff=0。
      - 11/12/1 月底（11-30 / 12-31 / 01-31）≥ 11-15 → 已滿：正常 true-up。
    小新帶單師「中班」class（payroll 1_teacher target 12），18 生（全期入學無退學，
    count_enrolled_on 各月恆 18，**早月在園 > 0** → sabotage 非空轉）；年終目標 head=9。
      已滿月 已發 = round(2000×18/12) = 3000；應領 = 2000×18/9 = 4000；diff = 1000/月。
      總額 = 0×3（早月 gated）+ 1000×3（後段）= 3000。

    **sabotage（移掉 gate → 早月 windfall 使本斷言 FAIL）**：若應領未套 eligibility，
    早月 應領 = 2000×18/9 = 4000（已發仍 0，payroll 有 gate）→ diff 4000/月 × 3 = 12000
    額外 windfall → 總額會變 15000 ≠ 3000。本斷言（== 3000 + 早月 diff==0）鎖死 gate。
    """
    db, cycle = seed["db"], seed["cycle"]

    grade_mid = ClassGrade(name="中班")
    db.add(grade_mid)
    db.flush()
    emp_new = _mk_employee(db, "E_NEW_001", "小新", position="班導", title="幼兒園教師")
    # 年中入職：2025-08-15 → 滿 3 個月 = 2025-11-15（8/9/10 月底未滿，11 月底起滿）。
    emp_new.hire_date = date(2025, 8, 15)
    db.flush()

    cls_new = Classroom(
        name="鳶尾",
        grade_id=grade_mid.id,
        head_teacher_id=emp_new.id,
        assistant_teacher_id=0,  # 單師 → payroll 中班 1_teacher target 12
        school_year=114,
        semester=1,
        is_active=True,
    )
    db.add(cls_new)
    db.flush()
    db.add(
        ClassEnrollmentTarget(
            year_end_cycle_id=cycle.id,
            semester_first=True,
            classroom_id=cls_new.id,
            head_teacher_employee_id=emp_new.id,
            head_count_target=9,
        )
    )
    db.flush()
    # 18 生，全期已入學、無退學 → 各月 count_enrolled_on 恆 18（早月在園 > 0）。
    for i in range(18):
        _mk_student(
            db, f"鳶尾生{i}", classroom_id=cls_new.id, enrollment_date=date(2025, 7, 1)
        )
    db.commit()

    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, emp_new.id)

    # 總額：早月 3 個 gated（diff 0）+ 後段 3 個 true-up（1000/月）= 3000（非 15000）。
    assert item.amount == Decimal("3000.00")

    # 年中入職的班導仍正確歸屬其班級：item_classroom_id 只看 eligible 月份，
    # 不被早月 gated（classroom_id=None placeholder）稀釋 → 仍是 cls_new.id。
    assert item.classroom_id == cls_new.id
    assert item.calc_meta["is_head_teacher"] is True

    months = item.calc_meta["months"]
    assert len(months) == 6
    # 早月（8/9/10）：未滿資格 → 應領 0、已發 0、diff 0、eligible False、enrolled None。
    for mrow in months[:3]:
        assert mrow["eligible"] is False
        assert Decimal(mrow["due"]) == Decimal("0")
        assert Decimal(mrow["paid"]) == Decimal("0")  # payroll 同樣 gate
        assert Decimal(mrow["diff"]) == Decimal("0")  # 無 windfall
        assert mrow["enrolled"] is None  # gate 前 short-circuit，未查 count
    # 後段（11/12/1）：已滿資格 → 正常 per-class true-up。
    for mrow in months[3:]:
        assert mrow["eligible"] is True
        assert Decimal(mrow["due"]) == Decimal("4000.00")  # 2000×18/9
        assert Decimal(mrow["paid"]) == Decimal("3000.00")  # round(2000×18/12)
        assert Decimal(mrow["diff"]) == Decimal("1000.00")
        assert mrow["enrolled"] == 18
        assert mrow["target"] == 9


def test_paid_uses_resolve_bonus_counts_snapshot(seed):
    """P1-9：已發人數必須鏡像 payroll 的 resolve_bonus_counts（含 HR 確認/手調快照），

    而非原始 count_students_active_on。payroll 自 L2 起改走 resolve_bonus_counts
    （有快照讀快照）；festival_diff 原本注入 count_students_active_on → 短路掉
    resolve_bonus_counts → 「已發」≠ payroll 實發 → true-up 失真。

    seed 班上實際 20 生；對涵蓋月份加一張全校快照 school=30（模擬 HR 手調），
    主管「已發」應反映快照 30：round(3500×30/160)=656/月，而非 live 20 的 438。
    """
    from models.enrollment_snapshot import ClassEnrollmentSnapshot

    db, cycle = seed["db"], seed["cycle"]

    # 對整個學年（2025-08 ~ 2026-07）加全校快照 school=30 + 班級快照 20（班級維持不變，
    # 隔離出「全校人數」單一變因 → 只動主管/辦公室的已發）。
    for y, m in [(2025, mm) for mm in range(8, 13)] + [(2026, mm) for mm in range(1, 8)]:
        db.add(
            ClassEnrollmentSnapshot(
                snapshot_year=y,
                snapshot_month=m,
                classroom_id=None,
                student_count=Decimal("30"),
                is_confirmed=True,
            )
        )
        db.add(
            ClassEnrollmentSnapshot(
                snapshot_year=y,
                snapshot_month=m,
                classroom_id=seed["cls"].id,
                student_count=Decimal("20"),
                is_confirmed=True,
            )
        )
    db.commit()

    fd.derive_festival_diff(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.FESTIVAL_DIFF)
    item = _item_for(items, seed["emp_dir"].id)
    assert item is not None, "主管應有 FESTIVAL_DIFF 筆"
    for mrow in item.calc_meta["months"]:
        # 已發鏡像快照 school=30：round(3500×30/160)=656（修補前用 live 20 → 438）
        assert Decimal(mrow["paid"]) == Decimal(
            "656.00"
        ), f"已發應反映快照人數 30，實際 paid={mrow['paid']}"
        # 應領不受快照影響（走 count_enrolled_on=20）：3500×20/40=1750
        assert Decimal(mrow["due"]) == Decimal("1750.00")
