"""薪資列表 enrollment breakdown 批次化（P1 N+1 修補）。

api/salary/records.py 的 /salaries/records 列表原本逐筆呼叫
compute_enrollment_breakdown（每員工 3-5 查詢），全園 ~200 員工單頁
即 600-1000 次序列查詢。改為一次預載班級 + 一次 GROUP BY 各班人數後，
查詢數不隨員工數成長。

語意等價由既有 tests/test_salary_breakdown_enrollment.py 守護（單筆函式現
委派批次）；本檔額外驗證批次 API 直接行為 + 查詢數上限（N+1 哨兵）。
若日後有人把 per-employee 查詢加回迴圈，員工數 × 每人多 1 條即超標被抓到。
"""

from datetime import date

from sqlalchemy import event

from models.classroom import ClassGrade, Classroom, Student
from models.employee import Employee
from services.salary.breakdown_enrollment import (
    compute_enrollment_breakdown,
    compute_enrollment_breakdowns,
)


class _SelectCounter:
    """攔截 engine before_cursor_execute，只數 SELECT（排除 flush 的 INSERT/UPDATE）。"""

    def __init__(self, engine):
        self._engine = engine
        self.count = 0
        self.statements: list[str] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.count += 1
            self.statements.append(statement)

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._on)
        return False


# --------------------------------------------------------------------------- #
# fixtures（對齊 test_salary_breakdown_enrollment.py 的建構慣例）              #
# --------------------------------------------------------------------------- #
def _grade(session, name="大班"):
    g = ClassGrade(name=name)
    session.add(g)
    session.flush()
    return g


def _teacher(session, code, name="老師"):
    emp = Employee(
        employee_id=code,
        name=name,
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _classroom(session, name, grade_id, head=None, asst=None, art=None):
    c = Classroom(
        name=name,
        school_year=2026,
        semester=1,
        grade_id=grade_id,
        head_teacher_id=head,
        assistant_teacher_id=asst,
        art_teacher_id=art,
        is_active=True,
    )
    session.add(c)
    session.flush()
    return c


def _students(session, classroom_id, n):
    for i in range(n):
        s = Student(
            student_id=f"S{classroom_id}{i:03d}",
            name=f"學生{i}",
            classroom_id=classroom_id,
            is_active=True,
            enrollment_date=date(2025, 8, 1),
            lifecycle_status="active",
        )
        session.add(s)
    session.flush()


# --------------------------------------------------------------------------- #
# 語意：批次結果逐員工與單筆函式完全一致                                       #
# --------------------------------------------------------------------------- #
def test_batch_matches_singleton_across_roles(test_db_session):
    session = test_db_session
    grade = _grade(session)

    head = _teacher(session, "BT001", "班導")
    head_room = _classroom(session, "大班 A", grade.id, head=head.id)
    _students(session, head_room.id, n=23)

    asst = _teacher(session, "BT002", "副班導")
    _classroom(session, "中班 A", grade.id, asst=asst.id)
    _classroom(session, "中班 B", grade.id, asst=asst.id)

    art = _teacher(session, "BT003", "美術老師")
    _classroom(session, "美術班", grade.id, art=art.id)

    admin = _teacher(session, "BT004", "行政")  # 不帶任何班 → None

    ids = [head.id, asst.id, art.id, admin.id]
    target = date(2026, 5, 31)

    batch = compute_enrollment_breakdowns(session, ids, target)

    # 所有輸入 id 都要在回傳 dict（非教師為 None）
    assert set(batch.keys()) == set(ids)
    assert batch[admin.id] is None

    # 與單筆函式逐一比對（守護未來實作分歧）
    for emp_id in ids:
        assert batch[emp_id] == compute_enrollment_breakdown(session, emp_id, target)

    # 顯式值（不依賴單筆函式正確性）
    assert batch[head.id]["enrollment"]["total"] == 23
    assert batch[head.id]["enrollment"]["classroom_name"] == "大班 A"
    assert batch[head.id]["assistant"] is None
    assert batch[asst.id]["enrollment"] is None
    assert batch[asst.id]["assistant"] == {"by_classroom": ["中班 A", "中班 B"]}
    assert batch[art.id]["assistant"] == {"by_classroom": ["美術班"]}


def test_batch_empty_employee_list_returns_empty(test_db_session):
    assert compute_enrollment_breakdowns(test_db_session, [], date(2026, 5, 31)) == {}


def test_batch_multi_head_flag_preserved(test_db_session):
    session = test_db_session
    grade = _grade(session)
    teacher = _teacher(session, "BMH", "多頭老師")
    c1 = _classroom(session, "甲班", grade.id, head=teacher.id)
    c2 = _classroom(session, "乙班", grade.id, head=teacher.id)
    _students(session, c1.id, n=10)
    _students(session, c2.id, n=12)

    batch = compute_enrollment_breakdowns(session, [teacher.id], date(2026, 5, 31))

    # 取 id 升冪第一個（甲班），補 multi_head=True
    assert batch[teacher.id]["enrollment"]["classroom_name"] == "甲班"
    assert batch[teacher.id]["enrollment"]["multi_head"] is True


# --------------------------------------------------------------------------- #
# N+1 哨兵：查詢數不隨員工數成長                                               #
# --------------------------------------------------------------------------- #
def test_batch_query_count_independent_of_employee_count(test_db_session):
    session = test_db_session
    grade = _grade(session)

    ids = []
    for i in range(8):
        t = _teacher(session, f"BQ{i:03d}", f"班導{i}")
        room = _classroom(session, f"班{i}", grade.id, head=t.id)
        _students(session, room.id, n=5)
        ids.append(t.id)

    engine = session.get_bind()
    with _SelectCounter(engine) as counter:
        batch = compute_enrollment_breakdowns(session, ids, date(2026, 5, 31))

    assert len(batch) == 8
    # 一次撈班級（含 joinedload grade）+ 一次 GROUP BY 各班人數 ≈ 2 條；
    # 給足餘裕設 ≤ 4。逐筆 N+1 會是 8 員工 × 3 ≈ 24，必然超標。
    assert (
        counter.count <= 4
    ), f"批次 enrollment breakdown 查詢數 {counter.count} 超標（疑 N+1 回歸）：\n" + "\n".join(
        counter.statements
    )
