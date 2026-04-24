"""薪資節慶獎金明細回歸測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    ClassGrade,
    Classroom,
    Employee,
    LeaveRecord,
    SalaryRecord,
    Student,
    WorkdayOverride,
)
from services.salary_engine import SalaryEngine


@pytest.fixture
def salary_engine_db(tmp_path):
    """建立隔離 sqlite DB，驗證 salary breakdown 的實際查詢路徑。"""
    db_path = tmp_path / "salary-breakdown-regressions.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
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


def _create_teacher(
    session,
    *,
    employee_id: str,
    name: str,
    title: str,
    position: str,
    hire_date: date,
) -> Employee:
    teacher = Employee(
        employee_id=employee_id,
        name=name,
        title=title,
        position=position,
        employee_type="regular",
        base_salary=30000,
        insurance_salary_level=30000,
        hire_date=hire_date,
        is_active=True,
    )
    session.add(teacher)
    session.flush()
    return teacher


def _create_students(
    session,
    classroom_id: int,
    count: int,
    prefix: str,
    *,
    enrollment_date: date | None = None,
    graduation_date: date | None = None,
    is_active: bool = True,
):
    for idx in range(count):
        session.add(
            Student(
                student_id=f"{prefix}{idx:03d}",
                name=f"{prefix}學生{idx}",
                classroom_id=classroom_id,
                enrollment_date=enrollment_date,
                graduation_date=graduation_date,
                is_active=is_active,
            )
        )


class TestFestivalEligibilityReferenceDate:
    def test_calculate_salary_uses_salary_month_reference_date(self, engine):
        employee = {
            "employee_id": "E900",
            "name": "六月應符合資格",
            "title": "幼兒園教師",
            "position": "幼兒園教師",
            "employee_type": "regular",
            "base_salary": 30000,
            "hourly_rate": 0,
            "insurance_salary": 30000,
            "dependents": 0,
            "hire_date": "2026-03-01",
        }
        classroom_context = {
            "role": "head_teacher",
            "grade_name": "大班",
            "current_enrollment": 24,
            "has_assistant": True,
            "is_shared_assistant": False,
        }

        breakdown = engine.calculate_salary(
            employee=employee,
            year=2026,
            month=6,
            classroom_context=classroom_context,
        )

        assert breakdown.festival_bonus == 2000

    def test_reference_date_is_month_end_not_month_start(self, engine):
        """2025-11-15 到職的員工，2026-02 月薪結算時年資：
        - 月初基準日 2026-02-01：滿 2 個月 18 天 → 不足 3 個月（舊 bug：發不到獎金）
        - 月底基準日 2026-02-28：滿 3 個月 13 天 → 符合資格
        """
        employee = {
            "employee_id": "E901",
            "name": "臨界到職",
            "title": "幼兒園教師",
            "position": "幼兒園教師",
            "employee_type": "regular",
            "base_salary": 30000,
            "hourly_rate": 0,
            "insurance_salary": 30000,
            "dependents": 0,
            "hire_date": "2025-11-15",
        }
        classroom_context = {
            "role": "head_teacher",
            "grade_name": "大班",
            "current_enrollment": 24,
            "has_assistant": True,
            "is_shared_assistant": False,
        }
        breakdown = engine.calculate_salary(
            employee=employee,
            year=2026,
            month=2,
            classroom_context=classroom_context,
        )
        assert breakdown.festival_bonus > 0, "月底基準日應視為符合資格，應發獎金"


class TestFestivalBonusBreakdownRegressions:
    def test_uses_month_end_enrollment_for_festival_and_overtime_bonus(
        self, salary_engine_db
    ):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            assistant_teacher = _create_teacher(
                session,
                employee_id="T905A",
                name="月底副班導",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            teacher = _create_teacher(
                session,
                employee_id="T905",
                name="月底老師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            classroom = Classroom(
                name="月底班",
                grade_id=grade.id,
                head_teacher_id=teacher.id,
                assistant_teacher_id=assistant_teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id

            _create_students(
                session,
                classroom.id,
                26,
                "END",
                enrollment_date=date(2025, 8, 1),
                is_active=True,
            )
            _create_students(
                session,
                classroom.id,
                1,
                "GRAD",
                enrollment_date=date(2025, 8, 1),
                graduation_date=date(2026, 6, 15),
                is_active=True,
            )
            session.commit()
            teacher_id = teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)
        salary = engine.process_salary_calculation(teacher_id, 2026, 6)

        assert result["currentEnrollment"] == 26
        assert result["festivalBonus"] == 2167
        assert salary.festival_bonus == 2167
        assert salary.overtime_bonus == 400

    def test_breakdown_uses_salary_month_reference_date(self, salary_engine_db):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            assistant_teacher = _create_teacher(
                session,
                employee_id="T900A",
                name="六月副班導",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            teacher = _create_teacher(
                session,
                employee_id="T900",
                name="六月老師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2026, 3, 1),
            )
            classroom = Classroom(
                name="六月班",
                grade_id=grade.id,
                head_teacher_id=teacher.id,
                assistant_teacher_id=assistant_teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id
            _create_students(session, classroom.id, 24, "JUN")
            session.commit()
            teacher_id = teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)

        assert result["festivalBonus"] == 2000
        assert result["remark"] != "未滿3個月"

    def test_breakdown_art_teacher_matches_salary_engine(self, salary_engine_db):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="中班", is_active=True)
            session.add(grade)
            session.flush()

            assistant_teacher = _create_teacher(
                session,
                employee_id="T901A",
                name="中班副班導",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            teacher = _create_teacher(
                session,
                employee_id="T901",
                name="美語老師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            classroom = Classroom(
                name="美語班",
                grade_id=grade.id,
                assistant_teacher_id=assistant_teacher.id,
                art_teacher_id=teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id
            _create_students(session, classroom.id, 18, "ART")
            session.commit()
            teacher_id = teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)
        salary = engine.calculate_salary(
            employee={
                "employee_id": "T901",
                "name": "美語老師",
                "title": "幼兒園教師",
                "position": "幼兒園教師",
                "employee_type": "regular",
                "base_salary": 30000,
                "hourly_rate": 0,
                "insurance_salary": 30000,
                "dependents": 0,
                "hire_date": "2025-01-01",
            },
            year=2026,
            month=6,
            classroom_context={
                "role": "art_teacher",
                "grade_name": "中班",
                "current_enrollment": 18,
                "has_assistant": True,
                "is_shared_assistant": False,
            },
        )

        assert result["festivalBonus"] == salary.festival_bonus == 2000

    def test_breakdown_shared_assistant_averages_two_classes(self, salary_engine_db):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            teacher = _create_teacher(
                session,
                employee_id="T902",
                name="共用副班導",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            first_classroom = Classroom(
                name="共用甲班",
                grade_id=grade.id,
                assistant_teacher_id=teacher.id,
                is_active=True,
            )
            second_classroom = Classroom(
                name="共用乙班",
                grade_id=grade.id,
                assistant_teacher_id=teacher.id,
                is_active=True,
            )
            session.add(first_classroom)
            session.add(second_classroom)
            session.flush()
            teacher.classroom_id = first_classroom.id
            _create_students(session, first_classroom.id, 20, "SHA")
            _create_students(session, second_classroom.id, 16, "SHB")
            session.commit()
            teacher_id = teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)

        # 加權平均：base=1200, 甲班 enroll=20(score 1200), 乙班 enroll=16(score 960)
        # 加權 = (1200*20 + 960*16) / 36 = 39360 / 36 ≈ 1093
        assert result["festivalBonus"] == round((1200 * 20 + 960 * 16) / (20 + 16))


class TestDailySalaryBaseConsistency:
    """`emp.base_salary` 與 `_resolve_standard_base(emp)` 不同時，
    請假扣款 / 曠職扣款 / 遲到早退扣款必須一致使用同一個底薪基準。

    Bug context (2026-04-22):
    `_resolve_standard_base` 於 2026-04-16 加入後，`_load_emp_dict` 將
    `emp_dict["base_salary"]` 設為標準化底薪。但 `process_salary_calculation`
    於 1807 行（leave_deduction）與 `_detect_absences` 於 1762 行
    （absence_deduction）仍直接讀 `emp.base_salary`，導致同一筆薪資
    內的扣款基準不一致。
    """

    def test_leave_deduction_uses_standardized_base_when_raw_diverges(
        self, salary_engine_db
    ):
        """raw=42000、standard(head_teacher_a)=39240 時，
        請假扣款應以 standard 39240/30=1308 計算，而非 raw 42000/30≈1400。"""
        engine, session_factory = salary_engine_db
        engine._position_salary_standards = {"head_teacher_a": 39240}

        with session_factory() as session:
            teacher = Employee(
                employee_id="DIV_LEAVE",
                name="非標班導",
                title="幼兒園教師",
                position="班導",
                employee_type="regular",
                base_salary=42000,
                insurance_salary_level=42000,
                # 計算 2026/3 的薪資：hire_date 必須早於或等於該月，否則
                # proration 會（正確地）回 0，導致 net 為負。
                hire_date=date(2025, 4, 1),
                is_active=True,
            )
            session.add(teacher)
            session.flush()
            session.add(
                LeaveRecord(
                    employee_id=teacher.id,
                    leave_type="personal",
                    leave_hours=8,
                    start_date=date(2026, 3, 5),
                    end_date=date(2026, 3, 5),
                    is_approved=True,
                    deduction_ratio=1.0,
                )
            )
            session.commit()
            tid = teacher.id

        salary = engine.process_salary_calculation(tid, 2026, 3)

        assert salary.leave_deduction == 1308

    def test_absence_deduction_uses_standardized_base_when_raw_diverges(
        self, salary_engine_db
    ):
        """`_detect_absences` 內部的曠職日薪基準同樣應採標準化底薪。
        2026-03-31 (週二) 入職，無打卡無請假 → 1 天曠職。
        應扣 39240/30=1308，而非 42000/30≈1400。
        直接呼叫 `_detect_absences` 以避開 `process_salary_calculation`
        的 net_salary < 0 防護（折算後底薪僅 1 天 < 曠職扣款 + 保費）。
        """
        engine, session_factory = salary_engine_db
        engine._position_salary_standards = {"head_teacher_a": 39240}

        with session_factory() as session:
            teacher = Employee(
                employee_id="DIV_ABSENCE",
                name="非標曠職",
                title="幼兒園教師",
                position="班導",
                employee_type="regular",
                base_salary=42000,
                insurance_salary_level=42000,
                hire_date=date(2026, 3, 31),
                is_active=True,
            )
            session.add(teacher)
            session.commit()
            tid = teacher.id

        with session_factory() as session:
            emp = session.query(Employee).get(tid)
            absent_count, absence_amount = engine._detect_absences(
                session,
                emp,
                attendances=[],
                approved_leaves=[],
                start_date=date(2026, 3, 1),
                end_date=date(2026, 3, 31),
                year=2026,
                month=3,
            )

        assert absent_count == 1
        assert round(absence_amount) == 1308


class TestMakeupWorkdayAbsenceDetection:
    """P2-1 回歸：補班日（WorkdayOverride）未出勤應被視為曠職。

    Bug context (2026-04-24): `_detect_absences` 過去只查 Holiday 與
    DailyShift，沒查 WorkdayOverride。官方補班週六員工無打卡且無請假
    時不會扣曠職，與請假/工作日判定邏輯不一致。
    """

    def test_absent_on_makeup_saturday_counts_as_absence(self, salary_engine_db):
        """2026-02-07 (Sat) 為官方補班日，員工無打卡且無請假 → 應算 1 天曠職。"""
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            teacher = Employee(
                employee_id="MAKEUP_ABS",
                name="補班曠職",
                title="幼兒園教師",
                position="幼兒園教師",
                employee_type="regular",
                base_salary=30000,
                insurance_salary_level=30000,
                hire_date=date(2025, 1, 1),
                is_active=True,
            )
            session.add(teacher)
            session.add(
                WorkdayOverride(
                    date=date(2026, 2, 7),
                    name="補班日",
                    is_active=True,
                )
            )
            session.commit()
            tid = teacher.id

        with session_factory() as session:
            emp = session.query(Employee).get(tid)
            absent_count, _ = engine._detect_absences(
                session,
                emp,
                attendances=[],
                approved_leaves=[],
                start_date=date(2026, 2, 1),
                end_date=date(2026, 2, 28),
                year=2026,
                month=2,
            )

        # 2026/2 有 20 個平日（2/2-2/6, 2/9-2/13, 2/16-2/20, 2/23-2/27）+ 補班 2/7 = 21
        assert absent_count == 21

    def test_no_makeup_entry_only_counts_weekdays(self, salary_engine_db):
        """未登錄 WorkdayOverride 時，週六不應被視為工作日。"""
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            teacher = Employee(
                employee_id="NO_MAKEUP",
                name="無補班",
                title="幼兒園教師",
                position="幼兒園教師",
                employee_type="regular",
                base_salary=30000,
                insurance_salary_level=30000,
                hire_date=date(2025, 1, 1),
                is_active=True,
            )
            session.add(teacher)
            session.commit()
            tid = teacher.id

        with session_factory() as session:
            emp = session.query(Employee).get(tid)
            absent_count, _ = engine._detect_absences(
                session,
                emp,
                attendances=[],
                approved_leaves=[],
                start_date=date(2026, 2, 1),
                end_date=date(2026, 2, 28),
                year=2026,
                month=2,
            )

        # 僅 20 個平日
        assert absent_count == 20


class TestPreviewSalaryCalculationNoSideEffect:
    """P1-2 回歸：preview 端點（GET）不應寫入 SalaryRecord。

    Bug context (2026-04-24): final-salary-preview 直接呼叫會寫入 DB 的
    process_salary_calculation，且讀取不存在的 breakdown.pension 屬性會拋
    AttributeError 回 500，但 commit 已落地造成 DB 污染。
    """

    def test_preview_does_not_write_salary_record(self, salary_engine_db):
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            teacher = Employee(
                employee_id="PREVIEW",
                name="預覽員工",
                title="幼兒園教師",
                position="幼兒園教師",
                employee_type="regular",
                base_salary=30000,
                insurance_salary_level=30000,
                hire_date=date(2025, 1, 1),
                is_active=True,
            )
            session.add(teacher)
            session.commit()
            tid = teacher.id

        breakdown = engine.preview_salary_calculation(tid, 2026, 3)

        # 計算成功且必備欄位存在
        assert breakdown.base_salary == 30000
        assert hasattr(breakdown, "pension_self")

        # 關鍵：DB 不應有任何 SalaryRecord 被寫入
        with session_factory() as session:
            count = (
                session.query(SalaryRecord)
                .filter(
                    SalaryRecord.employee_id == tid,
                    SalaryRecord.salary_year == 2026,
                    SalaryRecord.salary_month == 3,
                )
                .count()
            )
        assert count == 0

    def test_process_calculation_still_writes_salary_record(self, salary_engine_db):
        """對照組：process_salary_calculation 仍應寫入 SalaryRecord（未被誤傷）。"""
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            teacher = Employee(
                employee_id="PROCESS",
                name="正式員工",
                title="幼兒園教師",
                position="幼兒園教師",
                employee_type="regular",
                base_salary=30000,
                insurance_salary_level=30000,
                hire_date=date(2025, 1, 1),
                is_active=True,
            )
            session.add(teacher)
            session.commit()
            tid = teacher.id

        engine.process_salary_calculation(tid, 2026, 3)

        with session_factory() as session:
            count = (
                session.query(SalaryRecord)
                .filter(
                    SalaryRecord.employee_id == tid,
                    SalaryRecord.salary_year == 2026,
                    SalaryRecord.salary_month == 3,
                )
                .count()
            )
        assert count == 1
