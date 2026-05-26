"""Phase 3 lint coverage：Ruff DTZ 抓不到 model `default=datetime.now`
(callable reference 非 call expression)，用 reflection 補。

PR3 完成後 MODEL_DEFAULT_ALLOWLIST 應為 empty set；新增 model column
用 default=datetime.now 即測試紅。

Note: 以 __qualname__ 比對而非物件 identity，因為 datetime.now 在 Python 中
是 built-in method，各 model 模組 import 時捕捉的 function object 位址不同，
identity (is / in set) 比對會全部失敗。
"""

import pytest
from sqlalchemy import inspect

# 觸發所有 model import → 註冊到 Base.registry
# models/__init__.py 只 re-export 部分 model，必須逐一 import 才能讓
# Base.registry 收錄全部 mapper。
import models  # noqa: F401
import models.academic_term  # noqa: F401
import models.activity  # noqa: F401
import models.appraisal  # noqa: F401
import models.approval  # noqa: F401
import models.art_teacher_payroll  # noqa: F401
import models.attendance  # noqa: F401
import models.audit  # noqa: F401
import models.auth  # noqa: F401
import models.classroom  # noqa: F401
import models.config  # noqa: F401
import models.contact_book  # noqa: F401
import models.disciplinary  # noqa: F401
import models.dismissal  # noqa: F401
import models.employee  # noqa: F401
import models.event  # noqa: F401
import models.fees  # noqa: F401
import models.gov_moe  # noqa: F401
import models.guardian  # noqa: F401
import models.leave  # noqa: F401
import models.line_config  # noqa: F401
import models.monthly_fixed_cost  # noqa: F401
import models.notification_log  # noqa: F401
import models.offboarding  # noqa: F401
import models.overtime  # noqa: F401
import models.overtime_comp_leave_grant  # noqa: F401
import models.parent_binding  # noqa: F401
import models.parent_db  # noqa: F401
import models.parent_message  # noqa: F401
import models.parent_notification  # noqa: F401
import models.parent_refresh_token  # noqa: F401
import models.permission_models  # noqa: F401
import models.portfolio  # noqa: F401
import models.recruitment  # noqa: F401
import models.report_cache  # noqa: F401
import models.salary  # noqa: F401
import models.security  # noqa: F401
import models.shift  # noqa: F401
import models.student_leave  # noqa: F401
import models.student_log  # noqa: F401
import models.student_transfer  # noqa: F401
import models.unused_leave_payout_log  # noqa: F401
import models.vendor_payment  # noqa: F401
import models.year_end  # noqa: F401
from models.base import Base

# __qualname__ 比對，而非物件 identity：
# datetime.now 是 built-in method；各模組 import 後捕捉的 function object
# 與 from datetime import datetime 拿到的 datetime.now 位址不同。
FORBIDDEN_QUALNAMES = {"datetime.now", "datetime.utcnow"}


def _is_forbidden(arg) -> bool:
    """回傳 True 若 callable arg 的 __qualname__ 在禁止清單中。"""
    if not callable(arg):
        return False
    return getattr(arg, "__qualname__", None) in FORBIDDEN_QUALNAMES


# PR1 初始填入；PR3 逐處替換時同步移除；PR3 結束應為 empty
MODEL_DEFAULT_ALLOWLIST: set[tuple[str, str]] = {
    ("AcademicTerm", "created_at"),
    ("AcademicTerm", "updated_at"),
    ("ActivityAttendance", "created_at"),
    ("ActivityAttendance", "updated_at"),
    ("ActivityCourse", "created_at"),
    ("ActivityCourse", "updated_at"),
    ("ActivityPosDailyClose", "approved_at"),
    ("ActivityPosDailyClose", "created_at"),
    ("ActivityPosDailyClose", "updated_at"),
    ("ActivityPosDailyCloseHistory", "unlocked_at"),
    ("ActivityRegistration", "created_at"),
    ("ActivityRegistration", "updated_at"),
    ("ActivityRegistrationSettings", "updated_at"),
    ("ActivitySession", "created_at"),
    ("ActivitySupply", "created_at"),
    ("ActivitySupply", "updated_at"),
    ("Announcement", "created_at"),
    ("Announcement", "updated_at"),
    ("AnnouncementParentRead", "read_at"),
    ("AnnouncementRead", "read_at"),
    ("ApprovalLog", "created_at"),
    ("ApprovalPolicy", "updated_at"),
    ("ArtTeacherPayrollEntry", "created_at"),
    ("ArtTeacherPayrollEntry", "updated_at"),
    ("Attachment", "created_at"),
    ("Attendance", "created_at"),
    ("Attendance", "updated_at"),
    ("AttendancePolicy", "created_at"),
    ("AttendancePolicy", "updated_at"),
    ("AuditLog", "created_at"),
    ("BonusConfig", "created_at"),
    ("BonusConfig", "updated_at"),
    ("BonusSetting", "created_at"),
    ("BonusSetting", "updated_at"),
    ("BonusType", "created_at"),
    ("BonusType", "updated_at"),
    ("ClassBonusSetting", "created_at"),
    ("ClassGrade", "created_at"),
    ("ClassGrade", "updated_at"),
    ("Classroom", "created_at"),
    ("Classroom", "updated_at"),
    ("CompetitorSchool", "created_at"),
    ("CompetitorSchool", "updated_at"),
    ("ContactBookTemplate", "created_at"),
    ("ContactBookTemplate", "updated_at"),
    ("DailyShift", "created_at"),
    ("DailyShift", "updated_at"),
    ("DeductionRule", "created_at"),
    ("DeductionRule", "updated_at"),
    ("DeductionType", "created_at"),
    ("DeductionType", "updated_at"),
    ("DisciplinaryAction", "created_at"),
    ("DisciplinaryAction", "updated_at"),
    ("Employee", "created_at"),
    ("Employee", "updated_at"),
    ("EmployeeCertificate", "created_at"),
    ("EmployeeCertificate", "updated_at"),
    ("EmployeeContract", "created_at"),
    ("EmployeeContract", "updated_at"),
    ("EmployeeEducation", "created_at"),
    ("EmployeeEducation", "updated_at"),
    ("EnrollmentCertificate", "created_at"),
    ("EventAcknowledgment", "acknowledged_at"),
    ("FeeTemplate", "created_at"),
    ("FeeTemplate", "updated_at"),
    ("GradeTarget", "created_at"),
    ("GradeTarget", "updated_at"),
    ("Guardian", "created_at"),
    ("Guardian", "updated_at"),
    ("GuardianBindingCode", "created_at"),
    ("Holiday", "created_at"),
    ("Holiday", "updated_at"),
    ("InsuranceBracket", "updated_at"),
    ("InsuranceRate", "created_at"),
    ("InsuranceRate", "updated_at"),
    ("InsuranceTable", "created_at"),
    ("LeaveQuota", "created_at"),
    ("LeaveQuota", "updated_at"),
    ("LeaveRecord", "created_at"),
    ("LeaveRecord", "updated_at"),
    ("LineConfig", "updated_at"),
    ("LineReplyContext", "created_at"),
    ("LineReplyContext", "updated_at"),
    ("LineWebhookEvent", "created_at"),
    ("MeetingRecord", "created_at"),
    ("MeetingRecord", "updated_at"),
    ("MonthlyFixedCost", "created_at"),
    ("MonthlyFixedCost", "updated_at"),
    ("NotificationLog", "created_at"),
    ("OfficialCalendarSync", "created_at"),
    ("OfficialCalendarSync", "updated_at"),
    ("OvertimeCompLeaveGrant", "created_at"),
    ("OvertimeCompLeaveGrant", "updated_at"),
    ("OvertimeRecord", "created_at"),
    ("OvertimeRecord", "updated_at"),
    ("ParentCommunicationLog", "created_at"),
    ("ParentCommunicationLog", "updated_at"),
    ("ParentInquiry", "created_at"),
    ("ParentMessage", "created_at"),
    ("ParentMessageThread", "created_at"),
    ("ParentMessageThread", "updated_at"),
    ("ParentNotificationPreference", "created_at"),
    ("ParentNotificationPreference", "updated_at"),
    ("ParentRefreshToken", "created_at"),
    ("PunchCorrectionRequest", "created_at"),
    ("PunchCorrectionRequest", "updated_at"),
    ("RecruitmentAreaInsightCache", "created_at"),
    ("RecruitmentAreaInsightCache", "updated_at"),
    ("RecruitmentCampusSetting", "created_at"),
    ("RecruitmentCampusSetting", "updated_at"),
    ("RecruitmentEventLog", "created_at"),
    ("RecruitmentGeocodeCache", "created_at"),
    ("RecruitmentGeocodeCache", "updated_at"),
    ("RecruitmentIvykidsRecord", "created_at"),
    ("RecruitmentIvykidsRecord", "updated_at"),
    ("RecruitmentMonth", "created_at"),
    ("RecruitmentPeriod", "created_at"),
    ("RecruitmentPeriod", "updated_at"),
    ("RecruitmentSyncState", "created_at"),
    ("RecruitmentSyncState", "updated_at"),
    ("RecruitmentVisit", "created_at"),
    ("RecruitmentVisit", "updated_at"),
    ("RegistrationChange", "created_at"),
    ("RegistrationCourse", "created_at"),
    ("RegistrationSupply", "created_at"),
    ("ReportSnapshot", "computed_at"),
    ("ReportSnapshot", "created_at"),
    ("ReportSnapshot", "updated_at"),
    ("SalaryCalcJobRecord", "created_at"),
    ("SalaryItem", "created_at"),
    ("SalaryRecord", "created_at"),
    ("SalaryRecord", "updated_at"),
    ("SalarySnapshot", "captured_at"),
    ("SchoolEvent", "created_at"),
    ("SchoolEvent", "updated_at"),
    ("ShiftAssignment", "created_at"),
    ("ShiftAssignment", "updated_at"),
    ("ShiftSwapRequest", "created_at"),
    ("ShiftSwapRequest", "updated_at"),
    ("ShiftType", "created_at"),
    ("ShiftType", "updated_at"),
    ("SpecialEducationSubsidy", "created_at"),
    ("SpecialEducationSubsidy", "updated_at"),
    ("Student", "created_at"),
    ("Student", "updated_at"),
    ("StudentAllergy", "created_at"),
    ("StudentAllergy", "updated_at"),
    ("StudentAssessment", "created_at"),
    ("StudentAssessment", "updated_at"),
    ("StudentAttendance", "created_at"),
    ("StudentAttendance", "updated_at"),
    ("StudentChangeLog", "created_at"),
    ("StudentClassroomTransfer", "transferred_at"),
    ("StudentContactBookAck", "read_at"),
    ("StudentContactBookEntry", "created_at"),
    ("StudentContactBookEntry", "updated_at"),
    ("StudentContactBookReply", "created_at"),
    ("StudentDisabilityDocument", "created_at"),
    ("StudentDisabilityDocument", "updated_at"),
    ("StudentFeeAdjustment", "created_at"),
    ("StudentFeeAdjustment", "updated_at"),
    ("StudentFeePayment", "created_at"),
    ("StudentFeeRecord", "created_at"),
    ("StudentFeeRecord", "updated_at"),
    ("StudentFeeRefund", "refunded_at"),
    ("StudentIEPRecord", "created_at"),
    ("StudentIEPRecord", "updated_at"),
    ("StudentIncident", "created_at"),
    ("StudentIncident", "updated_at"),
    ("StudentLeaveRequest", "created_at"),
    ("StudentLeaveRequest", "updated_at"),
    ("StudentMedicationLog", "created_at"),
    ("StudentMedicationOrder", "created_at"),
    ("StudentMedicationOrder", "updated_at"),
    ("StudentObservation", "created_at"),
    ("StudentObservation", "updated_at"),
    ("SystemConfig", "created_at"),
    ("SystemConfig", "updated_at"),
    ("User", "created_at"),
    ("User", "updated_at"),
    ("VendorPayment", "created_at"),
    ("VendorPayment", "updated_at"),
    ("WorkdayOverride", "created_at"),
    ("WorkdayOverride", "updated_at"),
}
# Total: 184


def _collect_violations() -> list[tuple[str, str]]:
    """走訪所有 model column 找出 default / onupdate callable in FORBIDDEN_QUALNAMES.

    同時檢查 column.default 和 column.onupdate，因為
    onupdate=datetime.now 與 default=datetime.now 有同樣的 TZ 問題。
    """
    violations: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        for col in inspect(cls).columns:
            for attr in ("default", "onupdate"):
                cd = getattr(col, attr, None)
                if cd is None:
                    continue
                arg = getattr(cd, "arg", None)
                if _is_forbidden(arg):
                    key = (cls.__name__, col.name)
                    if key not in seen:
                        seen.add(key)
                        violations.append(key)
    return violations


def test_no_naive_datetime_in_model_defaults():
    violations = _collect_violations()
    unauthorized = [v for v in violations if v not in MODEL_DEFAULT_ALLOWLIST]
    assert not unauthorized, (
        "Model column default / onupdate 用了 datetime.now / utcnow，"
        "請改用 utils.taipei_time.now_taipei_naive():\n"
        + "\n".join(f"  - {cls}.{col}" for cls, col in unauthorized)
    )


@pytest.mark.skip(reason="canary: PR3 收尾解 skip，斷言 allow-list 已空")
def test_model_default_allowlist_is_empty():
    assert MODEL_DEFAULT_ALLOWLIST == set(), (
        "PR3 收尾必須把 MODEL_DEFAULT_ALLOWLIST 清空。"
        f"剩餘：{sorted(MODEL_DEFAULT_ALLOWLIST)}"
    )
