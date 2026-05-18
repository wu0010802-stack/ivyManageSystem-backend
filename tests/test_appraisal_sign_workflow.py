"""sign_workflow.py 純函式測試（不接 DB）。"""

import pytest
from models.appraisal import SummaryStatus
from services.appraisal.sign_workflow import (
    can_advance,
    can_reject,
    default_reject_to_status,
    advance_target,
)


class TestCanAdvance:
    def test_draft_can_advance_to_supervisor_signed(self):
        assert can_advance(SummaryStatus.DRAFT, "SUPERVISOR") is True

    def test_finalized_cannot_advance(self):
        assert can_advance(SummaryStatus.FINALIZED, "SUPERVISOR") is False
        assert can_advance(SummaryStatus.FINALIZED, "ACCOUNTING") is False
        assert can_advance(SummaryStatus.FINALIZED, "FINALIZE") is False

    def test_must_match_current_stage(self):
        assert can_advance(SummaryStatus.SUPERVISOR_SIGNED, "ACCOUNTING") is True
        assert can_advance(SummaryStatus.SUPERVISOR_SIGNED, "FINALIZE") is False


class TestCanReject:
    def test_draft_cannot_reject(self):
        assert can_reject(SummaryStatus.DRAFT, SummaryStatus.DRAFT) is False

    def test_supervisor_signed_rejects_to_draft(self):
        assert can_reject(SummaryStatus.SUPERVISOR_SIGNED, SummaryStatus.DRAFT) is True

    def test_accounting_signed_can_reject_to_either(self):
        assert can_reject(SummaryStatus.ACCOUNTING_SIGNED, SummaryStatus.DRAFT) is True
        assert (
            can_reject(SummaryStatus.ACCOUNTING_SIGNED, SummaryStatus.SUPERVISOR_SIGNED)
            is True
        )

    def test_finalized_rejects_to_accounting(self):
        assert (
            can_reject(SummaryStatus.FINALIZED, SummaryStatus.ACCOUNTING_SIGNED) is True
        )
        assert can_reject(SummaryStatus.FINALIZED, SummaryStatus.DRAFT) is False


class TestDefaultRejectToStatus:
    def test_default_returns_one_stage_back(self):
        assert (
            default_reject_to_status(SummaryStatus.SUPERVISOR_SIGNED)
            == SummaryStatus.DRAFT
        )
        assert (
            default_reject_to_status(SummaryStatus.ACCOUNTING_SIGNED)
            == SummaryStatus.SUPERVISOR_SIGNED
        )
        assert (
            default_reject_to_status(SummaryStatus.FINALIZED)
            == SummaryStatus.ACCOUNTING_SIGNED
        )

    def test_draft_returns_none(self):
        assert default_reject_to_status(SummaryStatus.DRAFT) is None
