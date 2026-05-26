# Backend models __init__
from models.gov_moe import (
    StudentDisabilityDocument,
    StudentIEPRecord,
    SpecialEducationSubsidy,
    MonthlyEnrollmentSnapshot,
    EnrollmentCertificate,
)

from .appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalScoreItemCatalog,
    AppraisalSummary,
    AppraisalBonusRate,
    Semester as AppraisalSemester,
    CycleStatus as AppraisalCycleStatus,
    RoleGroup as AppraisalRoleGroup,
    Grade as AppraisalGrade,
    SummaryStatus as AppraisalSummaryStatus,
    ScoreItemSign as AppraisalScoreItemSign,
)

from .year_end import (
    YearEndCycle,
    OrgYearSettings,
    ClassEnrollmentTarget,
    EmployeeYearEndSnapshot,
    YearEndSettlement,
    SpecialBonusItem,
    YearEndCycleStatus,
    YearEndSettlementStatus,
    SpecialBonusType,
)


# --- P1 approval-status dual-write listeners --------------------------------
# IMPORTANT: must be at end of file — model classes must be loaded before
# event.listens_for() runs. Direction: is_approved (set) → status (mirror).
# Reversed in P2 PR; removed in P4 PR.
from .leave import LeaveRecord, LeaveQuota  # noqa: F401,E402
from .overtime import OvertimeRecord, PunchCorrectionRequest  # noqa: F401,E402
from .approval import ApprovalStatus, register_p1_listeners  # noqa: F401,E402

register_p1_listeners(LeaveRecord, OvertimeRecord, PunchCorrectionRequest)
