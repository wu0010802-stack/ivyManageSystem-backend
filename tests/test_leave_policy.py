"""請假規則 helper 測試。"""

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.leave_policy import (
    get_requested_calendar_days,
    requires_supporting_document,
    validate_portal_leave_rules,
)


class TestLeavePolicyHelpers:
    def test_requested_calendar_days_is_inclusive(self):
        assert get_requested_calendar_days(date(2026, 3, 1), date(2026, 3, 3)) == 3

    def test_supporting_document_required_only_when_more_than_two_days(self):
        assert requires_supporting_document(date(2026, 3, 1), date(2026, 3, 2)) is False
        assert requires_supporting_document(date(2026, 3, 1), date(2026, 3, 3)) is True

    def test_personal_leave_must_be_requested_two_days_in_advance(self):
        with pytest.raises(ValueError) as exc_info:
            validate_portal_leave_rules(
                "personal",
                date(2026, 3, 12),
                date(2026, 3, 12),
                1,
                today=date(2026, 3, 11),
            )
        assert "提前 2 日" in str(exc_info.value)

    def test_sick_leave_must_be_in_four_hour_blocks(self):
        with pytest.raises(ValueError) as exc_info:
            validate_portal_leave_rules(
                "sick",
                date(2026, 3, 12),
                date(2026, 3, 12),
                6,
                today=date(2026, 3, 1),
            )
        assert "4 小時" in str(exc_info.value)

    def test_valid_rules_pass(self):
        validate_portal_leave_rules(
            "sick",
            date(2026, 3, 12),
            date(2026, 3, 12),
            8,
            today=date(2026, 3, 1),
        )


class TestLeavePolicyInputValidation:
    """eval framework 揭露:hours=-4 與 hours=0 因 `-4 % 4 == 0` 與 `0 % 4 == 0`
    通過 sick 規則檢查;雖 API layer 已擋 < 0.5,helper 自身仍應防禦深度。
    """

    def test_sick_hours_zero_raises(self):
        with pytest.raises(ValueError, match="必須.*大於.*0|時數.*必須.*正"):
            validate_portal_leave_rules(
                "sick",
                date(2026, 3, 12),
                date(2026, 3, 12),
                0,
                today=date(2026, 3, 1),
            )

    def test_sick_hours_negative_raises(self):
        """`-4 % 4 == 0` 在 Python 為 True,但負時數無業務意義"""
        with pytest.raises(ValueError, match="必須.*大於.*0|時數.*必須.*正"):
            validate_portal_leave_rules(
                "sick",
                date(2026, 3, 12),
                date(2026, 3, 12),
                -4,
                today=date(2026, 3, 1),
            )

    def test_personal_hours_zero_raises(self):
        """事假同樣不應允許 0 時數"""
        with pytest.raises(ValueError, match="必須.*大於.*0|時數.*必須.*正"):
            validate_portal_leave_rules(
                "personal",
                date(2026, 3, 15),
                date(2026, 3, 15),
                0,
                today=date(2026, 3, 1),
            )
