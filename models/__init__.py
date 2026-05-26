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

# 2026-05-26 起補登：models.fees 的 FeeTemplate 是 student_fee_records.template_id 的
# FK target，但既有 import 路徑（models.database / models.__init__）都沒帶到。
# bootstrap 跑 `StudentFeeRecord.__table__.create()` 時 PG 若沒先建 fee_templates 即
# FK 解析炸；CI Tests step `Base.metadata.create_all` 也需要這條 import 才會建 fee_templates。
from .fees import FeeTemplate, StudentFeeRecord  # noqa: F401
