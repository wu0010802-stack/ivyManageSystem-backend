"""tests/test_recruitment_funnel_pure.py — 招生漏斗純函式測試"""

from dataclasses import dataclass
from typing import Optional

import pytest

from services.recruitment_funnel import (
    Stage,
    derive_stage,
    can_transition,
    is_destructive,
)


@dataclass
class _Visit:
    id: int = 1
    has_deposit: bool = False
    enrolled: bool = False


@dataclass
class _Student:
    id: int = 100
    lifecycle_status: str = "enrolled"


class TestDeriveStage:
    def test_visited_when_no_deposit_no_student(self):
        assert derive_stage(_Visit(has_deposit=False), None) == "visited"

    def test_deposited_when_has_deposit_no_student(self):
        assert derive_stage(_Visit(has_deposit=True), None) == "deposited"

    def test_enrolled_when_student_lifecycle_enrolled(self):
        assert (
            derive_stage(
                _Visit(has_deposit=True), _Student(lifecycle_status="enrolled")
            )
            == "enrolled"
        )

    def test_active_when_student_lifecycle_active(self):
        assert (
            derive_stage(_Visit(has_deposit=True), _Student(lifecycle_status="active"))
            == "active"
        )

    def test_student_presence_overrides_deposit_flag(self):
        # visit 顯示無 deposit 但 student 已建立 — student 為準
        assert derive_stage(_Visit(has_deposit=False), _Student()) == "enrolled"


class TestCanTransition:
    @pytest.mark.parametrize("frm", ["visited", "deposited", "enrolled", "active"])
    @pytest.mark.parametrize("to", ["visited", "deposited", "enrolled", "active"])
    def test_any_pair_allowed_in_phase_a(self, frm, to):
        assert can_transition(frm, to) is True


class TestIsDestructive:
    @pytest.mark.parametrize(
        "frm,to,expected",
        [
            ("visited", "deposited", False),
            ("deposited", "visited", False),
            ("deposited", "enrolled", False),
            ("enrolled", "active", False),
            ("enrolled", "deposited", True),
            ("enrolled", "visited", True),
            ("active", "enrolled", True),
            ("active", "deposited", True),
            ("active", "visited", True),
        ],
    )
    def test_destructive_mapping(self, frm, to, expected):
        assert is_destructive(frm, to) is expected
