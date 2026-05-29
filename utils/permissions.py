"""
Permission definitions for fine-grained access control
（text[] 版本，2026-05-21 重構：脫離 64-bit IntFlag 容量限制）
"""

from enum import Enum
from typing import List, Dict

WILDCARD = "*"


class Permission(str, Enum):
    """權限識別字串（繼承 str：perm.value == "EMPLOYEES_READ"）。

    位元值已搬到 LEGACY_PERMISSION_BITS，僅 alembic migration 使用，
    runtime 不參考。
    """

    DASHBOARD = "DASHBOARD"
    APPROVALS = "APPROVALS"
    CALENDAR = "CALENDAR"
    SCHEDULE = "SCHEDULE"
    MEETINGS = "MEETINGS"
    REPORTS = "REPORTS"
    AUDIT_LOGS = "AUDIT_LOGS"

    ATTENDANCE_READ = "ATTENDANCE_READ"
    ATTENDANCE_WRITE = "ATTENDANCE_WRITE"
    LEAVES_READ = "LEAVES_READ"
    LEAVES_WRITE = "LEAVES_WRITE"
    OVERTIME_READ = "OVERTIME_READ"
    OVERTIME_WRITE = "OVERTIME_WRITE"
    EMPLOYEES_READ = "EMPLOYEES_READ"
    EMPLOYEES_WRITE = "EMPLOYEES_WRITE"
    STUDENTS_READ = "STUDENTS_READ"
    STUDENTS_WRITE = "STUDENTS_WRITE"
    CLASSROOMS_READ = "CLASSROOMS_READ"
    CLASSROOMS_WRITE = "CLASSROOMS_WRITE"
    SALARY_READ = "SALARY_READ"
    SALARY_WRITE = "SALARY_WRITE"
    ANNOUNCEMENTS_READ = "ANNOUNCEMENTS_READ"
    ANNOUNCEMENTS_WRITE = "ANNOUNCEMENTS_WRITE"
    SETTINGS_READ = "SETTINGS_READ"
    SETTINGS_WRITE = "SETTINGS_WRITE"
    USER_MANAGEMENT_READ = "USER_MANAGEMENT_READ"
    USER_MANAGEMENT_WRITE = "USER_MANAGEMENT_WRITE"
    ACTIVITY_READ = "ACTIVITY_READ"
    ACTIVITY_WRITE = "ACTIVITY_WRITE"
    DISMISSAL_CALLS_READ = "DISMISSAL_CALLS_READ"
    DISMISSAL_CALLS_WRITE = "DISMISSAL_CALLS_WRITE"
    FEES_READ = "FEES_READ"
    FEES_WRITE = "FEES_WRITE"
    RECRUITMENT_READ = "RECRUITMENT_READ"
    RECRUITMENT_WRITE = "RECRUITMENT_WRITE"
    ACTIVITY_PAYMENT_APPROVE = "ACTIVITY_PAYMENT_APPROVE"

    STUDENTS_LIFECYCLE_WRITE = "STUDENTS_LIFECYCLE_WRITE"
    GUARDIANS_READ = "GUARDIANS_READ"
    GUARDIANS_WRITE = "GUARDIANS_WRITE"
    RECRUITMENT_CONVERT = "RECRUITMENT_CONVERT"
    BUSINESS_ANALYTICS = "BUSINESS_ANALYTICS"

    PORTFOLIO_READ = "PORTFOLIO_READ"
    PORTFOLIO_WRITE = "PORTFOLIO_WRITE"
    PORTFOLIO_PUBLISH = "PORTFOLIO_PUBLISH"
    STUDENTS_HEALTH_READ = "STUDENTS_HEALTH_READ"
    STUDENTS_HEALTH_WRITE = "STUDENTS_HEALTH_WRITE"
    STUDENTS_MEDICATION_ADMINISTER = "STUDENTS_MEDICATION_ADMINISTER"
    STUDENTS_SPECIAL_NEEDS_READ = "STUDENTS_SPECIAL_NEEDS_READ"
    STUDENTS_SPECIAL_NEEDS_WRITE = "STUDENTS_SPECIAL_NEEDS_WRITE"

    PARENT_MESSAGES_WRITE = "PARENT_MESSAGES_WRITE"

    GOV_REPORTS_VIEW = "GOV_REPORTS_VIEW"
    GOV_REPORTS_EXPORT = "GOV_REPORTS_EXPORT"

    APPRAISAL_READ = "APPRAISAL_READ"
    APPRAISAL_EVENT_WRITE = "APPRAISAL_EVENT_WRITE"
    APPRAISAL_REVIEW = "APPRAISAL_REVIEW"
    APPRAISAL_ACCOUNTING = "APPRAISAL_ACCOUNTING"
    APPRAISAL_FINALIZE = "APPRAISAL_FINALIZE"
    APPRAISAL_RULE_WRITE = "APPRAISAL_RULE_WRITE"

    YEAR_END_READ = "YEAR_END_READ"
    YEAR_END_WRITE = "YEAR_END_WRITE"
    YEAR_END_FINALIZE = "YEAR_END_FINALIZE"

    VENDOR_PAYMENT_READ = "VENDOR_PAYMENT_READ"
    VENDOR_PAYMENT_WRITE = "VENDOR_PAYMENT_WRITE"

    # DB-driven 自訂權限/角色 CRUD 守衛（(b) 子專案）
    ROLES_MANAGE = "ROLES_MANAGE"


# 位元值凍結快照——僅供 alembic upgrade()/downgrade() backfill 使用。
# 一旦 migration 跑過 prod，請勿變更此表（保持歷史 migration 可重跑）。
LEGACY_PERMISSION_BITS: Dict[str, int] = {
    "DASHBOARD": 1 << 0,
    "APPROVALS": 1 << 1,
    "CALENDAR": 1 << 2,
    "SCHEDULE": 1 << 3,
    "ATTENDANCE_READ": 1 << 4,
    "LEAVES_READ": 1 << 5,
    "OVERTIME_READ": 1 << 6,
    "MEETINGS": 1 << 7,
    "EMPLOYEES_READ": 1 << 8,
    "STUDENTS_READ": 1 << 9,
    "CLASSROOMS_READ": 1 << 10,
    "SALARY_READ": 1 << 11,
    "ANNOUNCEMENTS_READ": 1 << 12,
    "REPORTS": 1 << 13,
    "AUDIT_LOGS": 1 << 14,
    "SETTINGS_READ": 1 << 15,
    "USER_MANAGEMENT_READ": 1 << 16,
    "ATTENDANCE_WRITE": 1 << 17,
    "LEAVES_WRITE": 1 << 18,
    "OVERTIME_WRITE": 1 << 19,
    "EMPLOYEES_WRITE": 1 << 20,
    "STUDENTS_WRITE": 1 << 21,
    "CLASSROOMS_WRITE": 1 << 22,
    "SALARY_WRITE": 1 << 23,
    "ANNOUNCEMENTS_WRITE": 1 << 24,
    "SETTINGS_WRITE": 1 << 25,
    "USER_MANAGEMENT_WRITE": 1 << 26,
    "ACTIVITY_READ": 1 << 27,
    "ACTIVITY_WRITE": 1 << 28,
    "DISMISSAL_CALLS_READ": 1 << 29,
    "DISMISSAL_CALLS_WRITE": 1 << 30,
    "FEES_READ": 1 << 31,
    "FEES_WRITE": 1 << 32,
    "RECRUITMENT_READ": 1 << 33,
    "RECRUITMENT_WRITE": 1 << 34,
    "ACTIVITY_PAYMENT_APPROVE": 1 << 35,
    "STUDENTS_LIFECYCLE_WRITE": 1 << 36,
    "GUARDIANS_READ": 1 << 37,
    "GUARDIANS_WRITE": 1 << 38,
    "RECRUITMENT_CONVERT": 1 << 39,
    "BUSINESS_ANALYTICS": 1 << 40,
    "PORTFOLIO_READ": 1 << 41,
    "PORTFOLIO_WRITE": 1 << 42,
    "PORTFOLIO_PUBLISH": 1 << 43,
    "STUDENTS_HEALTH_READ": 1 << 44,
    "STUDENTS_HEALTH_WRITE": 1 << 45,
    "STUDENTS_MEDICATION_ADMINISTER": 1 << 46,
    "STUDENTS_SPECIAL_NEEDS_READ": 1 << 47,
    "STUDENTS_SPECIAL_NEEDS_WRITE": 1 << 48,
    "PARENT_MESSAGES_WRITE": 1 << 49,
    "GOV_REPORTS_VIEW": 1 << 50,
    "GOV_REPORTS_EXPORT": 1 << 51,
    "YEAR_END_READ": 1 << 52,
    "APPRAISAL_RULE_WRITE": 1 << 53,
    "VENDOR_PAYMENT_READ": 1 << 54,
    "APPRAISAL_READ": 1 << 55,
    "APPRAISAL_EVENT_WRITE": 1 << 56,
    "APPRAISAL_REVIEW": 1 << 57,
    "APPRAISAL_ACCOUNTING": 1 << 58,
    "APPRAISAL_FINALIZE": 1 << 59,
    "YEAR_END_WRITE": 1 << 60,
    "YEAR_END_FINALIZE": 1 << 61,
    "VENDOR_PAYMENT_WRITE": 1 << 62,
}


# ---------------------------------------------------------------------------
# 讀寫配對映射（模組基礎名稱 → read/write 權限名稱）
# ---------------------------------------------------------------------------

SPLIT_MODULES: Dict[str, Dict[str, str]] = {
    "ATTENDANCE": {"read": "ATTENDANCE_READ", "write": "ATTENDANCE_WRITE"},
    "LEAVES": {"read": "LEAVES_READ", "write": "LEAVES_WRITE"},
    "OVERTIME": {"read": "OVERTIME_READ", "write": "OVERTIME_WRITE"},
    "EMPLOYEES": {"read": "EMPLOYEES_READ", "write": "EMPLOYEES_WRITE"},
    "STUDENTS": {"read": "STUDENTS_READ", "write": "STUDENTS_WRITE"},
    "CLASSROOMS": {"read": "CLASSROOMS_READ", "write": "CLASSROOMS_WRITE"},
    "SALARY": {"read": "SALARY_READ", "write": "SALARY_WRITE"},
    "ANNOUNCEMENTS": {"read": "ANNOUNCEMENTS_READ", "write": "ANNOUNCEMENTS_WRITE"},
    "SETTINGS": {"read": "SETTINGS_READ", "write": "SETTINGS_WRITE"},
    "USER_MANAGEMENT": {
        "read": "USER_MANAGEMENT_READ",
        "write": "USER_MANAGEMENT_WRITE",
    },
    "ACTIVITY": {"read": "ACTIVITY_READ", "write": "ACTIVITY_WRITE"},
    "DISMISSAL_CALLS": {
        "read": "DISMISSAL_CALLS_READ",
        "write": "DISMISSAL_CALLS_WRITE",
    },
    "FEES": {"read": "FEES_READ", "write": "FEES_WRITE"},
    "RECRUITMENT": {"read": "RECRUITMENT_READ", "write": "RECRUITMENT_WRITE"},
    "GUARDIANS": {"read": "GUARDIANS_READ", "write": "GUARDIANS_WRITE"},
    "APPRAISAL": {"read": "APPRAISAL_READ", "write": "APPRAISAL_EVENT_WRITE"},
    "YEAR_END": {"read": "YEAR_END_READ", "write": "YEAR_END_WRITE"},
    "VENDOR_PAYMENT": {
        "read": "VENDOR_PAYMENT_READ",
        "write": "VENDOR_PAYMENT_WRITE",
    },
}


# ---------------------------------------------------------------------------
# RBAC 角色模板（text[] 版本：list[str]，admin 為 ["*"] wildcard）
# ---------------------------------------------------------------------------

ROLE_TEMPLATES: Dict[str, List[str]] = {
    "admin": [WILDCARD],
    "hr": [
        Permission.DASHBOARD.value,
        Permission.EMPLOYEES_READ.value,
        Permission.EMPLOYEES_WRITE.value,
        Permission.SALARY_READ.value,
        Permission.SALARY_WRITE.value,
        Permission.ATTENDANCE_READ.value,
        Permission.ATTENDANCE_WRITE.value,
        Permission.LEAVES_READ.value,
        Permission.LEAVES_WRITE.value,
        Permission.OVERTIME_READ.value,
        Permission.OVERTIME_WRITE.value,
        Permission.REPORTS.value,
        Permission.GOV_REPORTS_VIEW.value,
        Permission.GOV_REPORTS_EXPORT.value,
        # 教職員考核：人事/會計（核數字）
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
        Permission.APPRAISAL_ACCOUNTING.value,
        # 年終獎金：人事可檢視與編輯（會計核數字流程）
        Permission.YEAR_END_READ.value,
        Permission.YEAR_END_WRITE.value,
        # 廠商付款：HR 兼採購行政
        Permission.VENDOR_PAYMENT_READ.value,
        Permission.VENDOR_PAYMENT_WRITE.value,
    ],
    "supervisor": [
        Permission.DASHBOARD.value,
        Permission.APPROVALS.value,
        Permission.CALENDAR.value,
        Permission.SCHEDULE.value,
        Permission.ATTENDANCE_READ.value,
        Permission.ATTENDANCE_WRITE.value,
        Permission.LEAVES_READ.value,
        Permission.LEAVES_WRITE.value,
        Permission.OVERTIME_READ.value,
        Permission.OVERTIME_WRITE.value,
        Permission.MEETINGS.value,
        Permission.STUDENTS_READ.value,
        Permission.STUDENTS_WRITE.value,
        Permission.STUDENTS_LIFECYCLE_WRITE.value,
        Permission.GUARDIANS_READ.value,
        Permission.GUARDIANS_WRITE.value,
        Permission.CLASSROOMS_READ.value,
        Permission.CLASSROOMS_WRITE.value,
        Permission.FEES_READ.value,
        Permission.FEES_WRITE.value,
        Permission.RECRUITMENT_READ.value,
        Permission.RECRUITMENT_WRITE.value,
        Permission.RECRUITMENT_CONVERT.value,
        Permission.BUSINESS_ANALYTICS.value,
        Permission.REPORTS.value,
        # Portfolio (supervisor 含發佈與健康編輯權限)
        Permission.PORTFOLIO_READ.value,
        Permission.PORTFOLIO_WRITE.value,
        Permission.PORTFOLIO_PUBLISH.value,
        Permission.STUDENTS_HEALTH_READ.value,
        Permission.STUDENTS_HEALTH_WRITE.value,
        Permission.STUDENTS_MEDICATION_ADMINISTER.value,
        Permission.STUDENTS_SPECIAL_NEEDS_READ.value,
        Permission.STUDENTS_SPECIAL_NEEDS_WRITE.value,
        # 家園溝通平台
        Permission.PARENT_MESSAGES_WRITE.value,
        # 教育部申報模組：主管可檢視（不可匯出）
        Permission.GOV_REPORTS_VIEW.value,
        # 教職員考核：主管全程權限（評分+簽核+核定）
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
        Permission.APPRAISAL_REVIEW.value,
        Permission.APPRAISAL_FINALIZE.value,
        Permission.APPRAISAL_RULE_WRITE.value,
        # 年終獎金：主管全程權限
        Permission.YEAR_END_READ.value,
        Permission.YEAR_END_WRITE.value,
        Permission.YEAR_END_FINALIZE.value,
        # 廠商付款：主管全程權限
        Permission.VENDOR_PAYMENT_READ.value,
        Permission.VENDOR_PAYMENT_WRITE.value,
    ],
    "teacher": [
        Permission.DASHBOARD.value,
        Permission.CALENDAR.value,
        Permission.ANNOUNCEMENTS_READ.value,
        Permission.DISMISSAL_CALLS_READ.value,
        Permission.DISMISSAL_CALLS_WRITE.value,
        Permission.PORTFOLIO_READ.value,
        Permission.PORTFOLIO_WRITE.value,
        Permission.STUDENTS_HEALTH_READ.value,
        Permission.STUDENTS_MEDICATION_ADMINISTER.value,
        Permission.STUDENTS_SPECIAL_NEEDS_READ.value,
        Permission.PARENT_MESSAGES_WRITE.value,
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
    ],
    # 家長角色：恆無任何 Permission；資源存取一律由 user_id → guardians 過濾
    "parent": [],
}


# 角色名稱對照表
ROLE_LABELS: Dict[str, str] = {
    "admin": "系統管理員",
    "hr": "人事管理員",
    "supervisor": "主管",
    "teacher": "教師",
    "parent": "家長",
}

# principal 角色：supervisor 全部 + 薪資審視 + 稽核 + 政府報表匯出
ROLE_TEMPLATES["principal"] = ROLE_TEMPLATES["supervisor"] + [
    Permission.SALARY_READ.value,
    Permission.AUDIT_LOGS.value,
    Permission.GOV_REPORTS_EXPORT.value,
]
ROLE_LABELS["principal"] = "園長"

# accountant 角色：純財務，13 條，不含 EMPLOYEES_WRITE
ROLE_TEMPLATES["accountant"] = [
    Permission.DASHBOARD.value,
    Permission.REPORTS.value,
    Permission.GOV_REPORTS_VIEW.value,
    Permission.EMPLOYEES_READ.value,  # 要看誰可申報薪資（不含 WRITE）
    Permission.SALARY_READ.value,
    Permission.SALARY_WRITE.value,
    Permission.FEES_READ.value,
    Permission.FEES_WRITE.value,
    Permission.VENDOR_PAYMENT_READ.value,
    Permission.VENDOR_PAYMENT_WRITE.value,
    Permission.YEAR_END_READ.value,
    Permission.YEAR_END_WRITE.value,  # 不含 FINALIZE（簽核屬 supervisor/principal）
    Permission.APPRAISAL_ACCOUNTING.value,  # 核考核獎金數字
]
ROLE_LABELS["accountant"] = "會計"

# 角色說明（給前端 SettingsUsersTab 卡片顯示）
ROLE_DESCRIPTIONS: Dict[str, str] = {
    "admin": "唯一能改帳號、系統設定",
    "principal": "業務全包 + 薪資審視，不動帳號",
    "supervisor": "教務管理、招生轉換、考核全程",
    "hr": "員工資料、薪資發放、年終、廠商付款",
    "accountant": "純財務（薪資/學費/廠商/年終）",
    "teacher": "公告、考勤、放學接送、學生檔案",
    "parent": "家長端登入，無管理端權限",
}


# 權限名稱對照表 (供前端使用)
PERMISSION_LABELS: Dict[str, str] = {
    # 不拆分的模組
    "DASHBOARD": "儀表板",
    "APPROVALS": "審核工作台",
    "CALENDAR": "行事曆",
    "SCHEDULE": "排班管理",
    "MEETINGS": "園務會議",
    "REPORTS": "報表統計",
    "AUDIT_LOGS": "操作紀錄",
    # 讀寫分離模組
    "ATTENDANCE_READ": "出勤管理 (檢視)",
    "ATTENDANCE_WRITE": "出勤管理 (編輯)",
    "LEAVES_READ": "請假管理 (檢視)",
    "LEAVES_WRITE": "請假管理 (編輯)",
    "OVERTIME_READ": "加班管理 (檢視)",
    "OVERTIME_WRITE": "加班管理 (編輯)",
    "EMPLOYEES_READ": "員工管理 (檢視)",
    "EMPLOYEES_WRITE": "員工管理 (編輯)",
    "STUDENTS_READ": "學生管理 (檢視)",
    "STUDENTS_WRITE": "學生管理 (編輯)",
    "CLASSROOMS_READ": "班級管理 (檢視)",
    "CLASSROOMS_WRITE": "班級管理 (編輯)",
    "SALARY_READ": "薪資管理 (檢視)",
    "SALARY_WRITE": "薪資管理 (編輯)",
    "ANNOUNCEMENTS_READ": "公告管理 (檢視)",
    "ANNOUNCEMENTS_WRITE": "公告管理 (編輯)",
    "SETTINGS_READ": "系統設定 (檢視)",
    "SETTINGS_WRITE": "系統設定 (編輯)",
    "USER_MANAGEMENT_READ": "帳號管理 (檢視)",
    "USER_MANAGEMENT_WRITE": "帳號管理 (編輯)",
    "ACTIVITY_READ": "課後才藝 (檢視)",
    "ACTIVITY_WRITE": "課後才藝 (編輯)",
    "DISMISSAL_CALLS_READ": "接送通知 (檢視)",
    "DISMISSAL_CALLS_WRITE": "接送通知 (操作)",
    "FEES_READ": "學費管理 (檢視)",
    "FEES_WRITE": "學費管理 (編輯)",
    "RECRUITMENT_READ": "招生統計 (檢視)",
    "RECRUITMENT_WRITE": "招生統計 (編輯)",
    "ACTIVITY_PAYMENT_APPROVE": "才藝課收款簽核",
    "STUDENTS_LIFECYCLE_WRITE": "學生生命週期 (狀態轉移)",
    "GUARDIANS_READ": "監護人資料 (檢視)",
    "GUARDIANS_WRITE": "監護人資料 (編輯)",
    "RECRUITMENT_CONVERT": "招生轉化為學生",
    "BUSINESS_ANALYTICS": "經營分析",
    # Portfolio
    "PORTFOLIO_READ": "成長歷程 (檢視)",
    "PORTFOLIO_WRITE": "成長歷程 (編輯)",
    "PORTFOLIO_PUBLISH": "學期報告 (發佈)",
    "STUDENTS_HEALTH_READ": "健康資訊 (檢視)",
    "STUDENTS_HEALTH_WRITE": "健康資訊 (編輯)",
    "STUDENTS_MEDICATION_ADMINISTER": "餵藥執行與紀錄",
    "STUDENTS_SPECIAL_NEEDS_READ": "特殊需求 (檢視)",
    "STUDENTS_SPECIAL_NEEDS_WRITE": "特殊需求 (編輯 / IEP)",
    # 家園溝通平台
    "PARENT_MESSAGES_WRITE": "家長訊息 (發送/回覆)",
    # 教育部申報模組
    "GOV_REPORTS_VIEW": "政府申報資料 (檢視)",
    "GOV_REPORTS_EXPORT": "政府申報匯出 (執行)",
    # 教職員考核
    "APPRAISAL_READ": "考核資料 (檢視)",
    "APPRAISAL_EVENT_WRITE": "考核事件 (登錄)",
    "APPRAISAL_REVIEW": "考核簽核 (主管第一階)",
    "APPRAISAL_ACCOUNTING": "考核核數字 (會計第二階)",
    "APPRAISAL_FINALIZE": "考核核定 (最高主管第三階)",
    "APPRAISAL_RULE_WRITE": "考核扣分規則設定 (Phase 1 calibrate)",
    # 年終獎金結算
    "YEAR_END_READ": "年終結算 (檢視)",
    "YEAR_END_WRITE": "年終結算 (編輯)",
    "YEAR_END_FINALIZE": "年終核定 (最高主管)",
    # 廠商付款簽收
    "VENDOR_PAYMENT_READ": "廠商付款簽收 (檢視)",
    "VENDOR_PAYMENT_WRITE": "廠商付款簽收 (編輯/簽收)",
    # DB-driven 自訂權限/角色 CRUD 守衛 ((b) 子專案)
    "ROLES_MANAGE": "角色與權限管理",
}

# 權限分組 (供前端 UI 使用)
# permissions: 不拆分的單一權限
# split_permissions: 讀寫配對（module=顯示名稱, read/write=權限 key）
PERMISSION_GROUPS: List[Dict] = [
    {
        "name": "首頁",
        "permissions": ["DASHBOARD", "APPROVALS"],
    },
    {
        "name": "考勤管理",
        "permissions": ["CALENDAR", "SCHEDULE", "MEETINGS"],
        "split_permissions": [
            {
                "module": "出勤管理",
                "read": "ATTENDANCE_READ",
                "write": "ATTENDANCE_WRITE",
            },
            {"module": "請假管理", "read": "LEAVES_READ", "write": "LEAVES_WRITE"},
            {"module": "加班管理", "read": "OVERTIME_READ", "write": "OVERTIME_WRITE"},
        ],
    },
    {
        "name": "人事教務",
        "permissions": [
            "ACTIVITY_PAYMENT_APPROVE",
            "STUDENTS_LIFECYCLE_WRITE",
            "RECRUITMENT_CONVERT",
        ],
        "split_permissions": [
            {
                "module": "員工管理",
                "read": "EMPLOYEES_READ",
                "write": "EMPLOYEES_WRITE",
            },
            {"module": "學生管理", "read": "STUDENTS_READ", "write": "STUDENTS_WRITE"},
            {
                "module": "監護人資料",
                "read": "GUARDIANS_READ",
                "write": "GUARDIANS_WRITE",
            },
            {
                "module": "班級管理",
                "read": "CLASSROOMS_READ",
                "write": "CLASSROOMS_WRITE",
            },
            {"module": "薪資管理", "read": "SALARY_READ", "write": "SALARY_WRITE"},
            {"module": "課後才藝", "read": "ACTIVITY_READ", "write": "ACTIVITY_WRITE"},
            {
                "module": "接送通知",
                "read": "DISMISSAL_CALLS_READ",
                "write": "DISMISSAL_CALLS_WRITE",
            },
            {"module": "學費管理", "read": "FEES_READ", "write": "FEES_WRITE"},
            {
                "module": "招生統計",
                "read": "RECRUITMENT_READ",
                "write": "RECRUITMENT_WRITE",
            },
        ],
    },
    {
        "name": "教職員考核",
        "permissions": [
            "APPRAISAL_REVIEW",
            "APPRAISAL_ACCOUNTING",
            "APPRAISAL_FINALIZE",
            "YEAR_END_FINALIZE",
        ],
        "split_permissions": [
            {
                "module": "考核資料",
                "read": "APPRAISAL_READ",
                "write": "APPRAISAL_EVENT_WRITE",
            },
            {
                "module": "年終結算",
                "read": "YEAR_END_READ",
                "write": "YEAR_END_WRITE",
            },
        ],
    },
    {
        "name": "園務行政",
        "permissions": [
            "REPORTS",
            "AUDIT_LOGS",
            "BUSINESS_ANALYTICS",
            "PARENT_MESSAGES_WRITE",
            "GOV_REPORTS_VIEW",
            "GOV_REPORTS_EXPORT",
        ],
        "split_permissions": [
            {
                "module": "公告管理",
                "read": "ANNOUNCEMENTS_READ",
                "write": "ANNOUNCEMENTS_WRITE",
            },
            {
                "module": "廠商付款簽收",
                "read": "VENDOR_PAYMENT_READ",
                "write": "VENDOR_PAYMENT_WRITE",
            },
        ],
    },
    {
        "name": "成長歷程 / 教務",
        "permissions": [
            "PORTFOLIO_PUBLISH",
            "STUDENTS_MEDICATION_ADMINISTER",
        ],
        "split_permissions": [
            {
                "module": "成長歷程",
                "read": "PORTFOLIO_READ",
                "write": "PORTFOLIO_WRITE",
            },
            {
                "module": "健康資訊",
                "read": "STUDENTS_HEALTH_READ",
                "write": "STUDENTS_HEALTH_WRITE",
            },
            {
                "module": "特殊需求",
                "read": "STUDENTS_SPECIAL_NEEDS_READ",
                "write": "STUDENTS_SPECIAL_NEEDS_WRITE",
            },
        ],
    },
    {
        "name": "系統",
        "permissions": [],
        "split_permissions": [
            {"module": "系統設定", "read": "SETTINGS_READ", "write": "SETTINGS_WRITE"},
            {
                "module": "帳號管理",
                "read": "USER_MANAGEMENT_READ",
                "write": "USER_MANAGEMENT_WRITE",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Runtime helpers（text[] 版本）
# ---------------------------------------------------------------------------


def get_role_default_permissions(session, role_code: str) -> List[str]:
    """從 DB roles 表拉指定 role 的預設 permissions。

    fallback：未知 role 回 teacher 預設（既有行為）。
    """
    from models.permission_models import Role

    role = session.query(Role).filter_by(code=role_code).first()
    if role is None:
        teacher_role = session.query(Role).filter_by(code="teacher").first()
        return list(teacher_role.permissions) if teacher_role else []
    return list(role.permissions)


def has_permission(
    user_perms: List[str] | None,
    required: "Permission | str",
) -> bool:
    """單一權限檢查。

    user_perms 應為已 resolve 完的最終 list（從 resolve_user_permissions 取得）。
    若 caller 傳 None，視為「無權限」回 False；不在 helper 內 fallback role。
    required 接受 Permission enum 或 str。
    """
    if user_perms is None:
        return False
    if WILDCARD in user_perms:
        return True
    name = required.value if isinstance(required, Permission) else required
    return name in user_perms


def resolve_user_permissions(user) -> List[str]:
    """從 User 物件取出最終權限清單。

    - permission_names is None → 套用 role 預設模板
    - permission_names 為 list → 原樣回傳（已 override role 預設）
    """
    if user.permission_names is None:
        return list(ROLE_TEMPLATES.get(user.role, []))
    return list(user.permission_names)


def get_permission_list(user_perms: List[str] | None) -> List[str]:
    """展開權限清單為合法權限名稱列表。

    - None → []
    - 含 wildcard "*" → 全部 63 個 Permission name
    - 否則 → 過濾掉非法名稱
    """
    if user_perms is None:
        return []
    if WILDCARD in user_perms:
        return [p.value for p in Permission]
    return [p for p in user_perms if p in Permission.__members__]


def get_permissions_definition(session) -> Dict:
    """取得完整權限定義（從 DB permission_definitions + roles 兩表拉，取代 in-code dict）。

    runtime 從 DB 拉確保 admin runtime 改動立即生效。in-code dict 保留供 alembic
    rolesdb01 seed 用，但 runtime 不再參考。
    """
    from models.permission_models import PermissionDefinition, Role

    perm_defs = (
        session.query(PermissionDefinition)
        .order_by(PermissionDefinition.group_name, PermissionDefinition.code)
        .all()
    )
    role_defs = session.query(Role).order_by(Role.is_core.desc(), Role.code).all()

    permissions = {
        p.code: {"value": p.code, "label": p.label, "is_core": p.is_core}
        for p in perm_defs
    }

    # 動態組 groups：依 group_name 分群，對齊 SPLIT_MODULES 為 split_permissions
    split_codes = set()
    for sp in SPLIT_MODULES.values():
        split_codes.add(sp["read"])
        split_codes.add(sp["write"])

    groups_map: Dict[str, Dict] = {}
    for p in perm_defs:
        if p.group_name not in groups_map:
            groups_map[p.group_name] = {
                "name": p.group_name,
                "permissions": [],
                "split_permissions": [],
            }
        if p.code not in split_codes:
            groups_map[p.group_name]["permissions"].append(p.code)

    # 把 SPLIT_MODULES 的 read/write 配對加進對應 group
    for module_key, sp in SPLIT_MODULES.items():
        read_def = next((p for p in perm_defs if p.code == sp["read"]), None)
        if read_def and read_def.group_name in groups_map:
            module_label = PERMISSION_LABELS.get(sp["read"], sp["read"]).replace(
                " (檢視)", ""
            )
            groups_map[read_def.group_name]["split_permissions"].append(
                {
                    "module": module_label,
                    "read": sp["read"],
                    "write": sp["write"],
                }
            )

    groups = list(groups_map.values())

    roles = {
        r.code: {
            "label": r.label,
            "description": r.description or "",
            "permissions": list(r.permissions),
            "is_core": r.is_core,
        }
        for r in role_defs
    }

    return {
        "permissions": permissions,
        "groups": groups,
        "roles": roles,
        "split_modules": SPLIT_MODULES,
    }


# === PermissionGrant + resolve_grant (added Task 2) ===

from typing import NamedTuple, Optional


class PermissionGrant(NamedTuple):
    code: str
    scope: Optional[str]  # "all" | "own_class" | None (no scope_options)


# scope ranking: higher index = broader
_SCOPE_BREADTH = {"own_class": 0, "all": 1}


def resolve_grant(user, code: str) -> Optional[PermissionGrant]:
    """Resolve a user's grant for a permission code.

    Returns:
        PermissionGrant(code, scope) where scope is 'all' / 'own_class' / None.
        None if user does not hold this permission.

    Rules:
        - wildcard '*' → ('all')
        - bare 'STUDENTS_READ' → ('all')  [backward compat]
        - 'STUDENTS_READ:own_class' → ('own_class')
        - both bare and scoped present → broader (all) wins
        - multiple scoped → broadest wins
        - None / empty permission_names → None
    """
    names = getattr(user, "permission_names", None) or []
    if WILDCARD in names:
        return PermissionGrant(code, "all")

    found_scopes: list[str] = []
    for n in names:
        if n == code:
            found_scopes.append("all")
        elif n.startswith(f"{code}:"):
            scope = n.split(":", 1)[1]
            found_scopes.append(scope)

    if not found_scopes:
        return None

    # pick broadest valid scope; fail-closed if all scopes are invalid strings
    valid = [s for s in found_scopes if s in _SCOPE_BREADTH]
    if not valid:
        return None
    broadest = max(valid, key=lambda s: _SCOPE_BREADTH[s])
    return PermissionGrant(code, broadest)

