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

        # 單月明細：6 月底為 26（1 名於 6/15 畢業），festival = 2000 × (26/24) ≈ 2167
        assert result["currentEnrollment"] == 26
        assert result["festivalBonus"] == 2167
        # 6 月發放實發 = 2-5 月每月各自比例的合計（業主 2026-04-25 確認）。
        # 2-5 月每月底人數均為 27（畢業日為 6/15，5 月底前仍在籍），
        # 故每月 festival = 2000 × (27/24) = 2250；4 個月合計 9000。
        # 超額：每月 max(0, 27-25) × 400 = 800（OVERTIME_TARGET 大班=25）；合計 3200。
        assert salary.festival_bonus == 2250 * 4
        assert salary.overtime_bonus == 800 * 4

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

    def test_breakdown_art_teacher_averages_across_classes(self, salary_engine_db):
        """跨班美師應依在籍人數加權平均節慶/超額獎金，
        而非只用 _pick_primary_classroom 挑到的單班。

        原 bug：_build_classroom_context_from_db 只在 role==assistant_teacher
        時才填 shared_other_classes；art_teacher 即使掛多班也只算第一個主要班級。
        """
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            big_grade = ClassGrade(name="大班", is_active=True)
            mid_grade = ClassGrade(name="中班", is_active=True)
            session.add_all([big_grade, mid_grade])
            session.flush()

            art_teacher = _create_teacher(
                session,
                employee_id="T907",
                name="跨班美師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            big_class = Classroom(
                name="大班甲",
                grade_id=big_grade.id,
                art_teacher_id=art_teacher.id,
                is_active=True,
            )
            mid_class = Classroom(
                name="中班乙",
                grade_id=mid_grade.id,
                art_teacher_id=art_teacher.id,
                is_active=True,
            )
            session.add_all([big_class, mid_class])
            session.flush()
            _create_students(session, big_class.id, 25, "ARTBIG")
            _create_students(session, mid_class.id, 20, "ARTMID")
            session.commit()
            teacher_id = art_teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)

        # art_teacher base 一律 2000；is_shared_assistant 由 festival.py 強制 True。
        # 大班 festival_target=20, score=round(2000*25/20)=2500
        # 中班 festival_target=18, score=round(2000*20/18)=2222
        # 加權 = round((2500*25 + 2222*20) / (25+20)) = round(2376.44) = 2376
        big_score = round(2000 * 25 / 20)
        mid_score = round(2000 * 20 / 18)
        expected_festival = round((big_score * 25 + mid_score * 20) / (25 + 20))
        assert result["festivalBonus"] == expected_festival

        # overtime: per_person=100（assistant_teacher 大班/中班）
        # 大班 overtime_target=20, count=5 → 500
        # 中班 overtime_target=18, count=2 → 200
        # 加權 = round((500*25 + 200*20) / 45) = round(366.67) = 367
        big_ot = max(0, 25 - 20) * 100
        mid_ot = max(0, 20 - 18) * 100
        expected_overtime = round((big_ot * 25 + mid_ot * 20) / (25 + 20))
        assert result["overtimeBonus"] == expected_overtime
        # 共用班標記應提示「兩班平均」，而非單班
        assert result["remark"] == "兩班平均"

    def test_breakdown_single_class_art_teacher_unchanged(self, salary_engine_db):
        """美師只掛一班時行為不變：直接以該班數據計算，不觸發加權平均。"""
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            art_teacher = _create_teacher(
                session,
                employee_id="T908",
                name="單班美師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            classroom = Classroom(
                name="獨立美語班",
                grade_id=grade.id,
                art_teacher_id=art_teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            _create_students(session, classroom.id, 25, "SOLO")
            session.commit()
            teacher_id = art_teacher.id

        result = engine.calculate_festival_bonus_breakdown(teacher_id, 2026, 6)

        # 單班：festival = round(2000 * 25/20) = 2500，無加權平均
        assert result["festivalBonus"] == round(2000 * 25 / 20)
        assert result["remark"] != "兩班平均"

    def test_batch_context_collects_art_teacher_other_classes(self, salary_engine_db):
        """批次路徑 _build_classroom_context_from_batch 對美師同樣需把
        art_to_classes 中的其他班加進 shared_other_classes。
        驗證批次入口的參數傳遞（不只 DB 路徑）。
        """
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            big_grade = ClassGrade(name="大班", is_active=True)
            mid_grade = ClassGrade(name="中班", is_active=True)
            session.add_all([big_grade, mid_grade])
            session.flush()

            art_teacher = _create_teacher(
                session,
                employee_id="T910",
                name="批次美師",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            big_class = Classroom(
                name="批次大班",
                grade_id=big_grade.id,
                art_teacher_id=art_teacher.id,
                is_active=True,
            )
            mid_class = Classroom(
                name="批次中班",
                grade_id=mid_grade.id,
                art_teacher_id=art_teacher.id,
                is_active=True,
            )
            session.add_all([big_class, mid_class])
            session.flush()
            big_id, mid_id = big_class.id, mid_class.id

            db_count_map = {big_id: 25, mid_id: 20}
            # 模擬 process_bulk_salary_calculation 預載的反查表
            art_to_classes = {art_teacher.id: [big_class, mid_class]}
            assistant_to_classes: dict = {}

            ctx = engine._build_classroom_context_from_batch(
                art_teacher,
                big_class,
                db_count_map,
                assistant_to_classes,
                art_to_classes,
            )

        assert ctx["role"] == "art_teacher"
        assert ctx["current_enrollment"] == 25
        # 美師 is_shared_assistant 由 festival.py 計算時強制 True；
        # 上下文中此旗標不應被置為 True（行為僅針對副班導）
        assert ctx["is_shared_assistant"] is False
        assert ctx["shared_other_classes"] == [
            {"grade_name": "中班", "current_enrollment": 20}
        ]
        assert ctx["shared_second_class"] == {
            "grade_name": "中班",
            "current_enrollment": 20,
        }


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


class TestDebugSnapshotAlignment:
    """build_salary_debug_snapshot 必須與 SalaryEngine.calculate_salary 公式對齊。"""

    def test_snapshot_gross_salary_includes_performance_and_special_bonus(
        self, salary_engine_db
    ):
        """SalaryRecord 上的 performance_bonus / special_bonus 必須計入 snapshot gross_salary。
        Why: engine.calculate_salary 在 line 1269-1275 將兩者加進 gross_salary，
        snapshot 漏算會讓核對頁顯示金額與實發短少。"""
        from services.salary_field_breakdown import build_salary_debug_snapshot

        engine, session_factory = salary_engine_db

        with session_factory() as session:
            teacher = _create_teacher(
                session,
                employee_id="SNAP1",
                name="績效員工",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            session.add(
                SalaryRecord(
                    employee_id=teacher.id,
                    salary_year=2026,
                    salary_month=3,
                    base_salary=30000,
                    performance_bonus=5000,
                    special_bonus=2000,
                )
            )
            session.commit()
            tid = teacher.id

        with session_factory() as session:
            emp = session.query(Employee).get(tid)
            snapshot = build_salary_debug_snapshot(session, engine, emp, 2026, 3)

        summary = snapshot["salary_summary"]
        assert summary["performance_bonus"] == 5000
        assert summary["special_bonus"] == 2000
        # gross_salary = prorated_base(30000) + perf(5000) + special(2000) + 其他 0
        assert summary["gross_salary"] >= 37000

    def test_snapshot_gross_salary_zero_perf_when_no_record(self, salary_engine_db):
        """無對應 SalaryRecord 時，performance/special 都為 0（不報錯）。"""
        from services.salary_field_breakdown import build_salary_debug_snapshot

        engine, session_factory = salary_engine_db

        with session_factory() as session:
            teacher = _create_teacher(
                session,
                employee_id="SNAP2",
                name="無紀錄員工",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            session.commit()
            tid = teacher.id

        with session_factory() as session:
            emp = session.query(Employee).get(tid)
            snapshot = build_salary_debug_snapshot(session, engine, emp, 2026, 3)

        summary = snapshot["salary_summary"]
        assert summary["performance_bonus"] == 0
        assert summary["special_bonus"] == 0

    def test_snapshot_festival_eligibility_uses_month_end_not_today(
        self, salary_engine_db
    ):
        """snapshot 節慶獎金資格判斷必須以薪資月份月底為基準。
        Why: 2025-11-15 到職員工查 2026-02 明細時，月底基準日 2026-02-28
        應視為符合資格；以 today() 判斷則年資計算錯誤。"""
        from services.salary_field_breakdown import build_salary_debug_snapshot

        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            teacher = _create_teacher(
                session,
                employee_id="SNAP3",
                name="臨界到職",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 11, 15),
            )
            classroom = Classroom(
                name="臨界班",
                grade_id=grade.id,
                head_teacher_id=teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id
            _create_students(session, classroom.id, 24, "SNAP")
            session.commit()
            tid = teacher.id

        with session_factory() as session:
            emp = (
                session.query(Employee)
                .options(
                    __import__("sqlalchemy").orm.joinedload(Employee.job_title_rel)
                )
                .get(tid)
            )
            snapshot = build_salary_debug_snapshot(session, engine, emp, 2026, 2)

        festival = snapshot["festival_bonus_detail"]
        assert festival is not None
        assert (
            festival.get("eligible") is True
        ), "月底基準日應視為符合資格；舊 bug 用 today() 會誤判"


class TestDistributionPeriodAccrual:
    """發放月節慶/超額獎金 = 期間每月各自比例合計（業主 2026-04-25 確認）。"""

    def _setup_classroom_with_constant_enrollment(
        self, session_factory, *, students: int
    ) -> int:
        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            assistant = _create_teacher(
                session,
                employee_id="ACC_A",
                name="班副",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            teacher = _create_teacher(
                session,
                employee_id="ACC_T",
                name="班導",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            classroom = Classroom(
                name="累積班",
                grade_id=grade.id,
                head_teacher_id=teacher.id,
                assistant_teacher_id=assistant.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id
            _create_students(
                session,
                classroom.id,
                students,
                "ACC",
                enrollment_date=date(2024, 8, 1),  # 早於所有期間月份
                is_active=True,
            )
            session.commit()
            return teacher.id

    def test_june_festival_bonus_equals_sum_of_feb_to_may(self, salary_engine_db):
        """6 月發放 = 2/3/4/5 月每月各自比例的合計（不含 6 月本身）。"""
        engine, session_factory = salary_engine_db

        # 25 位學生：festival_bonus 每月 = 2000 × (25/24) = 2083 (round)
        teacher_id = self._setup_classroom_with_constant_enrollment(
            session_factory, students=25
        )

        salary = engine.process_salary_calculation(teacher_id, 2026, 6)
        # 4 個月 × 2083 = 8332，按 round(2000 × 25/24) per month 累積
        per_month = round(2000 * 25 / 24)
        assert salary.festival_bonus == per_month * 4
        # 超額 = max(0, 25-25) × 400 = 0/month → 合計 0
        assert salary.overtime_bonus == 0

    def test_september_festival_bonus_equals_sum_of_jun_jul_aug(self, salary_engine_db):
        """9 月發放 = 6/7/8 月（3 個月）每月比例合計。"""
        engine, session_factory = salary_engine_db

        teacher_id = self._setup_classroom_with_constant_enrollment(
            session_factory, students=24
        )
        salary = engine.process_salary_calculation(teacher_id, 2026, 9)
        # 24 學生 / 24 target → ratio=1.0 → 2000/month；3 個月合計 6000
        assert salary.festival_bonus == 2000 * 3

    def test_february_festival_bonus_crosses_year_boundary(self, salary_engine_db):
        """2 月發放 = 前年 12 月 + 當年 1 月（共 2 個月）。"""
        engine, session_factory = salary_engine_db

        teacher_id = self._setup_classroom_with_constant_enrollment(
            session_factory, students=24
        )
        salary = engine.process_salary_calculation(teacher_id, 2026, 2)
        # 2 個月 × 2000 = 4000
        assert salary.festival_bonus == 2000 * 2

    def test_non_distribution_month_festival_remains_zero(self, salary_engine_db):
        """非發放月份節慶獎金仍為 0（不應被 period accrual 改寫）。"""
        engine, session_factory = salary_engine_db

        teacher_id = self._setup_classroom_with_constant_enrollment(
            session_factory, students=27
        )
        for month in (3, 4, 5, 7, 8, 10, 11):
            salary = engine.process_salary_calculation(teacher_id, 2026, month)
            assert salary.festival_bonus == 0, f"非發放月 {month} 應為 0"
            assert salary.overtime_bonus == 0, f"非發放月 {month} 超額應為 0"

    def test_period_accrual_uses_each_month_enrollment_not_distribution_month(
        self, salary_engine_db
    ):
        """關鍵語意：期間每月使用「該月底」人數，不是發放月當月人數。"""
        engine, session_factory = salary_engine_db

        with session_factory() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()

            assistant = _create_teacher(
                session,
                employee_id="MIX_A",
                name="變動副",
                title="教保員",
                position="教保員",
                hire_date=date(2025, 1, 1),
            )
            teacher = _create_teacher(
                session,
                employee_id="MIX_T",
                name="變動班導",
                title="幼兒園教師",
                position="幼兒園教師",
                hire_date=date(2025, 1, 1),
            )
            classroom = Classroom(
                name="變動班",
                grade_id=grade.id,
                head_teacher_id=teacher.id,
                assistant_teacher_id=assistant.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            teacher.classroom_id = classroom.id

            # 24 位學生在 5/1 入學（2、3、4 月皆 0；5 月 24 位）
            _create_students(
                session,
                classroom.id,
                24,
                "MAY",
                enrollment_date=date(2026, 5, 1),
                is_active=True,
            )
            session.commit()
            teacher_id = teacher.id

        salary = engine.process_salary_calculation(teacher_id, 2026, 6)
        # 2-4 月各自為 0/24 = 0；5 月為 24/24 = 2000；合計 = 2000
        assert salary.festival_bonus == 2000
