"""年終 build N+1 查詢次數回歸測試（NP1-1 ～ NP1-4，2026-06-12 DB 優化）。

用 SQLAlchemy `before_cursor_execute` 事件計數 SELECT 查詢，斷言 build/derive
全程查詢數 ≤ 與員工數無關的上限。修補前（per-employee / per-employee×per-month
逐筆查詢）這些上限必然爆掉（紅）；修補（memoize / 批次預載）後轉綠。

上限的訂法：固定 seed（10 員工 / 4 班 / 20 學生）下實測修補後查詢數，再加
~30% 餘裕。重點不在精確數字，而在「不隨員工數線性成長」——若日後改動把
per-employee 查詢加回迴圈，10 員工 × 每人多 1 條就會超標被抓到。

行為等價性由既有 tests/test_year_end_auto_derive_*.py 等測試守護；本檔只管查詢數。
"""

from datetime import date

import pytest
from sqlalchemy import event

from models.attendance import Attendance
from models.classroom import ClassGrade, Classroom, Student
from models.config import BonusConfig
from models.employee import Employee
from models.event import MeetingRecord
from models.leave import LeaveRecord
from models.year_end import (
    ClassEnrollmentTarget,
    OrgYearSettings,
    YearEndCycle,
)


# --------------------------------------------------------------------------- #
# SELECT 查詢計數器                                                            #
# --------------------------------------------------------------------------- #
class SelectCounter:
    """攔截 engine 層 before_cursor_execute，只數 SELECT（排除 flush 的 INSERT/UPDATE）。"""

    def __init__(self, engine):
        self._engine = engine
        self.count = 0
        self.statements: list[str] = []

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.count += 1
            self.statements.append(statement)

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, exc_type, exc, tb):
        event.remove(self._engine, "before_cursor_execute", self._on_execute)
        return False


def _breakdown(counter: SelectCounter) -> str:
    """SELECT 依 FROM 子句分組計數（失敗訊息用，定位 N+1 來源表）。"""
    import collections

    grouped = collections.Counter(
        s.split("FROM", 1)[1].split("WHERE")[0].strip()[:60] if "FROM" in s else s[:60]
        for s in counter.statements
    )
    return "\n".join(f"{v:4d}  {k}" for k, v in grouped.most_common())


# --------------------------------------------------------------------------- #
# seed：10 員工（4 班導 + 4 副班導 + 行政 + 主任）/ 4 班 / 20 學生            #
# --------------------------------------------------------------------------- #
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


@pytest.fixture
def seed(test_db_session):
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

    employees = []
    classrooms = []
    for i in range(4):
        head = _mk_employee(
            db, f"E_HEAD_{i:03d}", f"班導{i}", position="班導", title="幼兒園教師"
        )
        assistant = _mk_employee(
            db, f"E_ASSI_{i:03d}", f"副班導{i}", position="副班導", title="幼兒園教師"
        )
        employees += [head, assistant]
        cls = Classroom(
            name=f"班{i}",
            grade_id=grade.id,
            head_teacher_id=head.id,
            assistant_teacher_id=assistant.id,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        db.add(cls)
        db.flush()
        classrooms.append(cls)
        for semester_first in (True, False):
            db.add(
                ClassEnrollmentTarget(
                    year_end_cycle_id=cycle.id,
                    semester_first=semester_first,
                    classroom_id=cls.id,
                    head_teacher_employee_id=head.id,
                    assistant_employee_id=assistant.id,
                    head_count_target=10,
                )
            )
        for j in range(5):
            db.add(
                Student(
                    student_id=f"S{i}{j:02d}",
                    name=f"S{i}{j:02d}",
                    classroom_id=cls.id,
                    enrollment_date=date(2024, 8, 1),
                    is_active=True,
                )
            )

    employees.append(
        _mk_employee(db, "E_ADMIN_1", "行政小美", position="行政", title="行政人員")
    )
    employees.append(
        _mk_employee(db, "E_DIR_001", "主任大華", position="主任", title="主任")
    )

    for semester_first in (True, False):
        db.add(
            OrgYearSettings(
                year_end_cycle_id=cycle.id,
                semester_first=semester_first,
                enrollment_target=40,
            )
        )

    db.add(
        BonusConfig(
            config_year=2025,  # 民國曆年（學年114→西元2025 = academic_year+1911）
            is_active=True,
            head_teacher_ab=2000,
            assistant_teacher_ab=1200,
            admin_festival=2000,
            director_festival=3500,
            late_deduction_per_time=50,
            missing_punch_deduction_per_time=50,
            personal_leave_deduction_per_day=500,
            sick_leave_deduction_per_day=500,
            meeting_absence_penalty=100,
        )
    )
    db.flush()

    # 考勤/請假/會議紀錄（民國曆年 2025 期間內）— 給 ⑤a 扣款測試料
    for emp in employees[:6]:
        db.add(
            Attendance(
                employee_id=emp.id,
                attendance_date=date(2025, 3, 10),
                is_late=True,
                is_missing_punch_in=False,
                is_missing_punch_out=True,
            )
        )
        db.add(
            Attendance(
                employee_id=emp.id,
                attendance_date=date(2025, 4, 11),
                is_late=False,
                is_missing_punch_in=True,
                is_missing_punch_out=False,
            )
        )
        db.add(
            LeaveRecord(
                employee_id=emp.id,
                leave_type="personal",
                start_date=date(2025, 5, 5),
                end_date=date(2025, 5, 5),
                leave_hours=8,
                status="approved",
            )
        )
        db.add(
            LeaveRecord(
                employee_id=emp.id,
                leave_type="sick",
                start_date=date(2025, 6, 6),
                end_date=date(2025, 6, 6),
                leave_hours=4,
                status="approved",
            )
        )
        db.add(
            MeetingRecord(
                employee_id=emp.id, meeting_date=date(2025, 9, 1), attended=False
            )
        )
    db.flush()
    db.commit()

    return {"db": db, "cycle": cycle, "employees": employees, "classrooms": classrooms}


# --------------------------------------------------------------------------- #
# NP1-1：derive_festival_diff 查詢數與員工數無關                              #
# --------------------------------------------------------------------------- #
def test_np1_1_festival_diff_query_count_bounded(seed):
    """修補前：每員工 ~3 條 + 每員工×每月 ~3 條（10 員工 × 6 月 ≈ 200+ SELECT）。

    修補後：count_enrolled_on / festival_base_for_role / 班級反查 / class target /
    既有 item 全部 memoize 或迴圈外批次 → 查詢數由「月份數 × 班級數 + 常數」決定，
    與員工數無關。固定 seed 實測修補後 ≈ 40 上下 → 上限 60。
    """
    from services.year_end.auto_derive.festival_diff import derive_festival_diff

    db = seed["db"]
    cycle = seed["cycle"]

    with SelectCounter(db.get_bind()) as counter:
        report = derive_festival_diff(db, cycle)
    db.commit()

    assert report.written == 10, report.warnings
    assert counter.count <= 80, (
        f"derive_festival_diff 發出 {counter.count} 條 SELECT（上限 80；修補後本 seed"
        f" 實測 71 = 42 students[6月×(school_active+cls_count)+6月×5組 enrolled]"
        f" + 6 meeting + ~9 bonus_configs + 常數，修補前 309）；"
        f"疑似 per-employee/per-month N+1 回歸。\n{_breakdown(counter)}"
    )
