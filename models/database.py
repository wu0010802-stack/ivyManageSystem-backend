"""
models/database.py — 向下相容 re-export hub

所有既有 import 皆無需修改。
新程式碼建議直接 from models.<domain> import <Model>。
"""

from models.base import (
    Base, get_session, session_scope, get_engine,
    get_session_factory, init_database,
)
from models.employee import Employee, JobTitle, EmployeeType
from models.classroom import Classroom, ClassGrade, Student
from models.attendance import Attendance, AttendanceStatus
from models.shift import ShiftType, ShiftAssignment, DailyShift, ShiftSwapRequest
from models.leave import LeaveRecord, LeaveQuota, LeaveType
from models.overtime import OvertimeRecord, PunchCorrectionRequest
from models.salary import (
    SalaryRecord, SalaryItem, EmployeeAllowance,
    AllowanceType, DeductionType, BonusType,
    DeductionRule, InsuranceTable,
    BonusSetting, ClassBonusSetting,
)
from models.config import (
    AttendancePolicy, BonusConfig, GradeTarget, InsuranceRate,
    SystemConfig, PositionSalaryConfig,
)
from models.event import (
    Holiday, MeetingRecord, SchoolEvent, Announcement, AnnouncementRead,
)
from models.auth import User
from models.audit import AuditLog
from models.approval import ApprovalPolicy, ApprovalLog

__all__ = [
    # base
    "Base", "get_session", "session_scope", "get_engine",
    "get_session_factory", "init_database",
    # employee
    "Employee", "JobTitle", "EmployeeType",
    # classroom
    "Classroom", "ClassGrade", "Student",
    # attendance
    "Attendance", "AttendanceStatus",
    # shift
    "ShiftType", "ShiftAssignment", "DailyShift", "ShiftSwapRequest",
    # leave
    "LeaveRecord", "LeaveQuota", "LeaveType",
    # overtime
    "OvertimeRecord", "PunchCorrectionRequest",
    # salary
    "SalaryRecord", "SalaryItem", "EmployeeAllowance",
    "AllowanceType", "DeductionType", "BonusType",
    "DeductionRule", "InsuranceTable",
    "BonusSetting", "ClassBonusSetting",
    # config
    "AttendancePolicy", "BonusConfig", "GradeTarget", "InsuranceRate",
    "SystemConfig", "PositionSalaryConfig",
    # event
    "Holiday", "MeetingRecord", "SchoolEvent", "Announcement", "AnnouncementRead",
    # auth
    "User",
    # audit
    "AuditLog",
    # approval
    "ApprovalPolicy", "ApprovalLog",
]
