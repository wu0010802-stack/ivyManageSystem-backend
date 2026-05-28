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

# Re-export ApprovalStatus enum for callers (LeaveRecord/OvertimeRecord/
# PunchCorrectionRequest store status as a String(20) column directly;
# the P1/P2 dual-write listeners were removed once is_approved was dropped.
from .leave import LeaveRecord, LeaveQuota  # noqa: F401,E402
from .overtime import OvertimeRecord, PunchCorrectionRequest  # noqa: F401,E402
from .approval import ApprovalStatus  # noqa: F401,E402

# 2026-05-26 起補登：models.fees 的 FeeTemplate 是 student_fee_records.template_id 的
# FK target，但既有 import 路徑（models.database / models.__init__）都沒帶到。
# bootstrap 跑 `StudentFeeRecord.__table__.create()` 時 PG 若沒先建 fee_templates 即
# FK 解析炸；CI Tests step `Base.metadata.create_all` 也需要這條 import 才會建 fee_templates。
from .fees import FeeTemplate, StudentFeeRecord  # noqa: F401

# 2026-05-26 Phase D：UnusedLeavePayoutLog 是 overtime_comp_leave_grants.payout_log_id 的
# FK target；CI Tests step `Base.metadata.create_all` 需要該表先建立。
from .unused_leave_payout_log import UnusedLeavePayoutLog  # noqa: F401
from .overtime_comp_leave_grant import OvertimeCompLeaveGrant  # noqa: F401

# P0c-1 2026-05-28 法規/個資 sprint: consent + policy version 表
# CI Tests step `Base.metadata.create_all` 與 prod 啟動需中央 import 否則漏建表
from .consent import ParentConsentLog, PolicyVersion  # noqa: F401
