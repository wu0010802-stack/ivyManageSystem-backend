"""純函式測試 — 不依賴 DB session。

用 SimpleNamespace 模擬 Student / RecruitmentEventLog / StudentChangeLog 結構。
"""

from datetime import date
from types import SimpleNamespace

import pytest

from services.student_lifecycle_overview import (
    StepInfo,
    compute_outer_steps,
)


def _stu(**kw):
    defaults = dict(
        id=1,
        lifecycle_status="active",
        enrollment_date=None,
        graduation_date=None,
        withdrawal_date=None,
        classroom_id=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _fe(event_type, to_stage, created_at, from_stage=None):
    return SimpleNamespace(
        event_type=event_type,
        from_stage=from_stage,
        to_stage=to_stage,
        created_at=created_at,
        metadata_json=None,
    )


def _cl(event_type, event_date):
    return SimpleNamespace(event_type=event_type, event_date=event_date)


def test_compute_outer_steps_only_visited():
    """prospect 學生只有參觀記錄 → visited current（學生正處此階段），其餘 future。"""
    fe = [_fe("visit_logged", "visited", date(2024, 7, 12))]
    steps = compute_outer_steps(_stu(lifecycle_status="prospect"), fe, [])
    assert [s.key for s in steps] == [
        "visited",
        "deposited",
        "enrolled",
        "active",
        "terminal",
    ]
    assert steps[0].status == "current"
    assert steps[0].occurred_at == date(2024, 7, 12)
    for s in steps[1:]:
        assert s.status == "future"


def test_compute_outer_steps_visited_to_active_full_path():
    """全程記錄 → visited/deposited/enrolled done，active current，terminal future。"""
    fe = [
        _fe("visit_logged", "visited", date(2024, 7, 12)),
        _fe("deposit_added", "deposited", date(2024, 8, 1), from_stage="visited"),
        _fe("converted", "enrolled", date(2024, 8, 15), from_stage="deposited"),
        _fe("activated", "active", date(2024, 9, 1), from_stage="enrolled"),
    ]
    cl = [_cl("升狀態-active", date(2024, 9, 1))]
    steps = compute_outer_steps(_stu(lifecycle_status="active"), fe, cl)
    assert [s.status for s in steps] == ["done", "done", "done", "current", "future"]
    assert steps[3].occurred_at == date(2024, 9, 1)


def test_compute_outer_steps_graduated_full_terminal():
    """已畢業 → 全 done，active 也是 done。"""
    student = _stu(lifecycle_status="graduated", graduation_date=date(2027, 7, 1))
    fe = [
        _fe("visit_logged", "visited", date(2024, 7, 12)),
        _fe("converted", "enrolled", date(2024, 8, 15)),
        _fe("activated", "active", date(2024, 9, 1)),
    ]
    steps = compute_outer_steps(student, fe, [])
    assert steps[3].status == "done"
    assert steps[4].status == "done"
    assert steps[4].occurred_at == date(2027, 7, 1)


def test_compute_outer_steps_withdrawn_from_active():
    student = _stu(lifecycle_status="withdrawn", withdrawal_date=date(2025, 5, 1))
    steps = compute_outer_steps(student, [], [])
    assert steps[4].status == "done"
    assert steps[4].occurred_at == date(2025, 5, 1)


def test_compute_outer_steps_legacy_student_no_funnel_events():
    """早期學生無 funnel — enrolled 與 active 兜底用 enrollment_date。"""
    student = _stu(lifecycle_status="active", enrollment_date=date(2023, 9, 1))
    steps = compute_outer_steps(student, [], [])
    # visited / deposited 沒有 funnel record → future
    assert steps[0].status == "future"
    assert steps[1].status == "future"
    # enrolled 兜底用 enrollment_date（lifecycle=active 已穿過 enrolled 階段）
    assert steps[2].status == "done"
    assert steps[2].occurred_at == date(2023, 9, 1)
    # active current，occurred_at 兜底用 enrollment_date
    assert steps[3].status == "current"
    assert steps[3].occurred_at == date(2023, 9, 1)
    assert steps[4].status == "future"


from services.student_lifecycle_overview import (
    GradeStepInfo,
    compute_inner_grade_steps,
)


def _grade(grade_id, name, sort_order, is_grad=False):
    return SimpleNamespace(
        id=grade_id,
        name=name,
        sort_order=sort_order,
        is_graduation_grade=is_grad,
        is_active=True,
    )


def _transfer(student_id, to_classroom_id, transferred_at, from_classroom_id=None):
    return SimpleNamespace(
        student_id=student_id,
        from_classroom_id=from_classroom_id,
        to_classroom_id=to_classroom_id,
        transferred_at=transferred_at,
    )


# 共用 grades + classroom maps
GRADES_FOUR = [
    _grade(1, "幼幼", 1),
    _grade(2, "小班", 2),
    _grade(3, "中班", 3),
    _grade(4, "大班", 4, is_grad=True),
]
# classroom_id → grade_id
CR_GRADE = {11: 1, 21: 2, 31: 3, 41: 4}
CR_NAME = {11: "幼幼A", 21: "小班A", 31: "中班A", 41: "大班A"}


def test_compute_inner_grades_full_journey_from_yo_yo():
    """幼幼班一路升大班 — 4 個年級全有 transfer。"""
    student = _stu(classroom_id=41, enrollment_date=date(2022, 8, 15))
    transfers = [
        _transfer(1, 11, date(2022, 8, 15)),
        _transfer(1, 21, date(2023, 8, 1), from_classroom_id=11),
        _transfer(1, 31, date(2024, 8, 1), from_classroom_id=21),
        _transfer(1, 41, date(2025, 8, 1), from_classroom_id=31),
    ]
    steps = compute_inner_grade_steps(
        student, GRADES_FOUR, transfers, CR_GRADE, CR_NAME
    )
    assert [s.grade_id for s in steps] == [1, 2, 3, 4]
    assert [s.status for s in steps] == ["done", "done", "done", "current"]
    assert steps[0].entered_at == date(2022, 8, 15)
    assert steps[3].entered_at == date(2025, 8, 1)


def test_compute_inner_grades_mid_year_enrollment():
    """5 歲入大班 — 幼幼/小班/中班 = skipped，大班 = current。"""
    student = _stu(classroom_id=41, enrollment_date=date(2025, 8, 1))
    transfers = [_transfer(1, 41, date(2025, 8, 1))]
    steps = compute_inner_grade_steps(
        student, GRADES_FOUR, transfers, CR_GRADE, CR_NAME
    )
    assert [s.status for s in steps] == ["skipped", "skipped", "skipped", "current"]
    assert steps[0].entered_at is None
    assert steps[3].entered_at == date(2025, 8, 1)


def test_compute_inner_grades_no_transfer_history_fallback():
    """無 transfer 紀錄 — 用 student.classroom_id 推當前年級，前段全 skipped。"""
    student = _stu(classroom_id=31, enrollment_date=date(2024, 8, 1))
    steps = compute_inner_grade_steps(student, GRADES_FOUR, [], CR_GRADE, CR_NAME)
    assert [s.status for s in steps] == ["skipped", "skipped", "current", "future"]
    assert steps[2].entered_at == date(2024, 8, 1)  # 兜底用 enrollment_date


def test_compute_inner_grades_with_class_change_same_grade():
    """同年級內換班 — 不影響 stepper status，但 entered_at 取最早。"""
    student = _stu(classroom_id=31, enrollment_date=date(2024, 8, 1))
    transfers = [
        _transfer(1, 31, date(2024, 8, 1)),  # 中班A
        _transfer(1, 31, date(2024, 10, 5), from_classroom_id=31),  # 換到中班B 但仍中班
    ]
    steps = compute_inner_grade_steps(
        student, GRADES_FOUR, transfers, CR_GRADE, CR_NAME
    )
    assert steps[2].entered_at == date(2024, 8, 1)


def test_compute_inner_grades_skipped_middle_grade():
    """跳級：幼幼 → 中班（跳過小班）— 小班 skipped。"""
    student = _stu(classroom_id=31)
    transfers = [
        _transfer(1, 11, date(2023, 8, 1)),
        _transfer(1, 31, date(2024, 8, 1), from_classroom_id=11),
    ]
    steps = compute_inner_grade_steps(
        student, GRADES_FOUR, transfers, CR_GRADE, CR_NAME
    )
    assert [s.status for s in steps] == ["done", "skipped", "current", "future"]


from services.student_lifecycle_overview import (
    TerminalInfo,
    compute_terminal,
)


def test_compute_terminal_expected_graduation_in_future():
    """在學中 — 預測 graduation = 當前年級到畢業年級的學年差 + 開學年。"""
    student = _stu(lifecycle_status="active", classroom_id=21)  # 小班
    inner = [
        GradeStepInfo(grade_id=1, name="幼幼", sort_order=1, status="done"),
        GradeStepInfo(
            grade_id=2,
            name="小班",
            sort_order=2,
            status="current",
            entered_at=date(2024, 8, 1),
        ),
        GradeStepInfo(grade_id=3, name="中班", sort_order=3, status="future"),
        GradeStepInfo(grade_id=4, name="大班", sort_order=4, status="future"),
    ]
    t = compute_terminal(
        student,
        inner,
        graduation_grade_sort_order=4,
        term_end_date_for=lambda year: None,
    )
    assert t.kind == "none"
    assert t.actual_date is None
    # 進入小班學年 = 2024（2024/8-2025/7），diff = 4-2 = 2 → 期望學年 2026
    # 學年 2026 = 2026/8-2027/7 → 預計畢業 2027/7/31
    assert t.expected_date == date(2027, 7, 31)


def test_compute_terminal_at_graduation_grade():
    """已在畢業年級 — expected = 同學年 7/31。"""
    student = _stu(lifecycle_status="active", classroom_id=41)
    inner = [
        GradeStepInfo(
            grade_id=4,
            name="大班",
            sort_order=4,
            status="current",
            entered_at=date(2026, 8, 1),
        ),
    ]
    t = compute_terminal(
        student,
        inner,
        graduation_grade_sort_order=4,
        term_end_date_for=lambda year: None,
    )
    assert t.expected_date == date(2027, 7, 31)


def test_compute_terminal_graduated_actual():
    student = _stu(lifecycle_status="graduated", graduation_date=date(2027, 7, 1))
    t = compute_terminal(
        student,
        [],
        graduation_grade_sort_order=4,
        term_end_date_for=lambda year: None,
    )
    assert t.kind == "graduated"
    assert t.actual_date == date(2027, 7, 1)
    assert t.expected_date is None


def test_compute_terminal_uses_academic_term_end_date_when_available():
    """若 term_end_date_for 回傳實際 end_date，優先於 7/31 預設。"""
    student = _stu(lifecycle_status="active", classroom_id=41)
    inner = [
        GradeStepInfo(
            grade_id=4,
            name="大班",
            sort_order=4,
            status="current",
            entered_at=date(2026, 8, 1),
        ),
    ]
    t = compute_terminal(
        student,
        inner,
        graduation_grade_sort_order=4,
        term_end_date_for=lambda year: date(2027, 6, 30) if year == 2026 else None,
    )
    assert t.expected_date == date(2027, 6, 30)
