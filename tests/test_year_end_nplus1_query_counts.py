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
    # 上限 110：P1-9 起「已發」人數改走 resolve_bonus_counts（與 payroll 同源、
    # 含快照/daily_weighted 感知），每月多一次 snapshot + BonusConfig mode 查詢，
    # 本 seed 由 71 升至 96。此為「每月」固定成本（與員工數無關），N+1 守衛的核心
    # （catch per-employee 級 309 條回歸）仍有效。
    assert counter.count <= 110, (
        f"derive_festival_diff 發出 {counter.count} 條 SELECT（上限 110；P1-9 後本 seed"
        f" 實測 ~96 = 每月 resolve_bonus_counts（snapshot+mode+live）+ 6月×5組 enrolled"
        f" + 6 meeting + bonus_configs + 常數，修補前 309）；"
        f"疑似 per-employee/per-month N+1 回歸。\n{_breakdown(counter)}"
    )


# --------------------------------------------------------------------------- #
# NP1-2：refresh_enrollment_rates 全校月底快照跨班共用                        #
# --------------------------------------------------------------------------- #
def test_np1_2_refresh_enrollment_rates_query_count_bounded(seed):
    """修補前：每個 ClassEnrollmentTarget（4 班 × 2 學期 = 8 列）各自重算
    「全校逐月在籍清單 + 轉班歸屬 map」（8 × 6 月 × ~2 條 ≈ 100 條 SELECT）。

    修補後：12 個 distinct 月底的全校快照只算一次、所有班共用 →
    12 × ~2 條 + org 4 條 + 常數 ≈ 30。上限 40：班數 +1（×6 月×2 條）即超標。
    """
    from services.year_end.settlement_builder import refresh_enrollment_rates

    db = seed["db"]
    cycle = seed["cycle"]

    with SelectCounter(db.get_bind()) as counter:
        refresh_enrollment_rates(db, cycle)
    db.commit()

    # 行為 sanity：4 班 × 2 學期 target 全有 stored rate（5 生 / 目標 10 = 50%）
    from decimal import Decimal

    from sqlalchemy import select as _select

    rows = db.scalars(
        _select(ClassEnrollmentTarget).where(
            ClassEnrollmentTarget.year_end_cycle_id == cycle.id
        )
    ).all()
    assert len(rows) == 8
    for row in rows:
        assert row.class_performance_rate == Decimal("50.00")

    assert counter.count <= 50, (
        f"refresh_enrollment_rates 發出 {counter.count} 條 SELECT（上限 50；"
        f"修補後本 seed 實測 43 = 12 distinct 月底 ×3[candidates+transfers+fallback]"
        f" + org 4 + 常數，修補前 ~100 且隨班數線性成長）；"
        f"疑似 per-target 重算全校快照回歸。\n{_breakdown(counter)}"
    )


# --------------------------------------------------------------------------- #
# NP1-3：derive_all_attendance_deductions 批次聚合（非逐員工 5 條）           #
# --------------------------------------------------------------------------- #
def test_np1_3_attendance_deductions_batch_query_count_bounded(seed):
    """修補前：「batch」只批次了 cfg，仍逐員工 5 條查詢（10 員工 ≈ 52 條 SELECT），
    且 _count_missing_punch 撈整年 row 回 Python 數。

    修補後：late/missing 合併一條 SUM(CASE) GROUP BY、leave 一條 GROUP BY
    (employee_id, leave_type)、meeting 一條 GROUP BY → 全體共 3 條 + cfg ~2。
    上限 12：任何 per-employee 查詢回歸（+10）即超標。
    """
    from services.year_end.auto_derive.attendance_deductions import (
        derive_all_attendance_deductions,
    )

    db = seed["db"]
    cycle = seed["cycle"]
    employees = seed["employees"]
    # seed fixture commit 後 ORM 物件 expired，先 touch 屬性讓 refresh 發生在計數窗外
    # （真實 build 路徑員工為同 transaction 現載、無 per-emp refresh，非本測試標的）。
    _ = [(e.id, cycle.id) for e in employees]

    with SelectCounter(db.get_bind()) as counter:
        result = derive_all_attendance_deductions(db, cycle, employees)

    # 行為 sanity：前 6 員工有扣款料（遲到1+未打卡2合併 -150 / 事假1天 -500 /
    # 病假0.5天 -250 / 會議缺席1次 -100），其餘 4 人全 0。
    from decimal import Decimal

    assert set(result.keys()) == {e.id for e in employees}
    for emp in employees[:6]:
        d = result[emp.id]
        assert d.late == Decimal("-150.00")
        assert d.personal_leave == Decimal("-500.00")
        assert d.sick_leave == Decimal("-250.00")
        assert d.meeting == Decimal("-100.00")
        assert d.calc_meta["late_count"] == 1
        assert d.calc_meta["missing_punch_count"] == 2
    for emp in employees[6:]:
        d = result[emp.id]
        assert (
            d.late == d.personal_leave == d.sick_leave == d.meeting == Decimal("0.00")
        )

    assert counter.count <= 12, (
        f"derive_all_attendance_deductions 發出 {counter.count} 條 SELECT"
        f"（上限 12；修補後本 seed 實測 ~5 = 考勤/請假/會議 3 條 GROUP BY + cfg ~2，"
        f"修補前 ~52）；疑似 per-employee 查詢回歸。\n{_breakdown(counter)}"
    )


# --------------------------------------------------------------------------- #
# NP1-4：build_settlements 主迴圈批次預載（settlement/snapshot/special/target）#
# --------------------------------------------------------------------------- #
def test_np1_4_build_settlements_main_loop_query_count_bounded(seed):
    """refresh_rates=False 隔離主迴圈（refresh/derive_all 屬 NP1-1/2/3 各自的測試）。

    修補前：每員工查 settlement 1 + 班導 target 2（class-role）+ special SUM 1 +
    snapshot 1（10 員工 ≈ 50 條）+ 副班導查不到 target 的全校舊生率 fallback 逐人重算。

    修補後：四者迴圈外各一條撈成 dict + 全校舊生率 fallback cycle 級 memo →
    常數級。上限 30：任何 per-employee 查詢回歸（+10）即超標。
    """
    from services.year_end.settlement_builder import build_settlements

    db = seed["db"]
    employees = seed["employees"]
    _ = [e.id for e in employees]  # 排除 expired refresh 的計數雜訊（同 NP1-3 註解）

    with SelectCounter(db.get_bind()) as counter:
        result = build_settlements(
            db, 114, included_resigned_ids=None, actor_id=None, refresh_rates=False
        )
    db.commit()

    assert result.built == 10
    assert result.skipped_finalized == 0

    assert counter.count <= 30, (
        f"build_settlements 主迴圈發出 {counter.count} 條 SELECT（上限 30；"
        f"修補後本 seed 實測 24 = 預載 4 dict + 扣款批次 + 底薪/級距等常數，修補前 72）；"
        f"疑似 per-employee 查詢回歸。\n{_breakdown(counter)}"
    )


def test_np1_4_build_settlements_idempotent_rerun_query_count(seed):
    """重跑 build（settlement/snapshot 已存在 → 走 update 路徑）查詢數同樣有界，
    且兩輪結果 idempotent（version bump、金額不變）——守住「預載 dict 取代逐筆
    select 後，update 路徑仍對得到既有列、不會誤建重複列」。"""
    from sqlalchemy import select as _select

    from models.year_end import EmployeeYearEndSnapshot, YearEndSettlement
    from services.year_end.settlement_builder import build_settlements

    db = seed["db"]
    cycle = seed["cycle"]

    build_settlements(db, 114, included_resigned_ids=None, actor_id=None)
    db.commit()
    first_amounts = {
        s.employee_id: (s.total_amount, s.version)
        for s in db.scalars(
            _select(YearEndSettlement).where(
                YearEndSettlement.year_end_cycle_id == cycle.id
            )
        )
    }
    assert len(first_amounts) == 10

    with SelectCounter(db.get_bind()) as counter:
        result = build_settlements(db, 114, included_resigned_ids=None, actor_id=None)
    db.commit()

    assert result.built == 10
    rows = db.scalars(
        _select(YearEndSettlement).where(
            YearEndSettlement.year_end_cycle_id == cycle.id
        )
    ).all()
    snaps = db.scalars(
        _select(EmployeeYearEndSnapshot).where(
            EmployeeYearEndSnapshot.year_end_cycle_id == cycle.id
        )
    ).all()
    assert len(rows) == 10, "重跑不可誤建重複 settlement"
    assert len(snaps) == 10, "重跑不可誤建重複 snapshot"
    for row in rows:
        amount, version = first_amounts[row.employee_id]
        assert row.total_amount == amount, "重跑金額必須 idempotent"
        assert row.version == version + 1, "重跑應 bump version"

    # refresh_rates=True 全鏈（refresh + derive_all + 主迴圈）：NP1-1~4 合計上限。
    # 修補前 dev 規模單次 build ≈ 900–1,100 條；修補後本 seed 實測 187。
    assert counter.count <= 220, (
        f"build_settlements 全鏈（refresh_rates=True）發出 {counter.count} 條 SELECT"
        f"（上限 220）；疑似 N+1 回歸。\n{_breakdown(counter)}"
    )
