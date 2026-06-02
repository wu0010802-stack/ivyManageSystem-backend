"""B2：① 才藝鼓勵 after_class_award 自動推導測試（Excel 對帳）。

Excel「年終獎金總表」AFTER_CLASS_AWARD：每班 L = J(報名人次) × K(班別單價)。
  - 天堂鳥（班導 林佳穎）：J=25, K=75 → 1875
  - 牡丹（班導 陳品棻）  ：J=13, K=85 → 1105
人次 = COUNT(RegistrationCourse)（非 distinct，一生報兩堂算 2），
       status IN ('enrolled','promoted_pending')，上學期（semester=1）。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
from models.classroom import Classroom
from models.config import BonusConfig
from models.employee import Employee
from models.year_end import (
    ClassEnrollmentTarget,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
)
from services.year_end.auto_derive import after_class_award as aca


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


def _mk_employee(db, code, name):
    emp = Employee(
        employee_id=code,
        name=name,
        id_number=f"A{code[-9:].rjust(9, '0')}",
        hire_date=date(2024, 8, 1),
        is_active=True,
    )
    db.add(emp)
    db.flush()
    return emp


def _mk_course(db, name, school_year, semester):
    c = ActivityCourse(
        name=name, price=1000, school_year=school_year, semester=semester
    )
    db.add(c)
    db.flush()
    return c


def _mk_registration(
    db,
    *,
    classroom_id,
    school_year,
    semester,
    match_status,
    student_name,
    class_name=None,
):
    reg = ActivityRegistration(
        student_name=student_name,
        class_name=class_name,
        classroom_id=classroom_id,
        match_status=match_status,
        school_year=school_year,
        semester=semester,
        is_active=True,
    )
    db.add(reg)
    db.flush()
    return reg


def _enroll(db, reg, course, status="enrolled"):
    rc = RegistrationCourse(registration_id=reg.id, course_id=course.id, status=status)
    db.add(rc)
    db.flush()
    return rc


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def seed(test_db_session):
    """seed 出能產生天堂鳥 1875 / 牡丹 1105 的資料。"""
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

    # 班導
    emp_lin = _mk_employee(db, "E_LIN_JY", "林佳穎")  # 天堂鳥
    emp_chen = _mk_employee(db, "E_CHEN_PF", "陳品棻")  # 牡丹

    # 班級
    cls_bird = Classroom(name="天堂鳥", school_year=sy, semester=sem)
    cls_peony = Classroom(name="牡丹", school_year=sy, semester=sem)
    db.add_all([cls_bird, cls_peony])
    db.flush()

    # ClassEnrollmentTarget（semester_first=True 上學期）— 班導來源
    db.add_all(
        [
            ClassEnrollmentTarget(
                year_end_cycle_id=cycle.id,
                semester_first=True,
                classroom_id=cls_bird.id,
                head_teacher_employee_id=emp_lin.id,
                head_count_target=30,
            ),
            ClassEnrollmentTarget(
                year_end_cycle_id=cycle.id,
                semester_first=True,
                classroom_id=cls_peony.id,
                head_teacher_employee_id=emp_chen.id,
                head_count_target=30,
            ),
        ]
    )
    db.flush()

    # BonusConfig（最新 active）：班名 → K 單價
    cfg = BonusConfig(
        config_year=114,
        is_active=True,
        after_class_award_unit_price={"天堂鳥": 75, "牡丹": 85},
        art_teacher_unit_price=None,  # 無才藝老師單價 → 跳過 art segment
    )
    db.add(cfg)
    db.flush()

    course = _mk_course(db, "美術", sy, sem)

    # 天堂鳥：人次 25（enrolled 23 + promoted_pending 2）
    for i in range(23):
        reg = _mk_registration(
            db,
            classroom_id=cls_bird.id,
            school_year=sy,
            semester=sem,
            match_status="matched",
            student_name=f"bird_e_{i}",
        )
        _enroll(db, reg, course, status="enrolled")
    for i in range(2):
        reg = _mk_registration(
            db,
            classroom_id=cls_bird.id,
            school_year=sy,
            semester=sem,
            match_status="matched",
            student_name=f"bird_p_{i}",
        )
        _enroll(db, reg, course, status="promoted_pending")

    # 牡丹：人次 13（全 enrolled）
    for i in range(13):
        reg = _mk_registration(
            db,
            classroom_id=cls_peony.id,
            school_year=sy,
            semester=sem,
            match_status="matched",
            student_name=f"peony_e_{i}",
        )
        _enroll(db, reg, course, status="enrolled")

    # 噪音：waitlist 不計 / 下學期不計
    reg_wait = _mk_registration(
        db,
        classroom_id=cls_bird.id,
        school_year=sy,
        semester=sem,
        match_status="matched",
        student_name="bird_wait",
    )
    _enroll(db, reg_wait, course, status="waitlist")
    course_s2 = _mk_course(db, "美術", sy, 2)
    reg_s2 = _mk_registration(
        db,
        classroom_id=cls_bird.id,
        school_year=sy,
        semester=2,
        match_status="matched",
        student_name="bird_s2",
    )
    _enroll(db, reg_s2, course_s2, status="enrolled")

    db.commit()
    return {
        "db": db,
        "cycle": cycle,
        "course": course,
        "cls_bird": cls_bird,
        "cls_peony": cls_peony,
        "emp_lin": emp_lin,
        "emp_chen": emp_chen,
        "sy": sy,
        "sem": sem,
    }


# --------------------------------------------------------------------------- #
# tests                                                                        #
# --------------------------------------------------------------------------- #
def test_after_class_award_per_class(seed):
    db = seed["db"]
    cycle = seed["cycle"]

    aca.derive_after_class_award(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.AFTER_CLASS_AWARD)
    assert _amount_for(items, seed["emp_lin"].id) == Decimal("1875")
    assert _amount_for(items, seed["emp_chen"].id) == Decimal("1105")


def test_after_class_award_reports_unmatched(seed):
    db = seed["db"]
    cycle = seed["cycle"]
    course = seed["course"]
    sy, sem = seed["sy"], seed["sem"]

    # 兩筆未配對報名課程（classroom_id IS NULL）→ 不計獎金、計入 unmatched_count
    reg_a = _mk_registration(
        db,
        classroom_id=None,
        school_year=sy,
        semester=sem,
        match_status="pending",
        student_name="orphan_a",
        class_name="天堂鳥",
    )
    _enroll(db, reg_a, course, status="enrolled")
    reg_b = _mk_registration(
        db,
        classroom_id=None,
        school_year=sy,
        semester=sem,
        match_status="unmatched",
        student_name="orphan_b",
        class_name="牡丹",
    )
    _enroll(db, reg_b, course, status="promoted_pending")
    db.commit()

    report = aca.derive_after_class_award(db, cycle)
    db.flush()

    assert report.unmatched_count == 2
    # 未配對不影響已配對班別金額
    items = _special_items(db, cycle, SpecialBonusType.AFTER_CLASS_AWARD)
    assert _amount_for(items, seed["emp_lin"].id) == Decimal("1875")


def test_after_class_award_clean_partition_manual_vs_pending(seed):
    """J / unmatched 乾淨互斥（成功集 = {'matched','manual'}）。

    - manual + classroom_id=bird → 計入該班 J（bird 25 → 26），不進 unmatched。
    - pending + classroom_id=bird → 進 unmatched，不進 J（bird 仍 26）。
    pending 帶 classroom_id 才能驗證 _count_enrollments 的 match_status 過濾
    （NULL-classroom 的 pending 無法觸發此分支）。
    """
    db = seed["db"]
    cycle = seed["cycle"]
    course = seed["course"]
    sy, sem = seed["sy"], seed["sem"]

    # manual 人工綁定（合法在班）→ 計入 bird J
    reg_manual = _mk_registration(
        db,
        classroom_id=seed["cls_bird"].id,
        school_year=sy,
        semester=sem,
        match_status="manual",
        student_name="bird_manual",
    )
    _enroll(db, reg_manual, course, status="enrolled")
    # pending 帶 bird classroom_id → 只進 unmatched，不得灌進 bird J
    reg_pending = _mk_registration(
        db,
        classroom_id=seed["cls_bird"].id,
        school_year=sy,
        semester=sem,
        match_status="pending",
        student_name="bird_pending",
    )
    _enroll(db, reg_pending, course, status="enrolled")
    db.commit()

    report = aca.derive_after_class_award(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.AFTER_CLASS_AWARD)
    # bird J = 25 + 1(manual) = 26 → 26 × 75 = 1950（pending 不計）
    assert _amount_for(items, seed["emp_lin"].id) == Decimal("1950")
    # 牡丹未受影響
    assert _amount_for(items, seed["emp_chen"].id) == Decimal("1105")
    # pending 進 unmatched，manual 不進 unmatched
    assert report.unmatched_count == 1


def test_after_class_award_skips_manual(seed):
    """已有一筆 manual 的 AFTER_CLASS_AWARD（source_ref 非 auto:）→ 不被覆寫。"""
    db = seed["db"]
    cycle = seed["cycle"]

    # 先寫一筆 manual：period_label 必須與 derive 計算的鍵碰撞才能驗證 skip
    manual_label = aca.period_label_for_class(cycle, seed["cls_bird"].id)
    manual = SpecialBonusItem(
        year_end_cycle_id=cycle.id,
        employee_id=seed["emp_lin"].id,
        bonus_type=SpecialBonusType.AFTER_CLASS_AWARD,
        period_label=manual_label,
        amount=Decimal("9999"),
        classroom_id=seed["cls_bird"].id,
        source_ref=None,  # 手動筆（None）
        calc_meta={},
    )
    db.add(manual)
    db.commit()

    aca.derive_after_class_award(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.AFTER_CLASS_AWARD)
    # 天堂鳥這筆 manual 不被覆寫，仍為 9999（且只有一筆）
    bird_items = [it for it in items if it.employee_id == seed["emp_lin"].id]
    assert len(bird_items) == 1
    assert bird_items[0].amount == Decimal("9999")
    assert bird_items[0].source_ref is None
    # 牡丹（無 manual）正常自動寫入
    assert _amount_for(items, seed["emp_chen"].id) == Decimal("1105")


def test_after_class_award_ignores_soft_deleted(seed):
    """軟刪除報名(is_active=False)即使課程仍 enrolled 也不計入 J / unmatched。"""
    db = seed["db"]
    cycle = seed["cycle"]
    course = seed["course"]
    sy, sem = seed["sy"], seed["sem"]

    # 天堂鳥一筆已軟刪除報名（course 仍 enrolled）→ 不得讓 1875 變動
    reg_del = _mk_registration(
        db,
        classroom_id=seed["cls_bird"].id,
        school_year=sy,
        semester=sem,
        match_status="matched",
        student_name="bird_deleted",
    )
    reg_del.is_active = False
    _enroll(db, reg_del, course, status="enrolled")
    # 一筆軟刪除的未配對報名 → 不得讓 unmatched_count 變動
    reg_del_unmatched = _mk_registration(
        db,
        classroom_id=None,
        school_year=sy,
        semester=sem,
        match_status="pending",
        student_name="orphan_deleted",
    )
    reg_del_unmatched.is_active = False
    _enroll(db, reg_del_unmatched, course, status="enrolled")
    db.commit()

    report = aca.derive_after_class_award(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.AFTER_CLASS_AWARD)
    assert _amount_for(items, seed["emp_lin"].id) == Decimal("1875")
    assert report.unmatched_count == 0


def test_after_class_award_reupsert_is_idempotent(seed):
    """連跑兩次：auto 筆 UPDATE 而非新增重複筆。"""
    db = seed["db"]
    cycle = seed["cycle"]

    aca.derive_after_class_award(db, cycle)
    db.flush()
    aca.derive_after_class_award(db, cycle)
    db.flush()

    items = _special_items(db, cycle, SpecialBonusType.AFTER_CLASS_AWARD)
    bird_items = [it for it in items if it.employee_id == seed["emp_lin"].id]
    assert len(bird_items) == 1
    assert bird_items[0].amount == Decimal("1875")
    assert bird_items[0].source_ref == "auto:after_class_award"
