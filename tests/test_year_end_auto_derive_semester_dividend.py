"""tests/test_year_end_auto_derive_semester_dividend.py — B4 ④ 學期紅利自動推導（TDD）

Excel「113上.113下學期紅利獎金」逐學期、每班導：
  - 舊生率 ≥ 門檻（dividend_returning_threshold，預設 0.9）→ +500（dividend_returning_amount）
  - 才藝班參加率 ≥ 門檻（dividend_activity_threshold，預設 0.8）→ +1000（dividend_activity_amount）
  - 小計 = 兩者加總；蔡宜倩上學期 = 500 + 1000 = 1500。
  - office/非帶班 = 0（無 ClassEnrollmentTarget，根本不進迴圈）。

才藝率（B4）= **distinct 學生參加率**（多少比例班學生有參加才藝），與 B2 的「人次」不同：
  分子 = COUNT(DISTINCT ActivityRegistration.student_id)（該班/學年/學期/is_active/student_id 非 NULL）
  分母 = 該班 active 學生數（Student.classroom_id==cid AND lifecycle_status==active）
  —— 對齊 services/appraisal/status_aggregator._aggregate_activity_rate 的 query 語意，
     但回 fraction（0.xxx）而非百分比（0-100），以對齊門檻單位。

舊生率 = 直接讀 ClassEnrollmentTarget.returning_student_rate（B6 已寫的小數，如 0.917）。

門檻比較：Decimal(str(cfg.field)) 避免 float 邊界；≥ 門檻（含等於）→ 0.900 達標、0.899 不達標。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 在 create_all 之前 import 所有用到的 model module，確保 metadata 註冊完整表
from models.activity import (  # noqa: E402
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
from models.base import Base  # noqa: E402
from models.classroom import (  # noqa: E402
    LIFECYCLE_ACTIVE,
    LIFECYCLE_WITHDRAWN,
    Classroom,
    Student,
)
from models.config import BonusConfig  # noqa: E402
from models.employee import Employee  # noqa: E402
from models.year_end import (  # noqa: E402
    ClassEnrollmentTarget,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
)

_ACADEMIC_YEAR = 114
_BASIS = date(2026, 1, 15)


# ============ Fixtures ============


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def cycle(session):
    c = YearEndCycle(
        academic_year=_ACADEMIC_YEAR,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=_BASIS,
    )
    session.add(c)
    session.flush()
    return c


# ============ helpers ============


def _mk_employee(session, code: str, name: str) -> Employee:
    emp = Employee(
        employee_id=code,
        name=name,
        id_number=f"A{code[-9:].rjust(9, '0')}",
        hire_date=date(2024, 8, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _mk_classroom(session, name: str) -> Classroom:
    c = Classroom(name=name, school_year=_ACADEMIC_YEAR, semester=1)
    session.add(c)
    session.flush()
    return c


def _mk_target(
    session,
    cycle,
    classroom_id: int,
    *,
    head_teacher_employee_id: int | None,
    returning_student_rate: Decimal,
    semester_first: bool = True,
    head_count_target: int = 30,
) -> ClassEnrollmentTarget:
    t = ClassEnrollmentTarget(
        year_end_cycle_id=cycle.id,
        semester_first=semester_first,
        classroom_id=classroom_id,
        head_teacher_employee_id=head_teacher_employee_id,
        head_count_target=head_count_target,
        returning_student_rate=returning_student_rate,
    )
    session.add(t)
    session.flush()
    return t


def _mk_config(
    session,
    *,
    is_active: bool = True,
    returning_threshold: float = 0.9,
    returning_amount: float = 500,
    activity_threshold: float = 0.8,
    activity_amount: float = 1000,
) -> BonusConfig:
    cfg = BonusConfig(
        config_year=_ACADEMIC_YEAR,
        is_active=is_active,
        dividend_returning_threshold=returning_threshold,
        dividend_returning_amount=returning_amount,
        dividend_activity_threshold=activity_threshold,
        dividend_activity_amount=activity_amount,
    )
    session.add(cfg)
    session.flush()
    return cfg


def _mk_student(session, *, classroom_id: int) -> Student:
    s = Student(
        student_id=f"S{session.query(Student).count() + 1:04d}",
        name="測試生",
        classroom_id=classroom_id,
        enrollment_school_year=113,
        enrollment_date=date(2025, 9, 1),
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add(s)
    session.flush()
    return s


def _mk_course(session, name: str, semester: int = 1) -> ActivityCourse:
    c = ActivityCourse(
        name=name, price=1000, school_year=_ACADEMIC_YEAR, semester=semester
    )
    session.add(c)
    session.flush()
    return c


def _mk_registration(
    session,
    *,
    classroom_id: int,
    student_id: int | None,
    student_name: str,
    semester: int = 1,
    is_active: bool = True,
    match_status: str = "matched",
) -> ActivityRegistration:
    reg = ActivityRegistration(
        student_name=student_name,
        classroom_id=classroom_id,
        student_id=student_id,
        match_status=match_status,
        school_year=_ACADEMIC_YEAR,
        semester=semester,
        is_active=is_active,
    )
    session.add(reg)
    session.flush()
    return reg


def _enroll(session, reg, course, status: str = "enrolled") -> RegistrationCourse:
    rc = RegistrationCourse(registration_id=reg.id, course_id=course.id, status=status)
    session.add(rc)
    session.flush()
    return rc


def _seed_activity_for_class(session, classroom_id: int, *, n_students: int):
    """在指定班建 n_students 個 active 學生，每人各一筆 才藝報名（distinct 學生）。"""
    students = [
        _mk_student(session, classroom_id=classroom_id) for _ in range(n_students)
    ]
    return students


def _register_students_for_activity(session, students, course, *, semester: int = 1):
    """為 students 各建一筆才藝報名（同一才藝課）→ distinct 學生 = len(students)。"""
    for st in students:
        reg = _mk_registration(
            session,
            classroom_id=st.classroom_id,
            student_id=st.id,
            student_name=st.name,
            semester=semester,
        )
        _enroll(session, reg, course)


def _items(session, cycle, bonus_type):
    return list(
        session.scalars(
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


# ============ Test: 主測（兩項皆達標 = 1500） ============


class TestBothQualify:
    def test_both_thresholds_met_returns_1500(self, session, cycle):
        """蔡宜倩上學期：舊生率 0.95≥0.9 + 才藝率 ≥0.8 → 500+1000=1500（FIRST）。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_TSAI", "蔡宜倩")
        cls = _mk_classroom(session, "天堂鳥")
        # 才藝率：10 active 學生中 9 報名 → 0.9 ≥ 0.8
        students = _seed_activity_for_class(session, cls.id, n_students=10)
        course = _mk_course(session, "美術")
        _register_students_for_activity(session, students[:9], course)
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        report = derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        assert _amount_for(items, emp.id) == Decimal("1500")
        assert report.written == 1


# ============ Test: 只達一項 ============


class TestOneThresholdOnly:
    def test_returning_only_returns_500(self, session, cycle):
        """舊生率達標 0.95≥0.9、才藝率不達標（0.5<0.8）→ 只 500。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_RET", "舊生達標師")
        cls = _mk_classroom(session, "茉莉")
        students = _seed_activity_for_class(session, cls.id, n_students=10)
        course = _mk_course(session, "美術")
        _register_students_for_activity(session, students[:5], course)  # 0.5 < 0.8
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        assert _amount_for(items, emp.id) == Decimal("500")

    def test_activity_only_returns_1000(self, session, cycle):
        """才藝率達標（0.9≥0.8）、舊生率不達標（0.5<0.9）→ 只 1000。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_ACT", "才藝達標師")
        cls = _mk_classroom(session, "牡丹")
        students = _seed_activity_for_class(session, cls.id, n_students=10)
        course = _mk_course(session, "美術")
        _register_students_for_activity(session, students[:9], course)  # 0.9 ≥ 0.8
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.500"),  # < 0.9
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        assert _amount_for(items, emp.id) == Decimal("1000")


# ============ Test: 兩學期分別寫 FIRST/SECOND ============


class TestBothSemesters:
    def test_writes_first_and_second(self, session, cycle):
        """同班導 semester_first True/False 兩列 → 各寫 FIRST / SECOND。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_BOTH", "雙學期師")
        cls = _mk_classroom(session, "薔薇")
        # 才藝率：上學期 9/10 達標；下學期才藝率 0（無下學期報名）→ 只舊生 500
        students = _seed_activity_for_class(session, cls.id, n_students=10)
        course = _mk_course(session, "美術", semester=1)
        _register_students_for_activity(session, students[:9], course, semester=1)
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),
            semester_first=True,
        )
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),
            semester_first=False,
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        report = derive_semester_dividend(session, cycle)
        session.commit()

        first = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        second = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_SECOND)
        # 上學期：舊生 500 + 才藝 1000 = 1500
        assert _amount_for(first, emp.id) == Decimal("1500")
        # 下學期：舊生 500（才藝率 0 不達標）
        assert _amount_for(second, emp.id) == Decimal("500")
        assert report.written == 2


# ============ Test: 門檻邊界（0.9 vs 0.899） ============


class TestThresholdBoundary:
    def test_returning_rate_exactly_at_threshold_qualifies(self, session, cycle):
        """舊生率 == 門檻 0.900 → 達標（≥，含等於）。才藝率設 0 隔離。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_EQ", "邊界等於師")
        cls = _mk_classroom(session, "百合")
        _seed_activity_for_class(session, cls.id, n_students=10)  # 才藝率 0
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.900"),  # == 門檻
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        assert _amount_for(items, emp.id) == Decimal("500")  # 含等於 → 達標

    def test_returning_rate_just_below_threshold_fails(self, session, cycle):
        """舊生率 0.899 < 門檻 0.900 → 不達標 → 0（才藝率 0 隔離）。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_LT", "邊界差一師")
        cls = _mk_classroom(session, "向日葵")
        _seed_activity_for_class(session, cls.id, n_students=10)  # 才藝率 0
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.899"),  # < 門檻
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        # 兩項皆不達標 → 0 元筆（always-write，比照 B2）
        assert _amount_for(items, emp.id) == Decimal("0.00")


# ============ Test: 才藝率用 distinct（非人次） ============


class TestActivityRateDistinct:
    def test_activity_rate_counts_distinct_students_not_courses(self, session, cycle):
        """一生報兩堂才藝 → 才藝率分子算 1 生（distinct），不是 2（人次）。

        班 4 active 學生；只有 1 名學生報名（但報兩堂課）。
        distinct 才藝率 = 1/4 = 0.25 < 0.8 → 不達標（才藝 0）；舊生率設 0 隔離 → 整筆 0。
        若誤用人次（2 筆 RegistrationCourse / 4 = 0.5），仍 < 0.8 故金額相同；
        但本測進一步斷言 calc_meta.才藝率 == 0.25（人次會是 0.5）以精準命中。
        """
        _mk_config(session)
        emp = _mk_employee(session, "E_DISTINCT", "重複報名師")
        cls = _mk_classroom(session, "櫻花")
        students = _seed_activity_for_class(session, cls.id, n_students=4)
        # 一名學生報兩堂課（兩筆 RegistrationCourse，同一筆 registration → distinct=1）
        st = students[0]
        reg = _mk_registration(
            session,
            classroom_id=st.classroom_id,
            student_id=st.id,
            student_name=st.name,
        )
        c1 = _mk_course(session, "美術")
        c2 = _mk_course(session, "音樂")
        _enroll(session, reg, c1)
        _enroll(session, reg, c2)
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.000"),  # 舊生率隔離
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        item = next(it for it in items if it.employee_id == emp.id)
        # distinct 才藝率 = 1/4 = 0.25（非人次 2/4=0.5）
        assert Decimal(str(item.calc_meta["activity_rate"])) == Decimal("0.25"), (
            f"才藝率必須用 distinct 學生（1/4=0.25），got "
            f"{item.calc_meta['activity_rate']}（若 0.5 = 誤用人次 2/4）"
        )
        assert item.amount == Decimal("0.00")  # 0.25<0.8 且舊生 0 → 0

    def test_two_distinct_registrations_same_student_still_one(self, session, cycle):
        """同一學生兩筆獨立 registration（極端資料）也只算 1 distinct student。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_DUP2", "雙報名師")
        cls = _mk_classroom(session, "芙蓉")
        students = _seed_activity_for_class(session, cls.id, n_students=4)
        st = students[0]
        course = _mk_course(session, "美術")
        # 兩筆 registration（同 student_id）
        reg1 = _mk_registration(
            session,
            classroom_id=st.classroom_id,
            student_id=st.id,
            student_name=st.name,
        )
        _enroll(session, reg1, course)
        reg2 = _mk_registration(
            session,
            classroom_id=st.classroom_id,
            student_id=st.id,
            student_name=st.name,
        )
        _enroll(session, reg2, course)
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.000"),
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        item = next(it for it in items if it.employee_id == emp.id)
        # distinct = 1（兩筆 registration 同 student）→ 1/4 = 0.25
        assert Decimal(str(item.calc_meta["activity_rate"])) == Decimal("0.25")


# ============ Test: manual skip（source_ref 非 auto: 不覆寫） ============


class TestManualSkip:
    def test_manual_item_not_overwritten(self, session, cycle):
        """既有 manual SEMESTER_DIVIDEND_FIRST（source_ref=None）→ 不被覆寫。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_MANUAL", "手動筆師")
        cls = _mk_classroom(session, "滿天星")
        students = _seed_activity_for_class(session, cls.id, n_students=10)
        course = _mk_course(session, "美術")
        _register_students_for_activity(session, students[:9], course)
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),
        )

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
            period_label_for_class,
        )

        manual_label = period_label_for_class(cycle, cls.id, semester_first=True)
        manual = SpecialBonusItem(
            year_end_cycle_id=cycle.id,
            employee_id=emp.id,
            bonus_type=SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
            period_label=manual_label,
            amount=Decimal("8888"),
            classroom_id=cls.id,
            source_ref=None,  # 手動筆
            calc_meta={},
        )
        session.add(manual)
        session.commit()

        report = derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        emp_items = [it for it in items if it.employee_id == emp.id]
        assert len(emp_items) == 1
        assert emp_items[0].amount == Decimal("8888")  # 不被覆寫
        assert emp_items[0].source_ref is None
        assert report.skipped_manual == 1


# ============ Test: 無班導 skip ============


class TestNoHeadTeacher:
    def test_no_head_teacher_skipped(self, session, cycle):
        """ClassEnrollmentTarget 無班導 → 跳過、不寫、記 warning。"""
        _mk_config(session)
        cls = _mk_classroom(session, "無導班")
        _seed_activity_for_class(session, cls.id, n_students=5)
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=None,  # 無班導
            returning_student_rate=Decimal("0.950"),
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        report = derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        assert items == []
        assert report.written == 0
        assert len(report.warnings) >= 1


# ============ Test: idempotent re-run ============


class TestIdempotent:
    def test_rerun_updates_not_duplicates(self, session, cycle):
        """連跑兩次：auto 筆 UPDATE 而非新增重複筆。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_IDEM", "冪等師")
        cls = _mk_classroom(session, "冪等班")
        students = _seed_activity_for_class(session, cls.id, n_students=10)
        course = _mk_course(session, "美術")
        _register_students_for_activity(session, students[:9], course)
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        derive_semester_dividend(session, cycle)
        session.commit()
        derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        emp_items = [it for it in items if it.employee_id == emp.id]
        assert len(emp_items) == 1
        assert emp_items[0].amount == Decimal("1500")
        assert emp_items[0].source_ref == "auto:semester_dividend"


# ============ Test: sabotage（門檻方向反了） ============


class TestSabotageThresholdDirection:
    def test_threshold_direction_not_inverted(self, session, cycle):
        """舊生率 0.95 ≥ 0.9 必須達標（+500）；若方向反了（<）會得 0 → FAIL。"""
        _mk_config(session)
        emp = _mk_employee(session, "E_SAB", "破壞測試師")
        cls = _mk_classroom(session, "破壞班")
        _seed_activity_for_class(session, cls.id, n_students=10)  # 才藝率 0 隔離
        _mk_target(
            session,
            cycle,
            cls.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),  # 明顯高於 0.9
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        amount = _amount_for(items, emp.id)
        assert amount == Decimal("500"), (
            f"舊生率 0.95≥0.9 應達標得 500，got {amount}；"
            f"若 0 → 門檻方向反了（用 <= 而非 >=）"
        )
        assert amount != Decimal("0.00"), "門檻方向反了 → sabotage 命中"


# ============ Test: 同班導同學期多班 period_label 不互蓋 ============


class TestMultiClassSameSemester:
    def test_one_head_teacher_two_classes_same_semester_no_collision(
        self, session, cycle
    ):
        """一位班導帶兩班（同上學期）→ 兩筆 FIRST 各自獨立（period_label 含 classroom_id）。

        若 period_label 不含 classroom_id，兩班會撞 uq 鍵 (cycle,emp,bonus_type,label)
        → 後者覆蓋前者，少發一筆紅利。
        """
        _mk_config(session)
        emp = _mk_employee(session, "E_MULTI", "兼帶兩班師")
        cls_a = _mk_classroom(session, "班甲")
        cls_b = _mk_classroom(session, "班乙")
        # 班甲：兩項皆達標 1500；班乙：才藝率 0 → 只舊生 500（金額不同便於辨識未互蓋）
        students_a = _seed_activity_for_class(session, cls_a.id, n_students=10)
        course = _mk_course(session, "美術")
        _register_students_for_activity(session, students_a[:9], course)
        _seed_activity_for_class(session, cls_b.id, n_students=10)  # 班乙才藝率 0
        _mk_target(
            session,
            cycle,
            cls_a.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),
        )
        _mk_target(
            session,
            cycle,
            cls_b.id,
            head_teacher_employee_id=emp.id,
            returning_student_rate=Decimal("0.950"),
        )
        session.commit()

        from services.year_end.auto_derive.semester_dividend import (
            derive_semester_dividend,
        )

        report = derive_semester_dividend(session, cycle)
        session.commit()

        items = _items(session, cycle, SpecialBonusType.SEMESTER_DIVIDEND_FIRST)
        emp_items = [it for it in items if it.employee_id == emp.id]
        # 兩筆獨立（未互蓋）
        assert len(emp_items) == 2, f"應兩筆獨立，got {len(emp_items)}（互蓋?）"
        amounts = sorted(it.amount for it in emp_items)
        assert amounts == [Decimal("500"), Decimal("1500")]
        assert report.written == 2
