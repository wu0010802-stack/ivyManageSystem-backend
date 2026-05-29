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
