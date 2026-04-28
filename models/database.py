"""
models/database.py — 向下相容 re-export hub

所有既有 import 皆無需修改。
新程式碼建議直接 from models.<domain> import <Model>。
"""

from models.base import (
    Base,
    get_session,
    session_scope,
    get_engine,
    get_session_factory,
    init_database,
)
from models.employee import (
    Employee,
    JobTitle,
    EmployeeType,
    EmployeeEducation,
    EmployeeCertificate,
    EmployeeContract,
)
from models.classroom import (
    Classroom,
    ClassGrade,
    Student,
    StudentAttendance,
    StudentIncident,
    StudentAssessment,
)
from models.guardian import Guardian
from models.parent_binding import GuardianBindingCode
from models.student_leave import StudentLeaveRequest
from models.attendance import Attendance, AttendanceStatus
from models.shift import ShiftType, ShiftAssignment, DailyShift, ShiftSwapRequest
from models.leave import LeaveRecord, LeaveQuota, LeaveType
from models.overtime import OvertimeRecord, PunchCorrectionRequest
from models.salary import (
    SalaryRecord,
    SalaryItem,
    SalarySnapshot,
    SalaryCalcJobRecord,
    DeductionType,
    BonusType,
    DeductionRule,
    InsuranceTable,
    BonusSetting,
    ClassBonusSetting,
)
from models.config import (
    AttendancePolicy,
    BonusConfig,
    GradeTarget,
    InsuranceRate,
    SystemConfig,
    PositionSalaryConfig,
)
from models.event import (
    Holiday,
    WorkdayOverride,
    OfficialCalendarSync,
    MeetingRecord,
    SchoolEvent,
    Announcement,
    AnnouncementRead,
    AnnouncementRecipient,
    AnnouncementParentRecipient,
    AnnouncementParentRead,
    EventAcknowledgment,
)
from models.auth import User
from models.audit import AuditLog
from models.approval import ApprovalPolicy, ApprovalLog
from models.line_config import LineConfig
from models.report_cache import ReportSnapshot
from models.activity import (
    ActivityCourse,
    ActivitySupply,
    ActivityRegistration,
    RegistrationCourse,
    RegistrationSupply,
    ParentInquiry,
    RegistrationChange,
    ActivityRegistrationSettings,
    ActivityPaymentRecord,
    ActivitySession,
    ActivityAttendance,
    ActivityPosDailyClose,
)
from models.dismissal import StudentDismissalCall
from models.student_transfer import StudentClassroomTransfer
from models.recruitment import (
    RecruitmentVisit,
    RecruitmentIvykidsRecord,
    RecruitmentMonth,
    RecruitmentPeriod,
    RecruitmentGeocodeCache,
    RecruitmentCampusSetting,
    RecruitmentAreaInsightCache,
    RecruitmentSyncState,
)
from models.portfolio import (
    Attachment,
    StudentObservation,
    StudentAllergy,
    StudentMedicationOrder,
    StudentMedicationLog,
)
from models.security import JwtBlocklist, RateLimitBucket
from models.parent_message import (
    LineReplyContext,
    LineWebhookEvent,
    ParentMessage,
    ParentMessageThread,
)
from models.parent_notification import ParentNotificationPreference

__all__ = [
    # base
    "Base",
    "get_session",
    "session_scope",
    "get_engine",
    "get_session_factory",
    "init_database",
    # employee
    "Employee",
    "JobTitle",
    "EmployeeType",
    "EmployeeEducation",
    "EmployeeCertificate",
    "EmployeeContract",
    # classroom
    "Classroom",
    "ClassGrade",
    "Student",
    "StudentIncident",
    "StudentAssessment",
    # guardian
    "Guardian",
    "GuardianBindingCode",
    "StudentLeaveRequest",
    # attendance
    "Attendance",
    "AttendanceStatus",
    # shift
    "ShiftType",
    "ShiftAssignment",
    "DailyShift",
    "ShiftSwapRequest",
    # leave
    "LeaveRecord",
    "LeaveQuota",
    "LeaveType",
    # overtime
    "OvertimeRecord",
    "PunchCorrectionRequest",
    # salary
    "SalaryRecord",
    "SalaryItem",
    "SalarySnapshot",
    "DeductionType",
    "BonusType",
    "DeductionRule",
    "InsuranceTable",
    "BonusSetting",
    "ClassBonusSetting",
    # config
    "AttendancePolicy",
    "BonusConfig",
    "GradeTarget",
    "InsuranceRate",
    "SystemConfig",
    "PositionSalaryConfig",
    # event
    "Holiday",
    "WorkdayOverride",
    "OfficialCalendarSync",
    "MeetingRecord",
    "SchoolEvent",
    "Announcement",
    "AnnouncementRead",
    "AnnouncementRecipient",
    "AnnouncementParentRecipient",
    "AnnouncementParentRead",
    "EventAcknowledgment",
    # auth
    "User",
    # audit
    "AuditLog",
    # approval
    "ApprovalPolicy",
    "ApprovalLog",
    # line
    "LineConfig",
    # report cache
    "ReportSnapshot",
    # activity
    "ActivityCourse",
    "ActivitySupply",
    "ActivityRegistration",
    "RegistrationCourse",
    "RegistrationSupply",
    "ParentInquiry",
    "RegistrationChange",
    "ActivityRegistrationSettings",
    "ActivityPaymentRecord",
    "ActivitySession",
    "ActivityAttendance",
    "ActivityPosDailyClose",
    # dismissal
    "StudentDismissalCall",
    # student transfer history
    "StudentClassroomTransfer",
    # recruitment
    "RecruitmentVisit",
    "RecruitmentIvykidsRecord",
    "RecruitmentMonth",
    "RecruitmentPeriod",
    "RecruitmentGeocodeCache",
    "RecruitmentCampusSetting",
    "RecruitmentAreaInsightCache",
    "RecruitmentSyncState",
    # portfolio
    "Attachment",
    "StudentObservation",
    "StudentAllergy",
    "StudentMedicationOrder",
    "StudentMedicationLog",
    # security support tables
    "RateLimitBucket",
    "JwtBlocklist",
    # parent communication (Phase 3)
    "ParentMessageThread",
    "ParentMessage",
    "LineWebhookEvent",
    # parent notification preferences (Phase 6)
    "ParentNotificationPreference",
    # LINE webhook reply context (Phase 5)
    "LineReplyContext",
]
