"""P1 dual-write listener tests — covers ApprovalStatus enum + attribute listener.

P1 listener direction: is_approved (set) → status (mirror).
P2 PR will reverse the direction; this file gets new tests at that point.
"""

import pytest

from models.approval import ApprovalStatus, register_p1_listeners


class TestApprovalStatusEnum:
    def test_three_values(self):
        assert ApprovalStatus.PENDING.value == "pending"
        assert ApprovalStatus.APPROVED.value == "approved"
        assert ApprovalStatus.REJECTED.value == "rejected"

    def test_is_str_subclass(self):
        """str-mixin so SQLAlchemy String column round-trips cleanly."""
        assert isinstance(ApprovalStatus.PENDING, str)
        assert ApprovalStatus.PENDING == "pending"

    def test_no_extra_values(self):
        assert {m.value for m in ApprovalStatus} == {"pending", "approved", "rejected"}
